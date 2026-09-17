import sqlite3
import logging
import re
from pathlib import Path

import os
import requests


# ============================================================
# CONFIGURATION
# ============================================================

TRELLO_API_KEY = os.getenv(
    "TRELLO_API_KEY"
)

# Accept either name.
TRELLO_TOKEN = (
    os.getenv("TRELLO_API_TOKEN")
    or os.getenv("TRELLO_TOKEN")
)

TRELLO_EXISTING_LIST_IDS = os.getenv(
    "TRELLO_EXISTING_LIST_IDS",
    ""
)

TRELLO_BOARD_ID = os.getenv(
    "TRELLO_BOARD_ID"
)

TRELLO_BOARD_NAME = "SPRING Daily Tasks"


def get_trello_cards():
    """
    Get all cards from the configured Trello board.
    """

    url = (
        f"https://api.trello.com/1/boards/"
        f"{TRELLO_BOARD_ID}/cards"
    )

    params = {
        "key": TRELLO_API_KEY,
        "token": TRELLO_TOKEN,
        "fields": "id,name"
    }

    response = requests.get(
        url,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    return response.json()

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent / "clients.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_client_database():
    """
    Create the clients table if it does not already exist.
    """

    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                trello_card_id TEXT NOT NULL UNIQUE,
                trello_card_name TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()

    logger.info("Client database initialized: %s", DB_PATH)



def normalize_phone(phone):
    """Normalize UK phone numbers consistently for WhatsApp/CRM matching.

    In addition to the usual formats, accept a UK mobile written without the
    national leading 0, e.g. 7599328827 -> 447599328827.
    """
    if not phone:
        return None

    digits = re.sub(r"\D", "", str(phone))
    if not digits:
        return None

    if digits.startswith("00"):
        digits = digits[2:]

    # Standard UK local mobile: 07599328827 -> 447599328827
    if digits.startswith("07") and len(digits) == 11:
        digits = "44" + digits[1:]

    # UK mobile stored/entered without the leading 0: 7599328827 -> 447599328827
    elif len(digits) == 10 and digits.startswith("7"):
        digits = "44" + digits

    # Other UK local 11-digit forms beginning with 0.
    elif digits.startswith("0") and len(digits) == 11:
        digits = "44" + digits[1:]

    # Leave already international values untouched.
    if len(digits) < 8 or len(digits) > 15:
        return None

    return digits


def extract_phone_from_card_name(card_name):
    """
    Extract the phone number from your Trello card format.

    Expected format:

        ALAND WALI – Phone: 07387850341 – Eviction date:

    Returns:

        447387850341
    """

    if not card_name:
        return None

    match = re.search(
        r"Phone\s*:\s*([+\d][\d\s().-]*)",
        card_name,
        re.IGNORECASE
    )

    if not match:
        return None

    raw_phone = match.group(1)

    # Stop at the next dash if the regex captured it.
    raw_phone = re.split(r"\s*[–—-]\s*", raw_phone)[0]

    return normalize_phone(raw_phone)


def extract_client_name(card_name):
    """
    Extract the client name from a Trello card.

    Examples:

        ALAND WALI – Phone: 07387850341 – Eviction date:
            -> ALAND WALI

        CAVITA SEESARRAN – Phone: 07395331578
            -> CAVITA SEESARRAN

        YASSIR ABDULJABAR MARAJAN – Phone: 07822843579 – Eviction date: 2026-07-06
            -> YASSIR ABDULJABAR MARAJAN
    """

    if not card_name:
        return None

    # Everything before "Phone:"
    match = re.match(
        r"^\s*(.*?)\s*[–—-]\s*Phone\s*:",
        card_name,
        re.IGNORECASE
    )

    if match:
        return match.group(1).strip()

    # Fallback if formatting doesn't contain the dash before Phone.
    match = re.match(
        r"^\s*(.*?)\s*Phone\s*:",
        card_name,
        re.IGNORECASE
    )

    if match:
        return match.group(1).strip()

    return None


def save_client(
    name,
    phone,
    trello_card_id,
    trello_card_name=None
):
    """
    Insert or update one client.

    The phone number and Trello card ID are both unique.
    """

    phone = normalize_phone(phone)

    if not phone:
        logger.warning(
            "Cannot save client '%s': invalid phone number",
            name
        )
        return False

    if not trello_card_id:
        logger.warning(
            "Cannot save client '%s': missing Trello card ID",
            name
        )
        return False

    with get_connection() as conn:

        conn.execute("""
            INSERT INTO clients (
                name,
                phone,
                trello_card_id,
                trello_card_name,
                updated_at
            )
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)

            ON CONFLICT(phone)
            DO UPDATE SET
                name = excluded.name,
                trello_card_id = excluded.trello_card_id,
                trello_card_name = excluded.trello_card_name,
                updated_at = CURRENT_TIMESTAMP
        """, (
            name,
            phone,
            trello_card_id,
            trello_card_name
        ))

        conn.commit()

    return True


def find_client_by_phone(phone):
    """
    Find a client using their normalized WhatsApp phone number.
    """

    phone = normalize_phone(phone)

    if not phone:
        return None

    with get_connection() as conn:

        row = conn.execute("""
            SELECT
                id,
                name,
                phone,
                trello_card_id,
                trello_card_name
            FROM clients
            WHERE phone = ?
            LIMIT 1
        """, (phone,)).fetchone()

    return row



def sync_clients_from_trello():
    """
    Synchronize Trello client cards into SQLite.

    Trello is the source of truth.
    """

    logger.info("Starting Trello -> SQLite client synchronization")

    init_client_database()

    cards = get_trello_cards()

    if cards is None:
        logger.error("Trello returned no card data")
        return False

    imported = 0
    skipped = 0

    logger.info("Found %d Trello cards", len(cards))

    for card in cards:

        card_id = card.get("id")
        card_name = (card.get("name") or "").strip()

        if not card_id:
            logger.warning("Skipping card with no ID")
            skipped += 1
            continue

        if not card_name:
            logger.warning(
                "Skipping Trello card %s with empty name",
                card_id
            )
            skipped += 1
            continue

        # ------------------------------------------
        # Extract client information
        # ------------------------------------------

        client_name = extract_client_name(card_name)
        phone = extract_phone_from_card_name(card_name)

        if not client_name:
            logger.warning(
                "Could not extract client name from: %s",
                card_name
            )
            skipped += 1
            continue

        if not phone:
            logger.warning(
                "Could not extract phone from: %s",
                card_name
            )
            skipped += 1
            continue

        # ------------------------------------------
        # Save to SQLite
        # ------------------------------------------

        try:
            save_client(
                name=client_name,
                phone=phone,
                trello_card_id=card_id,
                trello_card_name=card_name
            )

            imported += 1

            logger.info(
                "Client synchronized: %s | phone=%s | card=%s",
                client_name,
                phone,
                card_id
            )

        except Exception:
            logger.exception(
                "Failed to synchronize Trello card: %s",
                card_name
            )
            skipped += 1

    logger.info(
        "Trello -> SQLite synchronization finished: "
        "%d imported/updated, %d skipped",
        imported,
        skipped
    )

    return True