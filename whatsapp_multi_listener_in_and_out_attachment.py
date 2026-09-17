# V26: New Message label remains until explicit Trello "Mark Read" action.
import os
import re
import time
import signal
import hashlib
import sqlite3
import requests
import threading
import logging
import json
import pickle
import mimetypes
import tempfile
import shutil
from pathlib import Path
import difflib

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from logging.handlers import RotatingFileHandler
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from datetime import datetime
from playwright.sync_api import sync_playwright

from client_database import find_client_by_phone, normalize_phone as _db_normalize_phone


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


def _normalized_contact_matches(row, number):
    """Compare a CRM contact row against a normalized WhatsApp number."""
    target = normalize_phone(number)
    if not target:
        return False

    for raw in (row[0], row[1]):
        if raw and normalize_phone(raw) == target:
            return True

    return False
from flask import Flask, jsonify

# Instantiate Flask app BEFORE registering routes
app = Flask(__name__)

SYSTEM_CHAT_LABELS = {
    "unread", "all", "groups", "favorites", "chats",
    "community", "archived", "status", "channels",
    "profile details", "contact info", "meta ai", "select chats"
}

# The 5 monitored work accounts — ignored when identifying client phone numbers
WORK_PHONE_NUMBERS = {
    "447421313518",
    "447386215236",
    "447529222889",
    "447386215226",
    "447386215225",
}

IGNORED_CHAT_TITLES = {
    "coss staff team",
    "coss",
    "spring initial a team",
    "wednesday drop-in",
    "monday sanctuary",
    "sanctuary on thursdays",
    "volunteer announcements",
    "whatsapp business",
    "unread",
    "archived",
}

# Substring rules to automatically identify non-client groups or internal chats
IGNORED_TITLE_KEYWORDS = [
    "staff", "team", "drop-in", "sanctuary", "volunteer", 
    "announcements", "cohort", "group", "community"
]


# ============================================================
# CONFIGURATION
# ============================================================

VERSION = "20"

DB_PATH = os.getenv(
    "DB_PATH", "/home/springvolunteer/Airtable2Trello/whatsapp_service.db"
)
account_map = json.loads(os.getenv("ACCOUNT_MAP", "{}"))


# ============================================================
# PRODUCTION / MONITORING CONFIGURATION
# ============================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

LOG_FILE = os.getenv("LOG_FILE", "./whatsapp_service.log")

HEALTH_HOST = os.getenv("HEALTH_HOST", "127.0.0.1")

HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8765"))

ACCOUNT_RESTART_DELAY_SECONDS = int(
    os.getenv("ACCOUNT_RESTART_DELAY_SECONDS", "15")
)

MAX_ACCOUNT_RESTART_DELAY_SECONDS = int(
    os.getenv("MAX_ACCOUNT_RESTART_DELAY_SECONDS", "300")
)

ACCOUNT_HEALTH_INTERVAL_SECONDS = int(
    os.getenv("ACCOUNT_HEALTH_INTERVAL_SECONDS", "30")
)

WHATSAPP_LOAD_TIMEOUT_MS = int(os.getenv("WHATSAPP_LOAD_TIMEOUT_MS", "60000"))

WHATSAPP_READY_TIMEOUT_MS = int(
    os.getenv("WHATSAPP_READY_TIMEOUT_MS", "60000")
)

# ============================================================
# OUTGOING MESSAGE MONITOR STATE
# ============================================================

# Keeps track of outgoing messages already seen in the currently
# monitored chat.
#
# Key:
#     (whatsapp_account, phone)
#
# Value:
#     set of message hashes
#
# The database / already_processed() check remains the final
# deduplication safety net.
OUTGOING_MESSAGE_STATE = {}

OUTGOING_MESSAGE_STATE_LOCK = threading.Lock()

# Passive sidebar monitoring for messages synchronized from a physical phone.
# This path never clicks or opens a conversation.
SIDEBAR_MESSAGE_STATE = {}
SIDEBAR_MESSAGE_STATE_LOCK = threading.RLock()

# Tracks hashes already observed for operator-opened chats. The first scan
# establishes history as a baseline instead of treating old messages as new.
OPEN_CHAT_BASELINE_STATE = {}
OPEN_CHAT_BASELINE_LOCK = threading.RLock()

# Old automatic unread-chat opening is opt-in. Default is passive monitoring.
WHATSAPP_AUTO_UNREAD = os.getenv("WHATSAPP_AUTO_UNREAD", "false").strip().lower() in {
    "1", "true", "yes", "y", "on"
}

# Manual unread recovery is enabled by default: marking a WhatsApp chat/message unread
# is treated as an explicit request to reconcile its missing Trello/Drive work.
WHATSAPP_MANUAL_UNREAD_RECOVERY = os.getenv(
    "WHATSAPP_MANUAL_UNREAD_RECOVERY", "true"
).strip().lower() in {"1", "true", "yes", "y", "on"}

# ============================================================
# GOOGLE DRIVE CONFIGURATION
# ============================================================

DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

GOOGLE_CREDENTIALS_PATH = os.getenv(
    "GOOGLE_CREDENTIALS_PATH",
    "/home/springvolunteer/Airtable2Trello/credentials.json",
)

GOOGLE_TOKEN_PATH = os.getenv("GOOGLE_TOKEN_PATH", "./google_drive_token.pickle")

# Full Drive scope is recommended here because the service
# needs to create folders and upload files into them.
GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

WHATSAPP_ATTACHMENT_DIR = os.getenv(
    "WHATSAPP_ATTACHMENT_DIR", "./whatsapp_attachments"
)

os.makedirs(WHATSAPP_ATTACHMENT_DIR, exist_ok=True)

# ============================================================
# OUTGOING MESSAGE MONITOR STATE HELPERS
# ============================================================


def get_outgoing_message_state_key(whatsapp_account, phone):
    return (whatsapp_account, phone)


def get_seen_outgoing_hashes(whatsapp_account, phone):
    key = get_outgoing_message_state_key(whatsapp_account, phone)

    with OUTGOING_MESSAGE_STATE_LOCK:
        return set(OUTGOING_MESSAGE_STATE.get(key, set()))


def update_seen_outgoing_hashes(whatsapp_account, phone, hashes):
    if not hashes:
        return

    key = get_outgoing_message_state_key(whatsapp_account, phone)

    with OUTGOING_MESSAGE_STATE_LOCK:
        existing = OUTGOING_MESSAGE_STATE.setdefault(key, set())

        existing.update(hashes)


def clear_outgoing_message_state(whatsapp_account, phone):
    key = get_outgoing_message_state_key(whatsapp_account, phone)

    with OUTGOING_MESSAGE_STATE_LOCK:
        OUTGOING_MESSAGE_STATE.pop(key, None)


# ============================================================
# GOOGLE DRIVE
# ============================================================

DRIVE_SERVICE_LOCK = threading.RLock()


def get_drive_service():
    with DRIVE_SERVICE_LOCK:
        creds = None

        if os.path.exists(GOOGLE_TOKEN_PATH):
            with open(GOOGLE_TOKEN_PATH, "rb") as token:
                creds = pickle.load(token)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request

                creds.refresh(Request())

            else:
                if not os.path.exists(GOOGLE_CREDENTIALS_PATH):
                    raise RuntimeError(
                        "Google credentials file not found: "
                        f"{GOOGLE_CREDENTIALS_PATH}"
                    )

                flow = InstalledAppFlow.from_client_secrets_file(
                    GOOGLE_CREDENTIALS_PATH, GOOGLE_DRIVE_SCOPES
                )

                creds = flow.run_local_server(port=0)

            with open(GOOGLE_TOKEN_PATH, "wb") as token:
                pickle.dump(creds, token)

        return build("drive", "v3", credentials=creds)


def get_trello_card(card_id):
    return trello_request("GET", f"/cards/{card_id}")


# ============================================================
# GOOGLE DRIVE FOLDER
# ============================================================
def ensure_client_drive_folder(phone, fullname=None):
    """Ensure the client's canonical WhatsApp-number Drive folder exists.

    This function ONLY creates/finds the Drive folder. It deliberately does not
    modify the Trello card description. A folder link is added to Trello only
    after a real WhatsApp attachment has been successfully uploaded.
    """
    phone = normalize_phone(phone)
    if not phone:
        raise ValueError(
            "Cannot create client Drive folder without a valid WhatsApp phone"
        )

    logger.info(
        "DRIVE FOLDER IDENTITY: whatsapp_phone=%s fullname=%r",
        phone,
        fullname,
    )

    folder = get_or_create_client_drive_folder(phone, fullname)
    folder_url = folder.get("url")
    if not folder_url:
        raise RuntimeError(
            f"Could not retrieve Google Drive folder URL for phone {phone}"
        )

    return folder


def drive_folder_contains_file(folder_id):
    """Return True only when the Drive folder contains at least one real file."""
    if not folder_id:
        return False

    service = get_drive_service()
    result = (
        service.files()
        .list(
            q=(
                f"'{folder_id}' in parents and trashed = false "
                "and mimeType != 'application/vnd.google-apps.folder'"
            ),
            spaces="drive",
            fields="files(id,name,size,mimeType)",
            pageSize=1,
        )
        .execute()
    )
    return bool(result.get("files"))


def add_drive_folder_link_to_trello_card(
    card_id, folder_url, phone=None, folder_id=None
):
    """Add the canonical Drive folder URL only when the folder has a real file."""
    if not card_id or not folder_url:
        return False

    try:
        if not folder_id:
            marker = "/folders/"
            if marker in folder_url:
                folder_id = folder_url.split(marker, 1)[1].split("?", 1)[0].split("/", 1)[0]

        if not drive_folder_contains_file(folder_id):
            logger.warning(
                "Not adding empty Drive folder to Trello card %s%s: %s",
                card_id,
                f" for phone {phone}" if phone else "",
                folder_url,
            )
            return False

        full_card = trello_request(
            "GET", f"/cards/{card_id}", {"fields": "id,name,desc"}
        )
        current_description = full_card.get("desc") or ""

        if folder_url in current_description:
            logger.info(
                "Drive folder link already exists on Trello card %s%s",
                card_id,
                f" for phone {phone}" if phone else "",
            )
            return True

        addition = "\n\n📁 WhatsApp Attachments:\n" f"[{folder_url.rsplit('/', 1)[-1]}]({folder_url})"
        new_description = (current_description + addition)[:15000]

        trello_request(
            "PUT", f"/cards/{card_id}", {"desc": new_description}
        )

        logger.info(
            "Successfully added Drive folder link to Trello card %s%s",
            card_id,
            f" for phone {phone}" if phone else "",
        )
        return True
    except Exception as e:
        logger.error(
            "Error adding Drive folder link to Trello card %s%s: %s",
            card_id,
            f" for phone {phone}" if phone else "",
            e,
        )
        return False


def is_ignored_chat_title(title: str) -> bool:
    """
    Checks if a chat title corresponds to a group, staff room, or system label.
    """
    if not title:
        return True

    # 1. Normalize whitespace
    clean_title = re.sub(r'\s+', ' ', title).strip().lower()

    # 2. Strip unread badge counts from BOTH prefix and suffix
    # e.g., "(4) Profile details" -> "profile details" or "John Doe (3)" -> "john doe"
    clean_title = re.sub(r'^\(\d+\)\s*', '', clean_title)
    clean_title = re.sub(r'\s*\(\d+\)$', '', clean_title)

    # 3. Exact match check
    if clean_title in SYSTEM_CHAT_LABELS or clean_title in IGNORED_CHAT_TITLES:
        return True

    # 4. Keyword check with boundary handling for hyphens/punctuation
    for keyword in IGNORED_TITLE_KEYWORDS:
        kw = keyword.lower()
        # Escaped keyword wrapped in flexible boundaries allowing spaces/hyphens
        pattern = r'(?:^|[^\w])' + re.escape(kw) + r'(?:[^\w]|$)'
        if re.search(pattern, clean_title):
            return True

    return False


def canonical_whatsapp_phone(contact=None, fallback_phone=None):
    """Return the canonical WhatsApp number for a client.

    For client records, ``whatsapp_phone`` is the identity key used for
    WhatsApp -> Trello matching and therefore MUST also be the identity key
    used for Google Drive folders/uploads. The CRM/UK ``phone`` remains a
    separate field and is never preferred for Drive.
    """
    candidates = []
    if isinstance(contact, dict):
        candidates.append(contact.get("whatsapp_phone"))
    candidates.append(fallback_phone)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            value = normalize_phone(candidate)
        except Exception:
            continue
        if value:
            return value

    return None


def clean_drive_filename(filename):
    if not filename:
        return "WhatsApp attachment"

    filename = os.path.basename(str(filename))

    filename = re.sub(r'[<>:"/\\|?*]', "_", filename)

    filename = re.sub(r"\s+", " ", filename).strip()

    if not filename:
        filename = "WhatsApp attachment"

    return filename[:200]


def restrict_drive_item_to_owner(file_id, label="Drive item"):
    """Remove direct public/link/domain permissions from a Drive item.

    Newly-created Drive items normally inherit the parent folder's access.
    Direct `anyone`/domain permissions are explicitly removed here so the
    uploaded WhatsApp file and its client folder are not intentionally shared
    by this service. Inherited permissions cannot be removed at the child;
    those are reported in the log rather than silently treated as fixed.
    """
    if not file_id:
        return False

    try:
        service = get_drive_service()
        permissions = (
            service.permissions()
            .list(
                fileId=file_id,
                fields="permissions(id,type,role,emailAddress,domain,allowFileDiscovery,permissionDetails(inherited,inheritedFrom))",
                pageSize=100,
            )
            .execute()
            .get("permissions", [])
        )

        owner_found = False
        public_removed = 0
        inherited_public = []

        for perm in permissions:
            ptype = (perm.get("type") or "").lower()
            role = (perm.get("role") or "").lower()
            details = perm.get("permissionDetails") or []
            inherited = bool(
                any(d.get("inherited") for d in details)
                or perm.get("inheritedFrom")
                or perm.get("inherited")
            )

            if ptype == "user" and role == "owner":
                owner_found = True
                continue

            if ptype in {"anyone", "domain"}:
                if inherited:
                    inherited_public.append(perm)
                    continue
                try:
                    service.permissions().delete(
                        fileId=file_id,
                        permissionId=perm["id"],
                    ).execute()
                    public_removed += 1
                except Exception as exc:
                    logger.warning(
                        "%s: could not remove direct %s permission id=%s: %s",
                        label, ptype, perm.get("id"), exc,
                    )

        if inherited_public:
            logger.warning(
                "%s still has inherited public/domain access from its parent; "
                "child-level restriction cannot remove inherited permissions. "
                "inherited=%r",
                label, inherited_public,
            )

        logger.info(
            "%s Drive permissions secured: owner=%s direct_public_removed=%d inherited_public=%d",
            label, owner_found, public_removed, len(inherited_public),
        )
        return owner_found and not inherited_public
    except Exception as exc:
        logger.warning("%s: Drive permission security check failed: %s", label, exc)
        return False


def get_or_create_client_drive_folder(phone, fullname=None):
    """Return/create the client's canonical WhatsApp-number Drive folder.

    Drive identity may be a UK or international WhatsApp number.  Do not use
    the UK-only ``clean_phone()`` validator here because it would reject valid
    international WhatsApp numbers such as ``33773700133``.
    """
    phone = normalize_phone(phone)

    if not phone:
        raise ValueError(
            "Cannot create Drive folder without a valid WhatsApp phone number."
        )

    if not DRIVE_FOLDER_ID:
        raise RuntimeError(
            "DRIVE_FOLDER_ID environment variable is not configured."
        )

    service = get_drive_service()

    # --------------------------------------------------------
    # Folder name
    # --------------------------------------------------------

    safe_name = clean_drive_filename(fullname or "Client")

    folder_name = f"{safe_name} - {phone}"

    # --------------------------------------------------------
    # Find existing folder
    # --------------------------------------------------------

    query = (
        "trashed = false and mimeType = 'application/vnd.google-apps.folder'"
        f" and '{DRIVE_FOLDER_ID}' in parents and name ="
        f" '{folder_name.replace(chr(39), chr(39) + chr(39))}'"
    )

    results = (
        service.files()
        .list(
            q=query,
            spaces="drive",
            fields="files(id,name,webViewLink)",
            pageSize=100,
        )
        .execute()
    )

    folders = results.get("files", [])

    if folders:
        # Multiple legacy folders with the same canonical name can exist.
        # Prefer a folder that already contains a real uploaded file so a new
        # empty folder is never created/selected merely because it sorts first.
        ranked = []
        for candidate in folders:
            count = 0
            try:
                listing = (
                    service.files()
                    .list(
                        q=(
                            f"'{candidate['id']}' in parents and trashed = false "
                            "and mimeType != 'application/vnd.google-apps.folder'"
                        ),
                        spaces="drive",
                        fields="files(id)",
                        pageSize=100,
                    )
                    .execute()
                )
                count = len(listing.get("files", []))
            except Exception as exc:
                logger.warning(
                    "Could not inspect candidate Drive folder %s: %s",
                    candidate.get("id"),
                    exc,
                )
            ranked.append((count, candidate))

        ranked.sort(key=lambda item: item[0], reverse=True)
        populated_count, folder = ranked[0]

        # Remove direct public/domain sharing from the selected canonical folder.
        restrict_drive_item_to_owner(
            folder["id"],
            label=f"Drive folder {folder_name!r}",
        )

        logger.info(
            "Using existing canonical Drive folder %r id=%s files=%d candidates=%d",
            folder_name, folder["id"], populated_count, len(folders),
        )

        return {
            "id": folder["id"],
            "name": folder["name"],
            "url": (
                folder.get("webViewLink")
                or f"https://drive.google.com/drive/folders/{folder['id']}"
            ),
            "created": False,
            "has_files": populated_count > 0,
        }

    # --------------------------------------------------------
    # Create folder
    # --------------------------------------------------------

    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [DRIVE_FOLDER_ID],
    }

    folder = (
        service.files()
        .create(body=metadata, fields="id,name,webViewLink")
        .execute()
    )

    folder_id = folder["id"]

    restrict_drive_item_to_owner(
        folder_id,
        label=f"Drive folder {folder_name!r}",
    )

    return {
        "id": folder_id,
        "name": folder.get("name", folder_name),
        "url": (
            folder.get("webViewLink")
            or f"https://drive.google.com/drive/folders/{folder_id}"
        ),
        "created": True,
    }


# ------------------------------------------------------------
# MULTIPLE WHATSAPP ACCOUNTS
# ------------------------------------------------------------

WHATSAPP_ACCOUNTS = {
    "main": {
        "name": "Main WhatsApp",
        "profile": "./whatsapp_profiles/main",
    },
    "spring2": {
        "name": "Spring2 WhatsApp",
        "profile": "./whatsapp_profiles/spring2",
    },
    "spring3": {
        "name": "Spring3 WhatsApp",
        "profile": "./whatsapp_profiles/spring3",
    },
    "spring4": {
        "name": "Spring4 WhatsApp",
        "profile": "./whatsapp_profiles/spring4",
    },
    "spring5": {
        "name": "Spring5 WhatsApp",
        "profile": "./whatsapp_profiles/spring5",
    },
}


TRELLO_API_KEY = os.getenv("TRELLO_API_KEY")

TRELLO_TOKEN = os.getenv("TRELLO_API_TOKEN") or os.getenv("TRELLO_TOKEN")

TRELLO_EXISTING_LIST_IDS = os.getenv("TRELLO_EXISTING_LIST_IDS", "")

TRELLO_BOARD_NAME = "SPRING Daily Tasks"

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "10"))

TRELLO_SYNC_INTERVAL_SECONDS = int(
    os.getenv("TRELLO_SYNC_INTERVAL_SECONDS", "300")
)

TRELLO_NEW_MESSAGE_LABEL_NAME = "New Message"

TRELLO_NEW_MESSAGE_LABEL_COLOR = "blue"


# ============================================================
# GLOBAL LOCKS
# ============================================================

DATABASE_LOCK = threading.RLock()

TRELLO_LOCK = threading.RLock()

TRELLO_SYNC_LOCK = threading.Lock()


# ============================================================
# SERVICE STATE
# ============================================================

SHUTDOWN_EVENT = threading.Event()

SERVICE_STARTED_AT = datetime.now()

ACCOUNT_STATUS_LOCK = threading.RLock()

ACCOUNT_STATUS = {}


def update_account_status(account_id, status, error=None):
    with ACCOUNT_STATUS_LOCK:
        current = ACCOUNT_STATUS.setdefault(account_id, {})

        current["status"] = status

        current["updated_at"] = datetime.now().isoformat()

        if error is not None:
            current["last_error"] = str(error)

        current.setdefault("restart_count", 0)

        current.setdefault("last_check", None)


def mark_account_check(account_id):
    with ACCOUNT_STATUS_LOCK:
        current = ACCOUNT_STATUS.setdefault(account_id, {})

        current["last_check"] = datetime.now().isoformat()

        current["status"] = "ONLINE"


# ============================================================
# GRACEFUL SHUTDOWN
# ============================================================


def request_shutdown(signum=None, frame=None):
    if SHUTDOWN_EVENT.is_set():
        return

    logger.warning("Shutdown requested.")

    SHUTDOWN_EVENT.set()


def install_signal_handlers():
    signal.signal(signal.SIGINT, request_shutdown)

    signal.signal(signal.SIGTERM, request_shutdown)


# ============================================================
# CONFIGURATION VALIDATION
# ============================================================


def validate_configuration():
    if not TRELLO_API_KEY:
        raise RuntimeError("TRELLO_API_KEY environment variable is missing.")

    if not TRELLO_TOKEN:
        raise RuntimeError(
            "TRELLO_API_TOKEN or TRELLO_TOKEN environment variable is missing."
        )

    if not TRELLO_EXISTING_LIST_IDS.strip():
        raise RuntimeError(
            "TRELLO_EXISTING_LIST_IDS environment variable is missing."
        )

    if not WHATSAPP_ACCOUNTS:
        raise RuntimeError("No WhatsApp accounts configured.")


# ============================================================
# LOGGING
# ============================================================


def setup_logging():
    logger_instance = logging.getLogger()

    logger_instance.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"
    )

    console_handler = logging.StreamHandler()

    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )

    file_handler.setFormatter(formatter)

    # Prevent duplicate handlers if setup_logging()
    # is ever called more than once.
    logger_instance.handlers.clear()

    logger_instance.addHandler(console_handler)

    logger_instance.addHandler(file_handler)


setup_logging()

logger = logging.getLogger(__name__)


def get_trello_list_ids():
    return [
        item.strip()
        for item in TRELLO_EXISTING_LIST_IDS.split(",")
        if item.strip()
    ]


# ============================================================
# NAME NORMALISATION
# ============================================================


def normalize_person_name(name):
    if not name:
        return ""

    name = str(name)

    name = re.sub(r"[–—−]", "-", name)

    name = re.sub(r"[^\w\s']", " ", name, flags=re.UNICODE)

    name = re.sub(r"\s+", " ", name)

    return name.strip().lower()


# ============================================================
# DATABASE CONNECTION
# ============================================================


def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)

    conn.execute("PRAGMA busy_timeout = 30000")

    conn.execute("PRAGMA journal_mode = WAL")

    return conn


# ============================================================
# DATABASE
# ============================================================


def init_db():
    with DATABASE_LOCK:
        conn = get_db_connection()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                phone TEXT PRIMARY KEY,
                airtable_id TEXT,
                fullname TEXT,
                contact_type TEXT NOT NULL DEFAULT 'client',
                trello_card_id TEXT,
                trello_card_name TEXT,
                updated_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_hash TEXT PRIMARY KEY,
                processed_at TEXT,
                whatsapp_account TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS processing_messages (
                message_hash TEXT PRIMARY KEY,
                whatsapp_account TEXT NOT NULL,
                claimed_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                whatsapp_account TEXT,
                phone TEXT,
                message TEXT,
                direction TEXT NOT NULL DEFAULT 'incoming',
                whatsapp_timestamp TEXT,
                created_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_message_identities (
                identity_key TEXT PRIMARY KEY,
                whatsapp_account TEXT,
                phone TEXT,
                direction TEXT,
                message_id TEXT,
                normalized_message TEXT,
                created_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_chat_mapping (
                whatsapp_account TEXT NOT NULL,
                chat_name TEXT NOT NULL,
                phone TEXT,
                last_seen TEXT,
                PRIMARY KEY (
                    whatsapp_account,
                    chat_name
                )
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                whatsapp_account TEXT NOT NULL,

                phone TEXT NOT NULL,

                message_hash TEXT,

                attachment_hash TEXT NOT NULL,

                filename TEXT,

                mime_type TEXT,

                local_path TEXT,

                drive_file_id TEXT,

                drive_file_url TEXT,

                created_at TEXT,

                UNIQUE(
                    whatsapp_account,
                    attachment_hash
                )
            )
        """)

        conn.commit()
        conn.close()


def attachment_already_uploaded(whatsapp_account, attachment_hash):
    """Return completed Drive metadata only; incomplete rows remain retryable."""
    with DATABASE_LOCK:
        conn = get_db_connection()

        row = conn.execute(
            """
            SELECT
                drive_file_id,
                drive_file_url
            FROM whatsapp_attachments
            WHERE whatsapp_account = ?
              AND attachment_hash = ?
            """,
            (whatsapp_account, attachment_hash),
        ).fetchone()

        conn.close()

    if not row or not row[0] or not row[1]:
        return None

    return {"drive_file_id": row[0], "drive_file_url": row[1]}


def link_uploaded_attachment_to_message(whatsapp_account, attachment_hash, message_hash, phone=None):
    """Associate an already-completed attachment with the current message hash.

    The same binary can be observed through a different DOM wrapper/hash, and
    an existing completed attachment record may therefore predate the current
    message hash.  Linking it here prevents a completed Drive upload from
    being treated as incomplete on the next polling cycle.
    """
    if not whatsapp_account or not attachment_hash or not message_hash:
        return

    with DATABASE_LOCK:
        conn = get_db_connection()
        try:
            if phone:
                conn.execute(
                    """
                    UPDATE whatsapp_attachments
                    SET message_hash = ?, phone = ?
                    WHERE whatsapp_account = ?
                      AND attachment_hash = ?
                      AND drive_file_id IS NOT NULL
                      AND TRIM(drive_file_id) <> ''
                      AND drive_file_url IS NOT NULL
                      AND TRIM(drive_file_url) <> ''
                    """,
                    (message_hash, phone, whatsapp_account, attachment_hash),
                )
            else:
                conn.execute(
                    """
                    UPDATE whatsapp_attachments
                    SET message_hash = ?
                    WHERE whatsapp_account = ?
                      AND attachment_hash = ?
                      AND drive_file_id IS NOT NULL
                      AND TRIM(drive_file_id) <> ''
                      AND drive_file_url IS NOT NULL
                      AND TRIM(drive_file_url) <> ''
                    """,
                    (message_hash, whatsapp_account, attachment_hash),
                )
            conn.commit()
        finally:
            conn.close()


def attachment_upload_completed_for_message(whatsapp_account, message_hash):
    """Whether this WhatsApp message has a completed Drive attachment record."""
    if not whatsapp_account or not message_hash:
        return False

    with DATABASE_LOCK:
        conn = get_db_connection()
        row = conn.execute(
            """
            SELECT 1
            FROM whatsapp_attachments
            WHERE whatsapp_account = ?
              AND message_hash = ?
              AND drive_file_id IS NOT NULL
              AND TRIM(drive_file_id) <> ''
              AND drive_file_url IS NOT NULL
              AND TRIM(drive_file_url) <> ''
            LIMIT 1
            """,
            (whatsapp_account, message_hash),
        ).fetchone()
        conn.close()

    return row is not None


def save_uploaded_attachment(
    whatsapp_account,
    phone,
    message_hash,
    attachment_hash,
    filename,
    mime_type,
    local_path,
    drive_file_id,
    drive_file_url,
):
    with DATABASE_LOCK:
        conn = get_db_connection()

        conn.execute(
            """
            INSERT INTO whatsapp_attachments
            (
                whatsapp_account,
                phone,
                message_hash,
                attachment_hash,
                filename,
                mime_type,
                local_path,
                drive_file_id,
                drive_file_url,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(whatsapp_account, attachment_hash) DO UPDATE SET
                phone = excluded.phone,
                message_hash = excluded.message_hash,
                filename = excluded.filename,
                mime_type = excluded.mime_type,
                local_path = excluded.local_path,
                drive_file_id = excluded.drive_file_id,
                drive_file_url = excluded.drive_file_url
            """,
            (
                whatsapp_account,
                phone,
                message_hash,
                attachment_hash,
                filename,
                mime_type,
                local_path,
                drive_file_id,
                drive_file_url,
                datetime.now().isoformat(),
            ),
        )

        conn.commit()
        conn.close()


# ----------------------------------------------------
# File Hashing
# ----------------------------------------------------


def calculate_file_hash(file_path):
    sha256 = hashlib.sha256()

    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            sha256.update(chunk)

    return sha256.hexdigest()


def migrate_db():
    with DATABASE_LOCK:
        conn = get_db_connection()

        try:
            # ====================================================
            # contacts
            # ====================================================

            columns = [
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(contacts)"
                ).fetchall()
            ]

            # ----------------------------------------------------
            # contact_type
            # ----------------------------------------------------

            if "contact_type" not in columns:
                logger.info("Adding contact_type column to contacts...")

                conn.execute("""
                    ALTER TABLE contacts
                    ADD COLUMN contact_type TEXT
                    NOT NULL DEFAULT 'client'
                """)

                columns.append("contact_type")

            # ----------------------------------------------------
            # whatsapp_phone
            # ----------------------------------------------------

            if "whatsapp_phone" not in columns:
                logger.info("Adding whatsapp_phone column to contacts...")

                conn.execute("""
                    ALTER TABLE contacts
                    ADD COLUMN whatsapp_phone TEXT
                """)

                columns.append("whatsapp_phone")

            # ----------------------------------------------------
            # airtable_id
            # ----------------------------------------------------

            if "airtable_id" not in columns:
                logger.info("Adding airtable_id column to contacts...")

                conn.execute("""
                    ALTER TABLE contacts
                    ADD COLUMN airtable_id TEXT
                """)

                columns.append("airtable_id")

            # ====================================================
            # processed_messages
            # ====================================================

            columns = [
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(processed_messages)"
                ).fetchall()
            ]

            if "whatsapp_account" not in columns:
                logger.info(
                    "Adding whatsapp_account column to processed_messages..."
                )

                conn.execute("""
                    ALTER TABLE processed_messages
                    ADD COLUMN whatsapp_account TEXT
                """)

            # ====================================================
            # whatsapp_messages
            # ====================================================

            columns = [
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(whatsapp_messages)"
                ).fetchall()
            ]

            if "whatsapp_account" not in columns:
                logger.info(
                    "Adding whatsapp_account column to whatsapp_messages..."
                )

                conn.execute("""
                    ALTER TABLE whatsapp_messages
                    ADD COLUMN whatsapp_account TEXT
                """)

            if "direction" not in columns:
                logger.info(
                    "Adding direction column to whatsapp_messages..."
                )

                conn.execute("""
                    ALTER TABLE whatsapp_messages
                    ADD COLUMN direction TEXT
                    NOT NULL DEFAULT 'incoming'
                """)

            if "whatsapp_timestamp" not in columns:
                logger.info(
                    "Adding whatsapp_timestamp column to whatsapp_messages..."
                )

                conn.execute("""
                    ALTER TABLE whatsapp_messages
                    ADD COLUMN whatsapp_timestamp TEXT
                """)

            # ====================================================
            # whatsapp_chat_mapping
            # ====================================================

            conn.execute("""
                CREATE TABLE IF NOT EXISTS
                whatsapp_chat_mapping_v2 (
                    whatsapp_account TEXT NOT NULL,
                    chat_name TEXT NOT NULL,
                    phone TEXT,
                    last_seen TEXT,
                    PRIMARY KEY (
                        whatsapp_account,
                        chat_name
                    )
                )
            """)

            old_mapping_exists = conn.execute("""
                SELECT name
                FROM sqlite_master
                WHERE type='table'
                  AND name='whatsapp_chat_mapping'
            """).fetchone()

            if old_mapping_exists:
                old_columns = [
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(whatsapp_chat_mapping)"
                    ).fetchall()
                ]

                if "whatsapp_account" not in old_columns:
                    rows = conn.execute("""
                        SELECT
                            chat_name,
                            phone,
                            last_seen
                        FROM whatsapp_chat_mapping
                    """).fetchall()

                    for row in rows:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO
                            whatsapp_chat_mapping_v2
                            (
                                whatsapp_account,
                                chat_name,
                                phone,
                                last_seen
                            )
                            VALUES (?, ?, ?, ?)
                        """,
                            ("main", row[0], row[1], row[2]),
                        )

                    conn.execute("""
                        DROP TABLE whatsapp_chat_mapping
                    """)

                    conn.execute("""
                        ALTER TABLE
                        whatsapp_chat_mapping_v2
                        RENAME TO whatsapp_chat_mapping
                    """)

                else:
                    conn.execute("""
                        DROP TABLE IF EXISTS
                        whatsapp_chat_mapping_v2
                    """)

            else:
                conn.execute("""
                    ALTER TABLE
                    whatsapp_chat_mapping_v2
                    RENAME TO whatsapp_chat_mapping
                """)

            conn.commit()

            logger.info("Database migration completed successfully.")

        except Exception:
            conn.rollback()

            logger.exception(
                "Database migration failed. All changes have been rolled back."
            )

            raise

        finally:
            conn.close()


# ============================================================
# Upload the attachment to Drive
# ============================================================
def upload_whatsapp_attachment_to_drive(file_path, phone, fullname=None):
    phone = normalize_phone(phone)
    if not phone:
        raise ValueError("Cannot upload WhatsApp attachment without a valid WhatsApp phone")

    if not os.path.isfile(file_path):
        raise ValueError(f"WhatsApp attachment file does not exist: {file_path}")

    try:
        file_size = os.path.getsize(file_path)
    except OSError as exc:
        raise ValueError(f"Cannot inspect WhatsApp attachment file: {file_path}") from exc

    if file_size <= 0:
        raise ValueError(f"WhatsApp attachment file is empty: {file_path}")

    logger.info(
        "DRIVE UPLOAD IDENTITY: whatsapp_phone=%s fullname=%r folder=%r",
        phone,
        fullname,
        f"{clean_drive_filename(fullname or 'Client')} - {phone}",
    )

    service = get_drive_service()
    folder = get_or_create_client_drive_folder(phone, fullname)

    filename = clean_drive_filename(os.path.basename(file_path))

    mime_type = (
        mimetypes.guess_type(filename)[0] or "application/octet-stream"
    )

    metadata = {"name": filename, "parents": [folder["id"]]}

    media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

    try:
        uploaded = (
            service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id,name,mimeType,webViewLink",
            )
            .execute()
        )
    except Exception:
        # If this invocation created a brand-new folder and the actual file
        # upload failed, clean up that empty folder so failed retries do not
        # accumulate abandoned Drive folders. Never remove an existing folder.
        if folder.get("created"):
            try:
                service.files().delete(fileId=folder["id"]).execute()
                logger.warning(
                    "Deleted newly-created empty Drive folder after upload failure: id=%s",
                    folder["id"],
                )
            except Exception:
                logger.exception(
                    "Could not delete newly-created empty Drive folder after upload failure: id=%s",
                    folder["id"],
                )
        raise

    file_id = uploaded["id"]

    restrict_drive_item_to_owner(
        file_id,
        label=f"Drive file {filename!r}",
    )

    file_url = (
        uploaded.get("webViewLink")
        or f"https://drive.google.com/file/d/{file_id}/view"
    )

    return {
        "file_id": file_id,
        "file_url": file_url,
        "folder_id": folder["id"],
        "folder_url": folder["url"],
        "filename": filename,
        "mime_type": mime_type,
    }


# ============================================================
# Downloading WhatsApp attachments
# ============================================================
def find_download_button(message_element):
    selectors = [
        '[data-testid="download"]',
        '[data-testid*="download"]',
        'button[aria-label*="Download"]',
        '[role="button"][aria-label*="Download"]',
    ]

    for selector in selectors:
        try:
            buttons = message_element.locator(selector)

            for i in range(buttons.count()):
                button = buttons.nth(i)

                try:
                    if button.is_visible():
                        return button

                except Exception:
                    continue

        except Exception:
            continue

    return None


def _visible_download_control(page, root=None):
    """Return the first visible WhatsApp Download control in root/page."""
    selectors = (
        '[data-testid="download"]',
        '[data-testid*="download"]',
        'button[aria-label*="Download" i]',
        '[role="button"][aria-label*="Download" i]',
        '[title*="Download" i]',
    )
    search_root = root if root is not None else page
    for selector in selectors:
        try:
            loc = search_root.locator(selector)
            for i in range(loc.count()):
                candidate = loc.nth(i)
                try:
                    if candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _find_clickable_media_preview(message_element):
    """Find a likely image/document preview that opens WhatsApp's media viewer."""
    selectors = (
        'img',
        'video',
        'canvas',
        '[data-testid*="media"]',
        '[data-testid*="image"]',
        '[data-testid*="document"]',
        '[data-icon*="document"]',
        '[data-icon*="media"]',
    )
    for selector in selectors:
        try:
            loc = message_element.locator(selector)
            for i in range(loc.count()):
                candidate = loc.nth(i)
                try:
                    if candidate.is_visible():
                        return candidate
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _save_playwright_download(download):
    suggested_filename = clean_drive_filename(
        download.suggested_filename or "whatsapp_attachment"
    )
    unique_name = f"{int(time.time() * 1000)}_{suggested_filename}"
    destination = os.path.join(WHATSAPP_ATTACHMENT_DIR, unique_name)
    download.save_as(destination)
    if not os.path.exists(destination):
        logger.warning(
            "WhatsApp attachment download produced no local file: %s",
            destination,
        )
        return None
    logger.info(
        "WhatsApp attachment downloaded successfully: filename=%r path=%s",
        suggested_filename,
        destination,
    )
    return {"path": destination, "filename": suggested_filename}


def download_whatsapp_attachment(page, message_element):
    """Download a WhatsApp document/media attachment to local staging.

    WhatsApp has two common UI states:
      1. a visible Download control on/near the message;
      2. a media/document preview which must first be opened in the viewer,
         after which the viewer exposes its Download control.

    We try the direct control first, then the viewer fallback.  This is the
    important path for image attachments that are detected structurally but
    do not expose a download button in the message DOM.
    """
    try:
        message_element.hover(timeout=2000)
    except Exception:
        pass

    # ------------------------------------------------------------
    # 1. Direct message/ancestor Download control
    # ------------------------------------------------------------
    button = find_download_button(message_element)
    if not button:
        search_roots = [message_element]
        try:
            parent = message_element.locator("xpath=..")
            if parent.count() > 0:
                search_roots.append(parent)
                grandparent = parent.locator("xpath=..")
                if grandparent.count() > 0:
                    search_roots.append(grandparent)
        except Exception:
            pass

        for root in search_roots:
            button = _visible_download_control(page, root)
            if button:
                break

    if button:
        try:
            with page.expect_download(timeout=15000) as download_info:
                try:
                    button.click(timeout=5000)
                except Exception:
                    button.click(timeout=5000, force=True)
            return _save_playwright_download(download_info.value)
        except Exception as e:
            logger.warning(
                "Direct WhatsApp attachment download failed; trying viewer fallback: %s",
                e,
            )

    # ------------------------------------------------------------
    # 2. Open the media/document viewer, then use its Download control
    # ------------------------------------------------------------
    preview = _find_clickable_media_preview(message_element)
    if not preview:
        logger.warning(
            "Could not find visible WhatsApp attachment preview for viewer fallback."
        )
        return None

    try:
        logger.info(
            "WhatsApp attachment: no message Download control; opening media viewer fallback."
        )
        try:
            preview.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass

        try:
            preview.click(timeout=5000)
        except Exception:
            preview.click(timeout=5000, force=True)

        # Give the viewer a moment to materialise its controls.  We deliberately
        # poll instead of sleeping for a fixed long interval.
        viewer_button = None
        for attempt in range(1, 11):
            try:
                viewer_button = _visible_download_control(page)
            except Exception:
                viewer_button = None
            if viewer_button:
                logger.info(
                    "WhatsApp attachment: viewer Download control found on attempt=%d",
                    attempt,
                )
                break
            page.wait_for_timeout(300)

        if not viewer_button:
            logger.warning(
                "Could not find visible WhatsApp attachment download control in viewer."
            )
            return None

        with page.expect_download(timeout=15000) as download_info:
            try:
                viewer_button.click(timeout=5000)
            except Exception:
                viewer_button.click(timeout=5000, force=True)

        return _save_playwright_download(download_info.value)

    except Exception as e:
        logger.warning(
            "Could not download WhatsApp attachment via viewer fallback: %s",
            e,
        )
        return None
    finally:
        # Escape closes the media viewer without changing the selected chat.
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
        except Exception:
            pass


def message_has_attachment(message_element):
    """Detect WhatsApp media/documents even when the download button is hidden.

    WhatsApp frequently renders the file card without a visible Download control
    until the message is hovered.  The scanner must therefore recognise the
    attachment from structural markers, filename text, and document/media nodes.
    """
    selectors = [
        '[data-testid="download"]',
        '[data-testid*="download"]',
        '[aria-label*="Download" i]',
        '[title*="Download" i]',
        '[data-testid*="media"]',
        '[data-testid*="document"]',
        '[data-testid*="attachment"]',
        '[data-testid*="file"]',
        '[data-icon*="document"]',
        '[data-icon*="media"]',
        'video',
        'audio',
        'img',
        'canvas',
        'a[href*="blob:"]',
        'a[href*="whatsapp"]',
    ]

    for selector in selectors:
        try:
            if message_element.locator(selector).count() > 0:
                return True
        except Exception:
            pass

    # Inspect visible text because document cards often expose only the filename
    # and file-type metadata before the hover/download control is materialised.
    try:
        raw = (message_element.inner_text(timeout=1000) or "").strip()
        lowered = raw.lower()
        attachment_markers = (
            "download", "document", "attachment", "file",
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".jpg", ".jpeg", ".png", ".webp", ".txt", ".zip",
            "mb", "kb"
        )
        if any(marker in lowered for marker in attachment_markers):
            return True
    except Exception:
        pass

    # A descendant with a button-like accessible name is another reliable clue
    # even when WhatsApp uses generated class names.
    try:
        buttons = message_element.locator('[role="button"], button, [aria-label], [title]')
        for i in range(min(buttons.count(), 40)):
            el = buttons.nth(i)
            aria = (el.get_attribute("aria-label") or "").lower()
            title = (el.get_attribute("title") or "").lower()
            text = (el.inner_text(timeout=300) or "").lower()
            probe = " ".join((aria, title, text))
            if any(token in probe for token in (
                "download", "open file", "document", "attachment", "media",
                "jpg", "jpeg", "png", "pdf", "docx", "xlsx"
            )):
                return True
    except Exception:
        pass

    return False


def process_message_attachments(
    whatsapp_account,
    page,
    phone,
    contact,
    message_info,
    message_element,
    message_hash,
):
    """Download/upload a WhatsApp attachment and report explicit success.

    Important invariant: when an attachment is detected, returning an empty
    attachment list is NOT success. The caller must distinguish:
      - no attachment present -> normal message processing may continue
      - attachment detected + successful Drive result -> continue
      - attachment detected + download/upload/link failure -> retry later and
        do not write/mark the WhatsApp message as processed
    """
    if not phone:
        logger.warning(
            "[%s] Cannot process attachment without phone.", whatsapp_account
        )
        return {"success": False, "attachment_detected": True, "attachments": []}

    phone = normalize_phone(phone) or clean_phone(phone)

    if not message_element:
        return {"success": True, "attachment_detected": False, "attachments": []}

    if not message_has_attachment(message_element):
        return {"success": True, "attachment_detected": False, "attachments": []}

    fullname = contact.get("fullname") if contact else None

    downloaded = download_whatsapp_attachment(page, message_element)

    if not downloaded:
        logger.warning(
            "[%s] Attachment detected for message_hash=%s but WhatsApp download failed; "
            "message remains retryable.",
            whatsapp_account, message_hash,
        )
        return {"success": False, "attachment_detected": True, "attachments": []}

    local_path = downloaded["path"]

    try:
        file_hash = calculate_file_hash(local_path)
        # The same binary can legitimately be sent by different clients.
        # Scope the persistent attachment key to the canonical WhatsApp phone
        # so one client's Drive file is never reused for another client.
        attachment_hash = f"{phone}:{file_hash}"

        existing = attachment_already_uploaded(
            whatsapp_account, attachment_hash
        )

        if existing:
            logger.info(
                "[%s] Attachment already uploaded: %s",
                whatsapp_account,
                downloaded["filename"],
            )

            # The completed Drive row may have been created under an older
            # message hash (for example after a DOM wrapper/hash changed).
            # Re-link it to this exact message before returning success so the
            # next poll's per-message completion check also succeeds.
            link_uploaded_attachment_to_message(
                whatsapp_account=whatsapp_account,
                attachment_hash=attachment_hash,
                message_hash=message_hash,
                phone=phone,
            )

            if not existing.get("drive_file_url"):
                logger.warning(
                    "[%s] Existing attachment record has no completed Drive URL; "
                    "message remains retryable.",
                    whatsapp_account,
                )
                return {"success": False, "attachment_detected": True, "attachments": []}

            return {
                "success": True,
                "attachment_detected": True,
                "attachments": [{
                    "filename": downloaded["filename"],
                    "drive_file_id": existing["drive_file_id"],
                    "drive_file_url": existing["drive_file_url"],
                    "duplicate": True,
                }],
            }

        upload = upload_whatsapp_attachment_to_drive(
            local_path, phone, fullname
        )

        save_uploaded_attachment(
            whatsapp_account=whatsapp_account,
            phone=phone,
            message_hash=message_hash,
            attachment_hash=attachment_hash,
            filename=upload["filename"],
            mime_type=upload["mime_type"],
            local_path=local_path,
            drive_file_id=upload["file_id"],
            drive_file_url=upload["file_url"],
        )

        logger.info(
            "[%s] Uploaded WhatsApp attachment for %s: %s -> %s",
            whatsapp_account,
            phone,
            upload["filename"],
            upload["file_url"],
        )

        if not upload.get("file_id") or not upload.get("file_url"):
            logger.warning(
                "[%s] Drive upload returned incomplete file state for %s; "
                "message remains retryable.",
                whatsapp_account, phone,
            )
            return {"success": False, "attachment_detected": True, "attachments": []}

        return {
            "success": True,
            "attachment_detected": True,
            "attachments": [{
                "filename": upload["filename"],
                "drive_file_id": upload["file_id"],
                "drive_file_url": upload["file_url"],
                "folder_url": upload["folder_url"],
                "folder_id": upload["folder_id"],
                "duplicate": False,
            }],
        }

    except Exception:
        logger.exception(
            "[%s] Failed processing WhatsApp attachment for %s.",
            whatsapp_account,
            phone,
        )

        raise

    finally:
        try:
            if os.path.exists(local_path):
                os.remove(local_path)

        except Exception:
            pass


# ============================================================
# DATABASE CONTACT LOOKUPS
# ============================================================


def lookup_contact_by_name(fullname):
    if not fullname:
        return None

    normalized_input = normalize_person_name(fullname)
    if not normalized_input:
        return None

    input_tokens = set(normalized_input.split())

    with DATABASE_LOCK:
        conn = get_db_connection()
        rows = conn.execute(
            """
            SELECT
                phone,
                whatsapp_phone,
                airtable_id,
                trello_card_id,
                fullname,
                contact_type
            FROM contacts
            WHERE contact_type = 'client'
            """
        ).fetchall()
        conn.close()

    best_match = None
    best_score = 0.0

    for row in rows:
        db_fullname = row[4]
        if not db_fullname:
            continue

        normalized_db = normalize_person_name(db_fullname)
        if not normalized_db:
            continue

        db_tokens = set(normalized_db.split())

        # 1. Exact match
        if normalized_db == normalized_input:
            return {
                "phone": row[0],
                "whatsapp_phone": row[1],
                "airtable_id": row[2],
                "trello_card_id": row[3],
                "fullname": row[4],
                "contact_type": row[5],
            }

        # 2. Substring match
        if (
            normalized_input in normalized_db
            or normalized_db in normalized_input
        ):
            return {
                "phone": row[0],
                "whatsapp_phone": row[1],
                "airtable_id": row[2],
                "trello_card_id": row[3],
                "fullname": row[4],
                "contact_type": row[5],
            }

        # 3. Token set match (handles omitted middle names/reordered names)
        common_tokens = input_tokens.intersection(db_tokens)
        if (
            len(common_tokens) >= 2
            and len(common_tokens) >= len(input_tokens) - 1
        ):
            return {
                "phone": row[0],
                "whatsapp_phone": row[1],
                "airtable_id": row[2],
                "trello_card_id": row[3],
                "fullname": row[4],
                "contact_type": row[5],
            }

        # 4. Fuzzy ratio fallback
        score = difflib.SequenceMatcher(
            None, normalized_input, normalized_db
        ).ratio()
        if score > best_score:
            best_score = score
            best_match = {
                "phone": row[0],
                "whatsapp_phone": row[1],
                "airtable_id": row[2],
                "trello_card_id": row[3],
                "fullname": row[4],
                "contact_type": row[5],
            }

    if best_score >= 0.75:
        return best_match

    return None


def lookup_client_by_phone_or_whatsapp(number):
    if not number:
        return None

    number = normalize_phone(number)
    if not number:
        return None

    with DATABASE_LOCK:
        conn = get_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT
                    phone,
                    whatsapp_phone,
                    airtable_id,
                    trello_card_id,
                    fullname,
                    contact_type
                FROM contacts
                WHERE contact_type = 'client'
                """
            ).fetchall()
        finally:
            conn.close()

    # Match after normalization so legacy CRM values such as 7599328827
    # resolve to WhatsApp's 447599328827 representation.
    matches = []
    for row in rows:
        if _normalized_contact_matches(row, number):
            matches.append(row)

    if len(matches) > 1:
        logger.error(
            "AMBIGUOUS WhatsApp phone %s: %d client records match. "
            "Refusing to guess a Trello card by name. Matches=%r",
            number,
            len(matches),
            [
                {
                    "fullname": r[4],
                    "phone": r[0],
                    "whatsapp_phone": r[1],
                    "trello_card_id": r[3],
                }
                for r in matches
            ],
        )
        return None

    if len(matches) == 1:
        row = matches[0]
        return {
            "phone": row[0],
            "whatsapp_phone": row[1],
            "airtable_id": row[2],
            "trello_card_id": row[3],
            "fullname": row[4],
            "contact_type": row[5],
        }

    return None


def save_contact(
    phone, whatsapp_phone, fullname, trello_card_id, contact_type="client"
):
    phone = clean_phone(phone)

    if not phone:
        return False

    # Normalize WhatsApp phone separately.
    if whatsapp_phone:
        try:
            whatsapp_phone = normalize_phone(whatsapp_phone)
        except Exception as e:
            logger.warning(
                "Could not normalize WhatsApp phone %r: %s", whatsapp_phone, e
            )
            whatsapp_phone = None

    with DATABASE_LOCK:
        conn = get_db_connection()

        conn.execute(
            """
            INSERT INTO contacts
            (
                phone,
                airtable_id,
                trello_card_id,
                fullname,
                contact_type,
                whatsapp_phone
            )
            VALUES (?, ?, ?, ?, ?, ?)

            ON CONFLICT(phone)
            DO UPDATE SET
                trello_card_id = excluded.trello_card_id,
                fullname = excluded.fullname,
                contact_type = excluded.contact_type,
                whatsapp_phone = COALESCE(
                    excluded.whatsapp_phone,
                    contacts.whatsapp_phone
                )
            """,
            (
                phone,
                None,
                trello_card_id,
                fullname,
                contact_type,
                whatsapp_phone,
            ),
        )

        conn.commit()
        conn.close()

    return True


def get_contact_by_phone(phone):
    if not phone:
        return None
    return lookup_client_by_phone_or_whatsapp(phone)


def update_contact_trello_card_id(phone, trello_card_id):
    phone = clean_phone(phone)
    if not phone:
        return

    with DATABASE_LOCK:
        conn = get_db_connection()
        conn.execute(
            """
            UPDATE contacts
            SET trello_card_id = ?
            WHERE phone = ? OR whatsapp_phone = ?
            """,
            (trello_card_id, phone, phone),
        )
        conn.commit()
        conn.close()


# ============================================================
# MESSAGE DATABASE
# ============================================================


def save_message(
    whatsapp_account,
    phone,
    message,
    direction="incoming",
    whatsapp_timestamp=None,
    identity_key=None,
    message_id=None,
):
    with DATABASE_LOCK:
        conn = get_db_connection()

        conn.execute(
            """
            INSERT INTO whatsapp_messages
            (
                whatsapp_account,
                phone,
                message,
                direction,
                whatsapp_timestamp,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                whatsapp_account,
                phone,
                message,
                direction,
                whatsapp_timestamp,
                datetime.now().isoformat(),
            ),
        )

        conn.commit()
        conn.close()

    save_message_identity(
        identity_key=identity_key,
        whatsapp_account=whatsapp_account,
        phone=phone,
        direction=direction,
        message_id=message_id,
        normalized_message=normalize_message_text(message),
    )


def already_processed(message_hash):
    with DATABASE_LOCK:
        conn = get_db_connection()

        row = conn.execute(
            """
            SELECT 1
            FROM processed_messages
            WHERE message_hash = ?
            """,
            (message_hash,),
        ).fetchone()

        conn.close()

    return row is not None


def clear_processed_marker(message_hash):
    """Remove a processed marker when an attachment upload is incomplete.

    A WhatsApp message may have reached Trello before a later attachment retry
    was completed. Such a message must remain retryable; otherwise the persistent
    processed marker prevents the attachment from ever being claimed again.
    """
    if not message_hash:
        return

    with DATABASE_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                "DELETE FROM processed_messages WHERE message_hash = ?",
                (message_hash,),
            )
            conn.commit()
        finally:
            conn.close()


def claim_message_for_processing(message_hash, whatsapp_account, stale_after_seconds=1800):
    """Atomically claim a message so concurrent pollers cannot post duplicate comments."""
    if not message_hash or not whatsapp_account:
        return False

    now = datetime.now()
    with DATABASE_LOCK:
        conn = get_db_connection()
        try:
            if conn.execute(
                "SELECT 1 FROM processed_messages WHERE message_hash = ? LIMIT 1",
                (message_hash,),
            ).fetchone():
                return False

            existing = conn.execute(
                "SELECT claimed_at FROM processing_messages WHERE message_hash = ? LIMIT 1",
                (message_hash,),
            ).fetchone()

            if existing:
                try:
                    age = (now - datetime.fromisoformat(existing[0])).total_seconds()
                except Exception:
                    age = 0
                if age < stale_after_seconds:
                    return False
                conn.execute(
                    "DELETE FROM processing_messages WHERE message_hash = ?",
                    (message_hash,),
                )

            conn.execute(
                "INSERT INTO processing_messages (message_hash, whatsapp_account, claimed_at) VALUES (?, ?, ?)",
                (message_hash, whatsapp_account, now.isoformat()),
            )
            conn.commit()
            return True
        finally:
            conn.close()


def release_message_processing(message_hash):
    """Release an in-flight message claim after success or failure."""
    if not message_hash:
        return
    with DATABASE_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                "DELETE FROM processing_messages WHERE message_hash = ?",
                (message_hash,),
            )
            conn.commit()
        finally:
            conn.close()


def mark_processed(message_hash, whatsapp_account):
    with DATABASE_LOCK:
        conn = get_db_connection()

        conn.execute(
            """
            INSERT OR IGNORE INTO
            processed_messages
            (
                message_hash,
                processed_at,
                whatsapp_account
            )
            VALUES (?, ?, ?)
            """,
            (message_hash, datetime.now().isoformat(), whatsapp_account),
        )

        conn.commit()
        conn.close()


def get_client_by_whatsapp_phone(whatsapp_phone):
    if not whatsapp_phone:
        return None

    try:
        whatsapp_phone = normalize_phone(whatsapp_phone)
    except Exception as e:
        logger.warning(
            "Could not normalize WhatsApp lookup number %r: %s",
            whatsapp_phone,
            e,
        )
        return None

    if not whatsapp_phone:
        return None

    with DATABASE_LOCK:
        conn = get_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT
                    phone,
                    whatsapp_phone,
                    airtable_id,
                    trello_card_id,
                    fullname,
                    contact_type
                FROM contacts
                WHERE contact_type = 'client'
                """
            ).fetchall()
        finally:
            conn.close()

    for row in rows:
        # Keep the legacy function semantics but normalize both sides, so a
        # Trello/CRM value like 7599328827 still matches WhatsApp 447599328827.
        if row[1] and normalize_phone(row[1]) == whatsapp_phone:
            return {
                "phone": row[0],
                "whatsapp_phone": row[1],
                "airtable_id": row[2],
                "trello_card_id": row[3],
                "fullname": row[4],
                "contact_type": row[5],
            }
        if row[0] and normalize_phone(row[0]) == whatsapp_phone:
            return {
                "phone": row[0],
                "whatsapp_phone": row[1],
                "airtable_id": row[2],
                "trello_card_id": row[3],
                "fullname": row[4],
                "contact_type": row[5],
            }

    return None


# ============================================================
# WHATSAPP CHAT/PHONE CACHE
# ============================================================


def get_cached_phone(whatsapp_account, chat_name):
    if not chat_name:
        return None

    with DATABASE_LOCK:
        conn = get_db_connection()

        row = conn.execute(
            """
            SELECT phone
            FROM whatsapp_chat_mapping
            WHERE whatsapp_account = ?
              AND chat_name = ?
            """,
            (whatsapp_account, chat_name),
        ).fetchone()

        conn.close()

    if not row:
        return None

    return clean_phone(row[0])


def save_chat_phone(whatsapp_account, chat_name, phone):
    if not chat_name or not phone:
        return

    phone = clean_phone(phone)

    if not phone:
        return

    with DATABASE_LOCK:
        conn = get_db_connection()

        conn.execute(
            """
            INSERT OR REPLACE INTO
            whatsapp_chat_mapping
            (
                whatsapp_account,
                chat_name,
                phone,
                last_seen
            )
            VALUES (?, ?, ?, ?)
            """,
            (whatsapp_account, chat_name, phone, datetime.now().isoformat()),
        )

        conn.commit()
        conn.close()


# ============================================================
# PHONE HELPERS
# ============================================================


def is_valid_uk_phone(phone):
    if not phone:
        return False

    digits = re.sub(r"\D", "", str(phone))

    return len(digits) == 12 and digits.startswith("447")


def clean_phone(phone):
    if not phone:
        return None

    digits = re.sub(r"\D", "", str(phone))

    if digits.startswith("00"):
        digits = digits[2:]

    if digits.startswith("0"):
        digits = "44" + digits[1:]

    if is_valid_uk_phone(digits):
        return digits

    return None


# ============================================================
# EXTRACT PHONE FROM TEXT
# ============================================================
def extract_phone_from_text(text):
    if not text:
        return None

    whatsapp_web_pattern = re.compile(
        r"web\.whatsapp\.com/send\?[^ \t\r\n]*?phone=" r"(\+?\d{8,15})",
        re.IGNORECASE,
    )

    wa_me_pattern = re.compile(
        r"(?:https?://)?wa\.me/" r"(\+?\d{8,15})", re.IGNORECASE
    )

    for pattern in (
        whatsapp_web_pattern,
        wa_me_pattern,
    ):
        matches = pattern.findall(text)

        for match in matches:
            phone = clean_phone(match)

            if phone and phone not in WORK_PHONE_NUMBERS:
                return phone

    patterns = [
        r"\+\d{1,3}(?:[ \t().-]*\d){7,14}",
        r"\b00\d{1,3}(?:[ \t().-]*\d){7,14}\b",
        r"\b\d{10,15}\b",
    ]

    for pattern in patterns:
        matches = re.findall(pattern, text)

        for match in matches:
            phone = clean_phone(match)

            if phone and phone not in WORK_PHONE_NUMBERS:
                return phone

    return None


# ============================================================
# EXTRACT WHATSAPP NUMBER FROM TRELLO CARD DESCRIPTION
# ============================================================


def extract_whatsapp_phone_from_trello_description(description):
    if not description:
        return None

    description = str(description)

    wa_me_match = re.search(
        r"https?://(?:www\.)?wa\.me/" r"(\+?\d+)", description, re.IGNORECASE
    )

    if wa_me_match:
        raw_phone = wa_me_match.group(1)

        try:
            normalized = normalize_phone(raw_phone)

            if normalized:
                logger.debug(
                    "[TRELLO] Extracted WhatsApp number from wa.me URL: %r ->"
                    " %r",
                    raw_phone,
                    normalized,
                )

                return normalized

        except Exception as e:
            logger.warning(
                "[TRELLO] Failed to normalize wa.me number %r: %s",
                raw_phone,
                e,
            )

    web_match = re.search(
        r"https?://web\.whatsapp\.com/send"
        r"\?[^ \n\r<>\"']*?"
        r"(?:^|[?&])phone="
        r"(\+?\d+)",
        description,
        re.IGNORECASE,
    )

    if web_match:
        raw_phone = web_match.group(1)

        try:
            normalized = normalize_phone(raw_phone)

            if normalized:
                logger.debug(
                    "[TRELLO] Extracted WhatsApp number from WhatsApp Web URL:"
                    " %r -> %r",
                    raw_phone,
                    normalized,
                )

                return normalized

        except Exception as e:
            logger.warning(
                "[TRELLO] Failed to normalize WhatsApp Web number %r: %s",
                raw_phone,
                e,
            )

    logger.warning(
        "[TRELLO] No WhatsApp URL/phone found in Trello card description."
    )

    return None


# ============================================================
# TRELLO API
# ============================================================


def trello_request(method, endpoint, params=None, timeout=30):
    if params is None:
        params = {}

    params = dict(params)

    params["key"] = TRELLO_API_KEY
    params["token"] = TRELLO_TOKEN

    url = "https://api.trello.com/1" + endpoint

    with TRELLO_LOCK:
        response = requests.request(
            method, url, params=params, timeout=timeout
        )

    if not response.ok:
        logger.error(
            "TRELLO API ERROR: %s %s -> %s %s",
            method,
            endpoint,
            response.status_code,
            response.text[:1000],
        )

        response.raise_for_status()

    if not response.text:
        return {}

    return response.json()


# ============================================================
# TRELLO CARD READING
# ============================================================


def get_trello_cards_from_list(list_id):
    logger.info("Reading Trello list: %s", list_id)

    return trello_request(
        "GET", f"/lists/{list_id}/cards", {"fields": "id,name,idList,closed,desc"}
    )


# ============================================================
# TRELLO CARD NAME PARSING
# ============================================================


def parse_client_card_name(card_name):
    if not card_name:
        return None

    pattern = re.compile(
        r"""
        Phone
        \s*:\s*
        (
            \+?
            \d
            [\d\s().-]{7,}
            \d
        )
        (?=
            \s*
            (?:
                [–—-]
                |
                Eviction\s+date
                |
                $
            )
        )
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    match = pattern.search(card_name)

    if match:
        phone = clean_phone(match.group(1))

        if not phone:
            return None

        fullname = card_name[: match.start()].strip()

        fullname = re.sub(r"\s*[–—-]\s*$", "", fullname).strip()

        if not fullname:
            return None

        return {"fullname": fullname, "phone": phone}

    phone = extract_phone_from_text(card_name)

    if not phone:
        return None

    phone_match = re.search(r"Phone\s*:", card_name, re.IGNORECASE)

    if phone_match:
        fullname = card_name[: phone_match.start()].strip()

    else:
        fullname = card_name

    fullname = re.sub(r"\s*[–—-]\s*$", "", fullname).strip()

    if not fullname:
        return None

    return {"fullname": fullname, "phone": phone}


# ============================================================
# EXTRACT WHATSAPP PHONE FROM DESCRIPTION
# ============================================================
def extract_whatsapp_phone_from_description(text):
    if not text:
        return None

    web_patterns = [
        r"web\.whatsapp\.com/send\?phone=([+]?\d[\d\s().-]{7,20})",
        r"web\.whatsapp\.com/send\?phone=([+]?\d{8,20})",
    ]

    for pattern in web_patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)

        for match in matches:
            phone = clean_phone(match)

            if phone and phone not in WORK_PHONE_NUMBERS:
                logger.debug("Extracted WhatsApp Web number %s", phone)

                return phone

    wa_me_patterns = [
        r"wa\.me/([+]?\d[\d\s().-]{7,20})",
        r"wa\.me/([+]?\d{8,20})",
    ]

    for pattern in wa_me_patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)

        for match in matches:
            phone = clean_phone(match)

            if phone and phone not in WORK_PHONE_NUMBERS:
                logger.debug("Extracted WhatsApp phone number %s", phone)

                return phone

    whatsapp_section_patterns = [
        r"WhatsApp\s+Phone.{0,300}?(\+?\d[\d\s().-]{8,20})",
        r"WhatsApp\s+Web.{0,300}?(\+?\d[\d\s().-]{8,20})",
    ]

    for pattern in whatsapp_section_patterns:
        matches = re.findall(
            pattern, text, flags=re.IGNORECASE | re.DOTALL
        )

        for match in matches:
            phone = clean_phone(match)

            if phone and phone not in WORK_PHONE_NUMBERS:
                logger.debug(
                    "Extracted WhatsApp number from description fallback: %s",
                    phone,
                )

                return phone

    logger.debug("No WhatsApp number found in Trello description.")

    return None


# ============================================================
# TRELLO CLIENT SYNCHRONISATION
# ============================================================


def sync_trello_clients():
    if not TRELLO_SYNC_LOCK.acquire(blocking=False):
        logger.info("Trello sync already running. Skipping.")

        return

    try:
        logger.info("SYNCING TRELLO CLIENTS - %s", TRELLO_BOARD_NAME)

        list_ids = get_trello_list_ids()

        total_cards = 0
        total_clients = 0
        duplicate_count = 0
        missing_phone_count = 0
        missing_whatsapp_count = 0

        seen_whatsapp_phones = set()

        for list_id in list_ids:
            try:
                cards = get_trello_cards_from_list(list_id)

            except Exception as e:
                logger.exception("Could not read list %s: %s", list_id, e)

                continue

            logger.info("List %s: %d cards", list_id, len(cards))

            total_cards += len(cards)

            for card in cards:
                if card.get("closed"):
                    continue

                card_id = card.get("id")

                card_name = card.get("name", "")

                parsed = parse_client_card_name(card_name)

                if not parsed:
                    logger.warning(
                        "CARD WITHOUT VALID PHONE IN NAME: %s", card_name
                    )

                    phone = None
                    fullname = card_name.strip()

                else:
                    phone = parsed.get("phone")

                    fullname = parsed.get("fullname", card_name.strip())

                    if phone:
                        try:
                            phone = normalize_phone(phone)

                        except Exception as e:
                            logger.warning(
                                "Could not normalize normal phone %r for card"
                                " %s: %s",
                                phone,
                                card_name,
                                e,
                            )

                            phone = None

                card_description = card.get("desc", "")

                logger.warning(
                    "[TRELLO DEBUG] card=%s name=%r desc=%r",
                    card_id,
                    card_name,
                    card_description,
                )

                whatsapp_phone = extract_whatsapp_phone_from_trello_description(
                    card_description
                )

                if whatsapp_phone:
                    try:
                        whatsapp_phone = normalize_phone(whatsapp_phone)

                    except Exception as e:
                        logger.warning(
                            "Could not normalize WhatsApp phone %r for card"
                            " %s: %s",
                            whatsapp_phone,
                            card_name,
                            e,
                        )

                        whatsapp_phone = None

                if not phone:
                    missing_phone_count += 1

                    logger.warning(
                        "SKIPPING CARD WITHOUT VALID NORMAL PHONE: %s |"
                        " card_id=%s",
                        card_name,
                        card_id,
                    )

                    continue

                if not whatsapp_phone:
                    missing_whatsapp_count += 1

                    logger.warning(
                        "CARD WITHOUT VALID WHATSAPP NUMBER: %s | card_id=%s |"
                        " phone=%s",
                        card_name,
                        card_id,
                        phone,
                    )

                if whatsapp_phone:
                    if whatsapp_phone in seen_whatsapp_phones:
                        duplicate_count += 1

                        logger.warning(
                            "Duplicate WhatsApp number %s: multiple Trello cards"
                            " use this number. Latest matching card will be"
                            " retained: %s",
                            whatsapp_phone,
                            card_name,
                        )

                    seen_whatsapp_phones.add(whatsapp_phone)

                logger.info(
                    "Trello client sync: name=%r | phone=%r |"
                    " whatsapp_phone=%r | card=%s",
                    fullname,
                    phone,
                    whatsapp_phone,
                    card_id,
                )

                saved = save_contact(
                    phone=phone,
                    whatsapp_phone=whatsapp_phone,
                    fullname=fullname,
                    trello_card_id=card_id,
                    contact_type="client",
                )

                if saved:
                    total_clients += 1

                else:
                    logger.warning(
                        "Could not save Trello client: phone=%s |"
                        " whatsapp_phone=%s | card=%s",
                        phone,
                        whatsapp_phone,
                        card_id,
                    )

        logger.info("Trello cards examined: %d", total_cards)

        logger.info("Client cards synchronised: %d", total_clients)

        logger.info("Duplicate WhatsApp numbers: %d", duplicate_count)

        logger.info("Cards without normal phone: %d", missing_phone_count)

        logger.info(
            "Cards without WhatsApp number: %d", missing_whatsapp_count
        )

    finally:
        TRELLO_SYNC_LOCK.release()


# ============================================================
# TRELLO LABEL SUPPORT
# ============================================================


def get_board_labels(board_id):
    return trello_request(
        "GET",
        f"/boards/{board_id}/labels",
        {"limit": 100, "fields": "id,name,color,idBoard"},
    )


def find_or_create_new_message_label(card_id):
    logger.info(
        "[TRELLO] Looking for '%s' label...", TRELLO_NEW_MESSAGE_LABEL_NAME
    )

    card = get_trello_card(card_id)

    board_id = card.get("idBoard")

    if not board_id:
        raise RuntimeError(
            f"Could not determine board ID for Trello card {card_id}."
        )

    for label in card.get("labels", []):
        label_name = (label.get("name") or "").strip()

        if label_name.lower() == TRELLO_NEW_MESSAGE_LABEL_NAME.lower():
            return label.get("id")

    labels = get_board_labels(board_id)

    for label in labels:
        label_name = (label.get("name") or "").strip()

        if label_name.lower() == TRELLO_NEW_MESSAGE_LABEL_NAME.lower():
            return label.get("id")

    label = trello_request(
        "POST",
        f"/boards/{board_id}/labels",
        {
            "name": TRELLO_NEW_MESSAGE_LABEL_NAME,
            "color": TRELLO_NEW_MESSAGE_LABEL_COLOR,
        },
    )

    label_id = label.get("id")

    if not label_id:
        raise RuntimeError(
            "Trello created the label but did not return a label ID."
        )

    return label_id


def add_new_message_label_to_card(card_id):
    label_id = find_or_create_new_message_label(card_id)

    if not label_id:
        raise RuntimeError("Could not obtain Trello New Message label ID.")

    card = get_trello_card(card_id)

    existing_label_ids = set(card.get("idLabels", []))

    if label_id in existing_label_ids:
        return

    trello_request(
        "POST", f"/cards/{card_id}/idLabels", {"value": label_id}
    )


# ============================================================
# TRELLO COMMENTS
# ============================================================


def _strip_trailing_whatsapp_word(value):
    text = str(value or "").strip()
    if text.lower().endswith(" whatsapp"):
        text = text[:-9].rstrip()
    return text or "WhatsApp"


def trello_comment_already_exists(card_id, comment_text, limit=100):
    """Return True when an equivalent WhatsApp comment is already on the card.

    Exact comparison remains the primary check.  A second normalized comparison
    prevents legacy/cross-phone duplicates where the only difference is whitespace
    or an attachment section generated by an earlier retry.
    """
    if not card_id or not comment_text:
        return False

    def normalize_comment(value):
        return re.sub(r"\s+", " ", str(value or "").strip())

    def whatsapp_base(value):
        text = str(value or "").strip()
        # Remove the generated attachment block from the comparison fingerprint.
        text = re.sub(r"\n+📎 Attachments:.*$", "", text, flags=re.S)

        lines = text.split("\n\n", 1)
        header = lines[0].strip() if lines else ""
        body = lines[1].strip() if len(lines) > 1 else ""

        # Current format:
        # **Spring3 (From: 447393698631) — 09/09/2026 13:34**
        match = re.match(
            r"^\*\*(.*?) \((From|To):\s*([^)]+)\)\s*(?:—\s*[^*]+)?\*\*$",
            header,
            flags=re.I,
        )
        if match:
            account, role, peer = match.groups()
            return normalize_comment(
                f"WhatsApp|{account}|{role.lower()}|{peer}|{body}"
            )

        # Legacy format:
        # WhatsApp message (Spring3 WhatsApp) FROM 447393698631:
        match = re.match(
            r"^WhatsApp message \((.*?)\)\s+(FROM|TO)\s+([^:]+):$",
            header,
            flags=re.I,
        )
        if match:
            account, role, peer = match.groups()
            account = _strip_trailing_whatsapp_word(account)
            return normalize_comment(
                f"WhatsApp|{account}|{role.lower()}|{peer.strip()}|{body}"
            )

        # Legacy format without a phone.
        match = re.match(
            r"^WhatsApp message \((.*?)\):$",
            header,
            flags=re.I,
        )
        if match:
            account = _strip_trailing_whatsapp_word(match.group(1))
            return normalize_comment(
                f"WhatsApp|{account}|unknown||{body}"
            )

        return normalize_comment(text)

    target_exact = str(comment_text).strip()
    target_norm = normalize_comment(comment_text)
    target_base = whatsapp_base(comment_text)

    try:
        actions = trello_request(
            "GET",
            f"/cards/{card_id}/actions",
            {
                "filter": "commentCard",
                "limit": str(limit),
                "fields": "data,date,type",
            },
        )
        for action in actions or []:
            data = action.get("data") or {}
            text = (data.get("text") or "").strip()
            if text == target_exact:
                return True
            if normalize_comment(text) == target_norm:
                return True
            if target_base and whatsapp_base(text) == target_base:
                logger.info(
                    "[TRELLO] Equivalent WhatsApp comment already exists on card %s; skipping duplicate POST.",
                    card_id,
                )
                return True
    except Exception as exc:
        logger.warning(
            "Could not check Trello card %s for duplicate comment: %s",
            card_id,
            exc,
        )

    return False


AIRTABLE_COMMENT_MARKER = "📌**SU AIRTABLE Record and Drive Docs**"


def promote_airtable_comment_to_top(card_id):
    """Keep the Airtable sync comment as the newest/top Trello comment.

    Trello does not expose a comment-reordering API. The reliable way to move
    the Airtable-owned comment back to the top is to delete and recreate that
    same comment after a WhatsApp comment has been posted.
    """
    try:
        actions = trello_request(
            "GET",
            f"/cards/{card_id}/actions",
            {
                "filter": "commentCard",
                "limit": "1000",
                "fields": "data,date,type",
            },
        ) or []

        airtable_action = None
        for action in actions:
            text = ((action.get("data") or {}).get("text") or "").strip()
            if AIRTABLE_COMMENT_MARKER in text:
                airtable_action = action
                break

        if not airtable_action:
            return False

        action_id = airtable_action.get("id")
        comment_text = ((airtable_action.get("data") or {}).get("text") or "").strip()
        if not action_id or not comment_text:
            return False

        # /actions returns newest first. If the Airtable comment is already
        # the first comment action, it is already at the top and nothing needs
        # to be changed.
        first_action = actions[0] if actions else None
        if first_action and first_action.get("id") == action_id:
            return False

        trello_request("DELETE", f"/actions/{action_id}")
        trello_request(
            "POST",
            f"/cards/{card_id}/actions/comments",
            {"text": comment_text},
        )

        logger.info(
            "[TRELLO] Promoted Airtable sync comment to top of card %s after WhatsApp comment.",
            card_id,
        )
        return True

    except Exception as exc:
        logger.warning(
            "[TRELLO] Could not promote Airtable sync comment to top for card %s: %s",
            card_id,
            exc,
        )
        return False


def _short_whatsapp_account_name(account_name):
    """Return the concise account title used in Trello WhatsApp headers."""
    return _strip_trailing_whatsapp_word(account_name)


def _format_whatsapp_trello_timestamp(timestamp):
    """Return WhatsApp's timestamp as a compact, human-readable date/time."""
    text = str(timestamp or "").strip()
    if not text:
        return ""

    match = re.search(
        r"(\d{1,2}:\d{2}),\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        text,
    )
    if match:
        time_part = match.group(1).zfill(5)
        date_part = match.group(2)
        return f"{date_part} {time_part}"

    match = re.search(r"(\d{1,2}:\d{2})", text)
    if match:
        return match.group(1).zfill(5)

    return text.strip("[] ")


def _format_whatsapp_trello_header(
    whatsapp_account, phone=None, direction="incoming", timestamp=None
):
    """Build the bold Trello header for one WhatsApp message."""
    account_name = WHATSAPP_ACCOUNTS.get(whatsapp_account, {}).get(
        "name", whatsapp_account
    )
    short_name = _short_whatsapp_account_name(account_name)
    role = "To" if direction == "outgoing" else "From"

    if phone:
        header = f"{short_name} ({role}: {phone})"
    else:
        header = short_name

    timestamp_display = _format_whatsapp_trello_timestamp(timestamp)
    if timestamp_display:
        header += f" — {timestamp_display}"

    return f"**{header}**"


def add_trello_comment(
    card_id,
    message,
    phone=None,
    whatsapp_account=None,
    direction="incoming",
    timestamp=None,
):
    header = _format_whatsapp_trello_header(
        whatsapp_account=whatsapp_account,
        phone=phone,
        direction=direction,
        timestamp=timestamp,
    )
    comment = f"{header}\n\n{message}"

    if trello_comment_already_exists(card_id, comment):
        logger.info(
            "[TRELLO] Duplicate comment already exists on card %s; "
            "skipping POST.",
            card_id,
        )
        return {"id": "existing", "duplicate": True}

    result = trello_request(
        "POST", f"/cards/{card_id}/actions/comments", {"text": comment}
    )

    # Trello shows comment actions newest-first. Recreate the Airtable-owned
    # comment immediately after each real WhatsApp POST so the Airtable block
    # remains the first/top comment, with WhatsApp messages underneath it.
    promote_airtable_comment_to_top(card_id)

    if direction == "incoming":
        add_new_message_label_to_card(card_id)

    return result


def update_client_trello_card_from_whatsapp(
    whatsapp_account,
    contact,
    message,
    direction="incoming",
    attachments=None,
    timestamp=None,
):
    """Posts a WhatsApp message (and optional attachment details) to the client's Trello card."""
    card_id = contact.get("trello_card_id")
    if not card_id:
        logger.warning(
            "[%s] Cannot update Trello card: missing card_id for contact %s",
            whatsapp_account,
            contact,
        )
        return False

    phone = canonical_whatsapp_phone(
        contact, contact.get("whatsapp_phone") or contact.get("phone")
    )

    formatted_message = message or ""

    if attachments:
        attachment_lines = ["\n\n📎 Attachments:"]
        for att in attachments:
            name = att.get("filename", "Attachment")
            url = att.get("drive_file_url")
            if url:
                attachment_lines.append(f"- [{name}]({url})")
            else:
                attachment_lines.append(f"- {name}")
        formatted_message += "\n".join(attachment_lines)

    try:
        add_trello_comment(
            card_id=card_id,
            message=formatted_message,
            phone=phone,
            whatsapp_account=whatsapp_account,
            direction=direction,
            timestamp=timestamp,
        )
        # The 'New Message' label is intentionally NOT removed here.
        # It remains until the human opens/reads the Trello card.
        return True
    except Exception as e:
        logger.error(
            "[%s] Failed to post comment to Trello card %s: %s",
            whatsapp_account,
            card_id,
            e,
        )
        return False


# ============================================================
# MESSAGE HASH
# ============================================================


def normalize_message_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_message_timestamp(timestamp):
    """Return only the stable date/time payload from WhatsApp pre-plain text."""
    text = str(timestamp or "").strip()
    if not text:
        return ""
    match = re.search(r"(\d{1,2}:\d{2},\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", text)
    if match:
        return re.sub(r"\s+", "", match.group(1))
    match = re.search(r"(\d{1,2}:\d{2})", text)
    return match.group(1) if match else ""


def extract_message_id(message_element):
    """Extract the strongest WhatsApp DOM identity available for one message."""
    if not message_element:
        return None
    try:
        mid = message_element.get_attribute("data-id")
        if mid:
            return mid
    except Exception:
        pass
    try:
        nested = message_element.locator("[data-id]").evaluate_all(
            "els => els.map(el => el.getAttribute('data-id')).filter(Boolean).slice(0, 8)"
        )
        if nested:
            return nested[0]
    except Exception:
        pass
    return None


def extract_attachment_identity(message_element):
    """Return a lightweight DOM fingerprint for media-only messages.

    This deliberately avoids binary/image hashing during the scan. The actual
    downloaded file hash remains the authoritative Drive duplicate key.
    """
    if not message_element:
        return ""
    try:
        value = message_element.evaluate(r"""el => {
            const nodes = [el, ...el.querySelectorAll('*')];
            const vals = [];
            for (const n of nodes) {
                const attrs = ['data-id','data-testid','aria-label','title','alt','download'];
                for (const a of attrs) {
                    const v = n.getAttribute && n.getAttribute(a);
                    if (v) vals.push(a + '=' + v);
                }
                // Do not include blob/download hrefs: WhatsApp can regenerate
                // those URLs between DOM scans even for the same logical message.
            }
            return [...new Set(vals)].slice(0, 40).join('|');
        }""")
        return normalize_message_text(value)[:2000]
    except Exception:
        return ""


def build_message_identity(
    whatsapp_account, phone, message, direction="incoming",
    message_id=None, timestamp=None, attachment_identity=None
):
    """Build a stable logical identity that survives DOM timestamp variation.

    Priority:
      1. WhatsApp data-id when available.
      2. Lightweight media DOM identity for attachment-only wrappers.
      3. Content identity (phone + direction + normalized text + optional stable date).

    The fallback intentionally does NOT require a timestamp because WhatsApp can
    expose the same message first without data-pre-plain-text and later with it.
    """
    account = str(whatsapp_account or "").strip()
    phone = str(phone or "").strip()
    direction = str(direction or "incoming").strip().lower()
    mid = str(message_id or "").strip()
    if mid:
        raw = f"id|{account}|{phone}|{mid}"
    else:
        text = normalize_message_text(message)
        media = normalize_message_text(attachment_identity)
        # Do NOT include the timestamp in the fallback identity. A message can
        # be observed once with no data-pre-plain-text timestamp and later with
        # the full `[HH:MM, DD/MM/YYYY] sender:` value. Including the timestamp
        # would recreate the exact identity drift V22 is designed to eliminate.
        raw = f"fp|{account}|{phone}|{direction}|{text}|{media}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_hash(
    whatsapp_account, phone, message, timestamp=None, extra=None,
    message_id=None, attachment_identity=None, message_key=None
):
    """Backward-compatible hash builder backed by stable message identity."""
    direction = str(extra or "incoming").strip().lower() or "incoming"
    identity = message_key or build_message_identity(
        whatsapp_account=whatsapp_account,
        phone=phone,
        message=message,
        direction=direction,
        message_id=message_id,
        timestamp=timestamp,
        attachment_identity=attachment_identity,
    )
    return hashlib.sha256(
        f"stable-v22|{identity}".encode("utf-8")
    ).hexdigest()


def save_message_identity(
    identity_key, whatsapp_account, phone, direction, message_id, normalized_message
):
    if not identity_key:
        return
    with DATABASE_LOCK:
        conn = get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO whatsapp_message_identities
                (identity_key, whatsapp_account, phone, direction, message_id, normalized_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity_key) DO UPDATE SET
                    message_id = COALESCE(excluded.message_id, whatsapp_message_identities.message_id),
                    normalized_message = excluded.normalized_message
                """,
                (
                    identity_key, whatsapp_account, phone, direction, message_id,
                    normalized_message, datetime.now().isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def message_identity_exists(identity_key):
    if not identity_key:
        return False
    with DATABASE_LOCK:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM whatsapp_message_identities WHERE identity_key = ? LIMIT 1",
                (identity_key,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()


def legacy_message_content_exists(
    whatsapp_account, phone, message, direction, timestamp=None
):
    """Compatibility lookup for messages successfully stored before V22.

    It ignores DOM-only timestamp formatting differences but uses direction and
    canonical phone. This is a fallback only; V22 identity keys are preferred.
    """
    normalized = normalize_message_text(message)
    if not normalized:
        return False
    with DATABASE_LOCK:
        conn = get_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT message, direction, whatsapp_timestamp
                FROM whatsapp_messages
                WHERE whatsapp_account = ? AND phone = ? AND direction = ?
                ORDER BY id DESC LIMIT 100
                """,
                (whatsapp_account, phone, direction),
            ).fetchall()
        finally:
            conn.close()
    target_ts = normalize_message_timestamp(timestamp)
    for row in rows or []:
        if normalize_message_text(row[0]) != normalized:
            continue
        row_ts = normalize_message_timestamp(row[2])
        if target_ts and row_ts and target_ts != row_ts:
            continue
        return True
    return False


# ============================================================
# WHATSAPP HELPERS
# ============================================================


# def get_conversation_header(page):
#     selectors = [
#         '[data-testid="conversation-panel-header"]',
#         '[data-testid="conversation-header"]',
#         "#main header",
#     ]

#     for selector in selectors:
#         try:
#             locator = page.locator(selector)
#             count = locator.count()

#             logger.info(
#                 "[WHATSAPP HEADER DEBUG] selector=%r count=%d", selector, count
#             )

#             for i in range(count):
#                 candidate = locator.nth(i)

#                 try:
#                     if not candidate.is_visible():
#                         continue

#                     text = candidate.inner_text(timeout=2000).strip()

#                     logger.info(
#                         "[WHATSAPP HEADER DEBUG] selector=%r index=%d text=%r",
#                         selector,
#                         i,
#                         text,
#                     )

#                     if text:
#                         return candidate

#                 except Exception as e:
#                     logger.debug(
#                         "[WHATSAPP HEADER DEBUG] selector=%r index=%d failed:"
#                         " %s",
#                         selector,
#                         i,
#                         e,
#                     )

#         except Exception as e:
#             logger.debug(
#                 "[WHATSAPP HEADER DEBUG] selector=%r failed: %s", selector, e
#             )

#     logger.warning(
#         "[WHATSAPP HEADER DEBUG] Could not find a visible conversation header."
#     )

#     return None

def get_conversation_header(page):
    """
    Return the header for the currently open WhatsApp conversation.

    Reuse the broader open-chat detector because WhatsApp Web's
    DOM does not consistently expose #main or conversation-header.
    """

    try:
        header = get_open_chat_header(page)

        if header is not None:
            if isinstance(header, dict):
                text = (header.get('chat_name') or '').strip()
            else:
                try:
                    text = header.inner_text(timeout=2000).strip()
                except Exception:
                    text = ""

            logger.info(
                "[WHATSAPP HEADER DEBUG] "
                "get_open_chat_header() found header text=%r",
                text
            )

            if text:
                return header

    except Exception as e:
        logger.warning(
            "[WHATSAPP HEADER DEBUG] "
            "get_open_chat_header() failed: %s",
            e
        )

    logger.debug(
        "[WHATSAPP HEADER DEBUG] "
        "No usable open-chat header found."
    )

    return None


def get_chat_title(page):
    try:
        header = get_conversation_header(page)

        if not header or header.count() == 0:
            return None

        text = header.first.inner_text(timeout=2000)

        if not text:
            return None

        lines = [line.strip() for line in text.split("\n") if line.strip()]

        return lines[0] if lines else None

    except Exception as e:
        logger.warning("Could not get chat title: %s", e)

        return None





# ============================================================
# MONITOR OPEN CHAT FOR NEW OUTGOING MESSAGES
# ============================================================


def extract_message_direction(page, element_locator) -> str | None:
    """
    Determines message direction ('in' or 'out') by scanning element classes,
    data-id attributes, and descendant/ancestor structures.
    """
    try:
        if not element_locator:
            return None

        return page.evaluate("""(el) => {
            if (!el) return null;

            // 1. Walk up to find the message wrapper or row container
            const container = el.closest('div.message-in, div.message-out, div[data-id], div[role="row"]');
            if (!container) return null;

            // Helper to determine direction from a class string or data-id
            const getDirFromNode = (node) => {
                if (!node) return null;
                const cls = node.className || '';
                if (typeof cls === 'string') {
                    if (cls.includes('message-in')) return 'in';
                    if (cls.includes('message-out')) return 'out';
                }
                const dataId = node.getAttribute('data-id') || '';
                if (dataId.startsWith('false_')) return 'in';  // Incoming message
                if (dataId.startsWith('true_')) return 'out';  // Outgoing message
                return null;
            };

            // 2. Direct check on the found container
            let dir = getDirFromNode(container);
            if (dir) return dir;

            // 3. Check internal descendants if container is a generic role="row"
            const innerMsg = container.querySelector('div.message-in, div.message-out, div[data-id]');
            if (innerMsg) {
                dir = getDirFromNode(innerMsg);
                if (dir) return dir;
            }

            // 4. Fallback check for incoming/outgoing indicator classes on child elements
            if (container.querySelector('.message-in, [data-is-outgoing="false"]')) return 'in';
            if (container.querySelector('.message-out, [data-is-outgoing="true"]')) return 'out';

            return null;
        }""", element_locator.element_handle())

    except Exception as e:
        logger.warning("Error resolving message direction: %s", e)
        return None

def monitor_open_chat_for_outgoing(whatsapp_account, page, phone):
    if not phone:
        return {"processed": 0, "duplicates": 0, "failures": 0}

    messages = get_new_messages(
        whatsapp_account=whatsapp_account, phone=phone, page=page
    )

    if not messages:
        return {"processed": 0, "duplicates": 0, "failures": 0}

    processed = 0
    duplicates = 0
    failures = 0

    newly_seen_hashes = set()

    for message_info in messages:
        # FIX: Ensure we only process outgoing ('out') messages in this handler
        if message_info.get("direction") not in ("out", "outgoing"):
            continue

        message_hash = message_info.get("message_hash")

        try:
            result = process_message(
                whatsapp_account=whatsapp_account,
                page=page,
                phone=phone,
                message_info=message_info,
            )

            if result.get("processed"):
                if message_hash:
                    newly_seen_hashes.add(message_hash)

                if result.get("duplicate"):
                    duplicates += 1
                    logger.debug("[%s] Outgoing message already processed for %s.", whatsapp_account, phone)
                else:
                    processed += 1
                    logger.info("[%s] New outgoing WhatsApp message added to Trello for %s.", whatsapp_account, phone)
            else:
                failures += 1
                logger.warning("[%s] Could not process outgoing message for %s.", whatsapp_account, phone)

        except Exception as e:
            failures += 1
            logger.exception("[%s] Failed to process outgoing message for %s: %s", whatsapp_account, phone, e)

    if newly_seen_hashes:
        update_seen_outgoing_hashes(
            whatsapp_account, phone, newly_seen_hashes
        )

    return {
        "processed": processed,
        "duplicates": duplicates,
        "failures": failures,
    }


# ============================================================
# WHATSAPP CHAT NAME CLEANING
# ============================================================


def clean_whatsapp_chat_name(text):
    if not text:
        return None

    # WhatsApp may concatenate the unread marker directly with the contact
    # name, e.g. ``UnreadDANIAL Miraki``. Strip spaced and glued forms.
    cleaned_text = re.sub(
        r"^\s*(?:\d+\s*)?unread\s*messages?\s*",
        "",
        str(text),
        flags=re.IGNORECASE,
    )
    cleaned_text = re.sub(
        r"^\s*unread(?=[A-Za-z0-9+])\s*",
        "",
        cleaned_text,
        flags=re.IGNORECASE,
    )

    lines = [
        re.sub(r"\s+", " ", line.strip())
        for line in cleaned_text.split("\n")
        if line.strip()
    ]

    for line in lines:
        if re.fullmatch(r"\d+\s+unread\s+messages?", line, re.IGNORECASE):
            continue

        if re.fullmatch(r"\d{1,2}:\d{2}", line):
            continue

        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", line):
            continue

        if line.lower() in ("today", "yesterday"):
            continue

        if line.isdigit():
            continue

        return line

    return None


# ============================================================
# UNREAD CHAT DISCOVERY & PROCESSING
# ============================================================


def hydrate_unread_virtual_list(page):
    """Hydrates virtual scroll items in WhatsApp side pane."""
    try:
        pane = page.locator('#pane-side')
        if pane.count() > 0:
            pane.first.wait_for(state="visible", timeout=3000)
            pane.evaluate('el => el.scrollBy(0, 400)')
            page.wait_for_timeout(150)
    except Exception as e:
        logger.debug("Virtual hydration note: %s", e)


SYSTEM_CHAT_LABELS = {
    "unread", "all", "groups", "favorites", "chats",
    "community", "archived", "status", "channels"
}

# These are controls/status rows produced by WhatsApp's dedicated Unread filter,
# not conversations. They must never enter recovery or phone/header resolution.
UNREAD_FILTER_CONTROL_LABELS = {
    "no unread chats",
    "you're all caught up",
    "view all chats",
    "view all",
}

def _is_unread_filter_control(title, raw_text):
    def norm(v):
        return re.sub(r"\s+", " ", (v or "")).strip().lower()
    t = norm(title)
    r = norm(raw_text)
    if t in UNREAD_FILTER_CONTROL_LABELS:
        return True
    if "view all chats" in t or "no unread chats" in t:
        return True
    if r in UNREAD_FILTER_CONTROL_LABELS:
        return True
    if "no unread chats" in r and "caught up" in r:
        return True
    if r.endswith("view all chats") and ("unread" in r or "caught up" in r):
        return True
    return False

def _discover_unread_via_filter(page):
    """
    V17 fallback for manual Mark as unread chats.

    WhatsApp can render a manually-unread row without an explicit unread
    class, aria-label, bold style, or numeric badge. The dedicated Unread
    filter is therefore used as the UI-level recovery signal.
    """
    try:
        unread_tab = page.locator(
            'button[aria-selected="false"]:has-text("Unread"), '
            'button[title*="Unread" i]:not([aria-selected="true"]), '
            '[role="tab"][aria-selected="false"]:has-text("Unread")'
        ).first
        if unread_tab.count() == 0 or not unread_tab.is_visible():
            logger.info("UNREAD FILTER FALLBACK: no visible Unread tab")
            return []
        logger.info("UNREAD FILTER FALLBACK: activating Unread filter")
        unread_tab.click(timeout=2500)
        # WhatsApp can update the filtered list asynchronously, and the row may
        # not retain data-testid=list-item-* while the filter is active. Wait for
        # the filter state to settle, then inspect several row shapes.
        page.wait_for_timeout(900)
        try:
            page.wait_for_function(r'''() => {
                const active = document.querySelector(
                    'button[aria-selected="true"], [role="tab"][aria-selected="true"]'
                );
                const t = ((active && (active.innerText || active.textContent)) || '').toLowerCase();
                return t.includes('unread') || t.includes('all');
            }''', timeout=1200)
        except Exception:
            pass
        data = page.evaluate(r'''() => {
            const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                return r.width > 40 && r.height > 24 && r.right > 0 && r.left < innerWidth &&
                       r.bottom > 0 && r.top < innerHeight && s.display !== 'none' &&
                       s.visibility !== 'hidden' && s.opacity !== '0';
            };
            const seen = new Set();
            const rows = [];
            const selectors = [
                '#pane-side [data-testid^="list-item-"]',
                '#pane-side [role="listitem"]',
                '#pane-side [role="row"]',
                '#pane-side [role="gridcell"]'
            ];
            for (const selector of selectors) {
                for (const row of Array.from(document.querySelectorAll(selector))) {
                    if (!visible(row) || seen.has(row)) continue;
                    seen.add(row); rows.push(row);
                }
            }
            if (!rows.length) {
                const pane = document.querySelector('#pane-side');
                if (pane) {
                    const pr = pane.getBoundingClientRect();
                    const candidates = Array.from(pane.querySelectorAll('div,li')).map(el => ({el, r: el.getBoundingClientRect()}))
                        .filter(({el,r}) => {
                            if (!visible(el)) return false;
                            const txt = (el.innerText || el.textContent || '').replace(/\s+/g,' ').trim();
                            if (txt.length < 2 || txt.length > 500) return false;
                            return r.left >= pr.left - 2 && r.right <= pr.right + 2 &&
                                   r.height >= 36 && r.height <= 120 && r.width >= 180 &&
                                   r.width <= pr.width + 2;
                        });
                    candidates.sort((a,b) => a.r.top - b.r.top || b.r.width - a.r.width);
                    for (const c of candidates) {
                        const duplicateBand = rows.some(existing => {
                            const er = existing.getBoundingClientRect();
                            return Math.abs(er.top - c.r.top) < 5 && Math.abs(er.left - c.r.left) < 8;
                        });
                        if (duplicateBand) continue;
                        const contained = candidates.some(other => other.el !== c.el &&
                            other.r.top <= c.r.top + 2 && other.r.bottom >= c.r.bottom - 2 &&
                            other.r.width > c.r.width + 15);
                        if (contained) continue;
                        seen.add(c.el); rows.push(c.el);
                    }
                }
            }
            rows.sort((a,b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
            return rows.map((row, idx) => {
                const titleNode = row.querySelector('[data-testid="cell-frame-title"], [data-testid="cell-frame-title-name"], span[title], span[dir="auto"]');
                const rawText = (row.innerText || row.textContent || '').replace(/\s+/g,' ').trim();
                let title = titleNode ? (titleNode.getAttribute('title') || titleNode.textContent || '').trim() : '';
                if (!title) title = ((row.innerText || '').split(/\n+/).map(x => x.trim()).filter(Boolean)[0] || '');
                return {index:idx,title,rawText,unread_count:1,rowTestid:row.getAttribute('data-testid'),rowRole:row.getAttribute('role'),evidence:['filter-unread'],visual:{boldTitle:false,boldPreview:false,semanticHits:0,numericBadge:false}};
            }).filter(item => {
                const t = (item.title || '').replace(/\s+/g,' ').trim().toLowerCase();
                const r = (item.rawText || '').replace(/\s+/g,' ').trim().toLowerCase();
                return !(t === 'no unread chats' || t === 'view all chats' || t === 'view all' ||
                         t === "you're all caught up" ||
                         r === 'view all chats' || r === "you're all caught up" ||
                         (r.includes('no unread chats') && r.includes("you're all caught up")));
            });
        }''');
        data = [
            item for item in (data or [])
            if not _is_unread_filter_control(item.get("title"), item.get("rawText"))
        ]
        logger.info("UNREAD FILTER FALLBACK: captured %d genuine row(s): %r", len(data or []), (data or [])[:15])
        if not data:
            logger.info("UNREAD FILTER FALLBACK: filter contained only controls/status rows or no conversations")
        try:
            state_diag = page.evaluate(r'''() => {
                const pane = document.querySelector('#pane-side');
                return {
                    activeTabs: Array.from(document.querySelectorAll('button[aria-selected="true"], [role="tab"][aria-selected="true"]')).map(e => (e.innerText || e.textContent || '').replace(/\s+/g,' ').trim()),
                    paneText: pane ? (pane.innerText || pane.textContent || '').replace(/\s+/g,' ').trim().slice(0,500) : '',
                    paneChildren: pane ? pane.children.length : 0
                };
            }''');
            logger.info("UNREAD FILTER FALLBACK STATE: %r", state_diag)
        except Exception as state_exc:
            logger.debug("UNREAD FILTER FALLBACK STATE failed: %s", state_exc)
        if not data:
            try:
                diag = page.evaluate(r'''() => ({
                    activeTabs: Array.from(document.querySelectorAll('button[aria-selected=\"true\"], [role=\"tab\"][aria-selected=\"true\"]')).map(e => (e.innerText || e.textContent || "").replace(/\s+/g, " " ).trim()),
                    rowCounts: {listItem: document.querySelectorAll("#pane-side [data-testid^=\"list-item-\"]").length, listitem: document.querySelectorAll("#pane-side [role=\"listitem\"]").length, row: document.querySelectorAll("#pane-side [role=\"row\"]").length, gridcell: document.querySelectorAll("#pane-side [role=\"gridcell\"]").length}
                })''')
                logger.info("UNREAD FILTER FALLBACK ZERO DIAG: %r", diag)
            except Exception as diag_exc:
                logger.debug("UNREAD FILTER FALLBACK ZERO DIAG failed: %s", diag_exc)
        return data or []
    except Exception as e:
        logger.warning("UNREAD FILTER FALLBACK failed: %s", e)
        return []
    finally:
        try:
            all_tab = page.locator(
                'button[aria-selected="false"]:has-text("All"), '
                'button[title*="All" i]:not([aria-selected="true"]), '
                '[role="tab"][aria-selected="false"]:has-text("All")'
            ).first
            if all_tab.count() > 0 and all_tab.is_visible():
                all_tab.click(timeout=2000)
                page.wait_for_timeout(250)
                logger.info("UNREAD FILTER FALLBACK: restored All filter")
        except Exception as e:
            logger.debug("UNREAD FILTER FALLBACK: could not restore All filter: %s", e)


def get_unread_chats(page):
    # V17: detect unread chats using DOM evidence first, then WhatsApp's own
    # Unread filter when a manually-marked chat exposes no row-level marker.
    try:
        page.evaluate(r'''() => {
            const pane = document.querySelector('#pane-side');
            if (pane) pane.scrollBy(0, 500);
        }''')
        page.wait_for_timeout(120)
    except Exception:
        pass

    unread_data = page.evaluate(r'''() => {
        const results = [];
        const rows = Array.from(document.querySelectorAll(
            '#pane-side div[role=\"row\"], #pane-side div[role=\"gridcell\"], #pane-side [data-testid^=\"list-item-\"]'
        ));
        const visible = el => {
            if (!el) return false;
            const r = el.getBoundingClientRect(), s = getComputedStyle(el);
            return r.width > 20 && r.height > 20 && r.right > 0 && r.left < innerWidth &&
                   r.bottom > 0 && r.top < innerHeight && s.display !== 'none' &&
                   s.visibility !== 'hidden' && s.opacity !== '0';
        };
        const norm = v => (v || '').replace(/\s+/g, ' ').trim().toLowerCase();
        const numeric = v => /^\d+$/.test((v || '').trim());
        rows.forEach((row, idx) => {
            if (!visible(row)) return;
            const allNodes = [row, ...row.querySelectorAll('*')].filter(visible);
            const titleNode = row.querySelector('[data-testid=\"cell-frame-title\"], [data-testid=\"cell-frame-title-name\"], span[title], span[dir=\"auto\"]');
            let unread = false, count = 1;
            const evidence = [];
            let semanticHits = 0, numericBadge = false, boldTitle = false, boldPreview = false;

            for (const n of allNodes) {
                const label = norm(n.getAttribute('aria-label'));
                const title = norm(n.getAttribute('title'));
                const testid = norm(n.getAttribute('data-testid'));
                const cls = typeof n.className === 'string' ? norm(n.className) : '';
                const icon = norm(n.getAttribute('data-icon'));
                const semantic = `${label} ${title} ${testid} ${cls} ${icon}`;
                const txt = (n.textContent || '').trim();
                // Current WhatsApp builds can expose a glued marker such as
                // ``UnreadDANIAL`` with no trailing word boundary.
                if (/(^|[^a-z])unread(?:\s*messages?)?(?=$|[^a-z])|^unread(?=[a-z0-9+])|message-unread|msg-unread/.test(semantic)) {
                    unread = true; semanticHits++; evidence.push('semantic-unread');
                }
                if (numeric(txt) && n !== row && n.offsetWidth > 0 && n.offsetWidth < 45 &&
                    n.offsetHeight > 0 && n.offsetHeight < 45) {
                    const v = parseInt(txt, 10);
                    if (v > 0) {
                        unread = true; numericBadge = true;
                        count = Math.max(count, Math.min(v, 999)); evidence.push('numeric-badge');
                    }
                }
            }

            if (titleNode && visible(titleNode)) {
                const fw = parseInt(getComputedStyle(titleNode).fontWeight || '400', 10) || 400;
                boldTitle = fw >= 600;
            }
            if (boldTitle) {
                for (const n of allNodes) {
                    if (n === titleNode) continue;
                    const txt = (n.textContent || '').replace(/\s+/g, ' ').trim();
                    if (txt.length < 6 || txt.length > 500) continue;
                    const fw = parseInt(getComputedStyle(n).fontWeight || '400', 10) || 400;
                    const r = n.getBoundingClientRect(), tr = titleNode ? titleNode.getBoundingClientRect() : null;
                    if (fw >= 600 && (!tr || r.top >= tr.bottom - 4)) { boldPreview = true; break; }
                }
            }
            if (!unread && boldTitle && boldPreview) { unread = true; evidence.push('visual-bold-title-preview'); }
            if (!unread) {
                const html = (row.outerHTML || '').toLowerCase();
                if (html.includes('unread') && !html.includes('unread-filter')) { unread = true; evidence.push('row-html-unread'); }
            }
            if (!unread) {
                // Final fallback: WhatsApp sometimes renders the unread token
                // only as text, including the glued ``UnreadName`` form.
                const rowVisibleText = norm(row.innerText || row.textContent || '');
                if (/(^|\s)unread(?:\s+messages?)?(?=\s|$)/.test(rowVisibleText) ||
                    /(?:^|\s)unread(?=[a-z0-9+])/.test(rowVisibleText)) {
                    unread = true; evidence.push('row-text-unread');
                }
            }
            if (!unread) return;
            const rawText = (row.innerText || row.textContent || '').replace(/\n/g, ' ').trim();
            let title = titleNode ? (titleNode.getAttribute('title') || titleNode.textContent || '') : '';
            if (!title) title = rawText.split(' ')[0] || '';
            results.push({index:idx,title:title.trim(),rawText,unread_count:count,rowTestid:row.getAttribute('data-testid'),evidence:[...new Set(evidence)],visual:{boldTitle,boldPreview,semanticHits,numericBadge}});
        });
        return results;
    }''')
    logger.info("UNREAD DISCOVERY SCAN: results=%d raw=%r", len(unread_data or []), unread_data[:15] if isinstance(unread_data, list) else unread_data)

    if not unread_data:
        filter_rows = _discover_unread_via_filter(page)
        if filter_rows:
            unread_data = filter_rows
            logger.info(
                "UNREAD DISCOVERY: using dedicated Unread filter fallback; results=%d",
                len(unread_data),
            )

    if not unread_data:
        try:
            diag = page.evaluate(r'''() => Array.from(document.querySelectorAll(
                '#pane-side div[role=\"row\"], #pane-side div[role=\"gridcell\"], #pane-side [data-testid^=\"list-item-\"]'
            )).filter(el => { const r=el.getBoundingClientRect(), s=getComputedStyle(el); return r.width>20 && r.height>20 && s.display!=="none" && s.visibility!=="hidden"; }).slice(0,30).map((row,i)=>{
                const nodes=[row,...row.querySelectorAll('*')];
                const title=row.querySelector('[data-testid=\"cell-frame-title\"],[data-testid=\"cell-frame-title-name\"],span[title],span[dir=\"auto\"]');
                const bold=nodes.filter(n=>{ const r=n.getBoundingClientRect(); if(r.width<=0||r.height<=0)return false; const fw=parseInt(getComputedStyle(n).fontWeight||'400',10)||400; return fw>=600 && (n.textContent||'').trim().length>2; }).slice(0,8).map(n=>({t:(n.textContent||'').replace(/\s+/g,' ').trim().slice(0,80),fw:getComputedStyle(n).fontWeight,color:getComputedStyle(n).color}));
                return {i,text:(row.innerText||row.textContent||'').replace(/\s+/g,' ').trim().slice(0,180),title:title?(title.getAttribute('title')||title.textContent||'').trim():'',rowClass:typeof row.className==='string'?row.className:'',rowTestid:row.getAttribute('data-testid'),rowAria:row.getAttribute('aria-label'),bold};
            })''')
            logger.info("UNREAD DISCOVERY ZERO DIAG: %r", diag)
        except Exception as e:
            logger.info("UNREAD DISCOVERY ZERO DIAG failed: %s", e)

    chats, processed_chat_names = [], set()
    for item in unread_data:
        try:
            raw_title, raw_text = item.get('title',''), item.get('rawText','')
            if _is_unread_filter_control(raw_title, raw_text):
                logger.info(
                    "UNREAD DISCOVERY: skipping filter-control/status row title=%r raw=%r",
                    raw_title, raw_text[:180],
                )
                continue
            chat_name = clean_whatsapp_chat_name(raw_title) or clean_whatsapp_chat_name(raw_text.split(' ')[0])
            if is_ignored_chat_title(chat_name) or chat_name in processed_chat_names:
                continue
            processed_chat_names.add(chat_name)
            phone = extract_phone_from_text(chat_name or raw_text)

            # Prefer the discovered row/title, because the live title may
            # still contain the glued unread prefix (e.g. UnreadDANIAL).
            esc_raw = (raw_title or '').replace('\\', '\\\\').replace('"', '\\"')
            container = page.locator(f'#pane-side span[title="{esc_raw}"]').first if raw_title else page.locator('#pane-side div[role="row"]').filter(has_text=chat_name).first
            if container.count() == 0:
                esc_name = chat_name.replace('\\', '\\\\').replace('"', '\\"')
                container = page.locator(f'#pane-side span[title*="{esc_name}"]').first
            if container.count() == 0:
                container = page.locator('#pane-side div[role="row"]').filter(has_text=re.compile(re.escape(chat_name), re.I)).first
            if container.count() == 0 and item.get('rowTestid'):
                tid = str(item.get('rowTestid')).replace('"', '\\"')
                container = page.locator(f'#pane-side [data-testid="{tid}"]').first
            logger.info("UNREAD DISCOVERY: chat=%r unread_count=%s evidence=%s visual=%s raw=%r", chat_name, item.get('unread_count',1), item.get('evidence',[]), item.get('visual',{}), raw_text[:180])
            chats.append({'chat_name':chat_name,'phone':phone,'unread_count':max(1,int(item.get('unread_count',1) or 1)),'container':container,'raw_text':raw_text,'recovery_source':','.join(item.get('evidence',[]) or [])})
        except Exception as e:
            logger.warning("Error parsing unread item %d: %s", item.get('index',-1), e)
    try:
        page.evaluate('document.querySelector("#pane-side")?.scrollTo(0, 0);')
    except Exception:
        pass
    return chats

def _sidebar_message_state_get(account_id):
    with SIDEBAR_MESSAGE_STATE_LOCK:
        return dict(SIDEBAR_MESSAGE_STATE.get(account_id, {}))


def _sidebar_message_state_update(account_id, updates):
    if not updates:
        return
    with SIDEBAR_MESSAGE_STATE_LOCK:
        state = SIDEBAR_MESSAGE_STATE.setdefault(account_id, {})
        state.update(updates)
        if len(state) > 500:
            excess = len(state) - 500
            for key in list(state.keys())[:excess]:
                state.pop(key, None)


def _sidebar_message_rows(page):
    """Read visible WhatsApp chat-list previews without changing the UI."""
    try:
        return page.evaluate(r"""
        () => {
            const clean = v => (v || '').replace(/\s+/g, ' ').trim();

            const rows = Array.from(document.querySelectorAll(
                '[data-testid^="list-item-"]'
            ));

            const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 20 && r.height > 20 &&
                       r.right > 0 && r.left < window.innerWidth &&
                       r.bottom > 0 && r.top < window.innerHeight &&
                       s.display !== 'none' &&
                       s.visibility !== 'hidden' &&
                       s.opacity !== '0';
            };

            const looksOutgoing = el => {
                const nodes = [el, ...el.querySelectorAll('*')];

                for (const n of nodes) {
                    const testid = (n.getAttribute('data-testid') || '').toLowerCase();
                    const aria = (n.getAttribute('aria-label') || '').toLowerCase();
                    const icon = (n.getAttribute('data-icon') || '').toLowerCase();
                    const cls = typeof n.className === 'string'
                        ? n.className.toLowerCase()
                        : '';

                    if (
                        testid.includes('msg-dblcheck') ||
                        testid.includes('msg-check') ||
                        aria.includes('message sent') ||
                        aria.includes('message delivered') ||
                        aria.includes('message read') ||
                        icon.includes('msg-dblcheck') ||
                        icon.includes('msg-check') ||
                        cls.includes('message-out')
                    ) {
                        return true;
                    }
                }

                const html = (el.outerHTML || '').toLowerCase();
                return html.includes('msg-dblcheck') ||
                       html.includes('msg-check') ||
                       html.includes('message-out');
            };

            const result = [];

            for (const row of rows) {
                if (!visible(row)) continue;

                const titleNode =
                    row.querySelector('[data-testid="cell-frame-title"]') ||
                    row.querySelector('[data-testid="cell-frame-title-name"]');

                const timeNode =
                    row.querySelector('[data-testid="cell-frame-primary-detail"]');

                const previewNode =
                    row.querySelector('[data-testid="cell-frame-secondary"]') ||
                    row.querySelector('[data-testid="last-msg-status"]');

                const title = clean(
                    titleNode
                        ? (titleNode.getAttribute('title') || titleNode.textContent)
                        : ''
                );

                const timestamp = clean(
                    timeNode
                        ? (timeNode.getAttribute('title') || timeNode.textContent)
                        : ''
                );

                // Extract semantic message text while removing WhatsApp's
                // status/icon markup (for example `wds-ic-read`).  Read/delivered
                // state changes must NOT look like a new message to the sidebar
                // trigger.
                let preview = '';
                if (previewNode) {
                    const clone = previewNode.cloneNode(true);
                    for (const n of Array.from(clone.querySelectorAll('*'))) {
                        const testid = (n.getAttribute('data-testid') || '').toLowerCase();
                        const aria = (n.getAttribute('aria-label') || '').toLowerCase();
                        const dataIcon = (n.getAttribute('data-icon') || '').toLowerCase();
                        const cls = typeof n.className === 'string' ? n.className.toLowerCase() : '';
                        if (
                            testid.includes('msg-check') ||
                            testid.includes('msg-dblcheck') ||
                            aria.includes('message sent') ||
                            aria.includes('message delivered') ||
                            aria.includes('message read') ||
                            dataIcon.includes('msg-check') ||
                            dataIcon.includes('msg-dblcheck') ||
                            cls.includes('wds-ic-') ||
                            cls.includes('ic-expand-more') ||
                            cls.includes('icon')
                        ) {
                            n.remove();
                        }
                    }
                    preview = clean(clone.textContent || clone.innerText);
                }

                // Fallback to raw text only if the semantic extraction removed
                // everything; this keeps attachment/system rows visible without
                // reintroducing read-receipt icon churn.
                if (!preview && previewNode) {
                    const raw = clean(previewNode.textContent || previewNode.innerText);
                    preview = raw.replace(/^wds-ic-[^\s]+\s*/i, '').replace(/ic-expand-more/gi, '').trim();
                }

                // A row with no real message preview is not a useful change trigger.
                if (!title || !preview) continue;

                const rowClone = row.cloneNode(true);
                for (const n of Array.from(rowClone.querySelectorAll('*'))) {
                    const testid = (n.getAttribute('data-testid') || '').toLowerCase();
                    const aria = (n.getAttribute('aria-label') || '').toLowerCase();
                    const dataIcon = (n.getAttribute('data-icon') || '').toLowerCase();
                    const cls = typeof n.className === 'string' ? n.className.toLowerCase() : '';
                    if (
                        testid.includes('msg-check') ||
                        testid.includes('msg-dblcheck') ||
                        aria.includes('message sent') ||
                        aria.includes('message delivered') ||
                        aria.includes('message read') ||
                        dataIcon.includes('msg-check') ||
                        dataIcon.includes('msg-dblcheck') ||
                        cls.includes('wds-ic-') ||
                        cls.includes('ic-expand-more') ||
                        cls.includes('icon')
                    ) {
                        n.remove();
                    }
                }
                const row_text = clean(rowClone.textContent || rowClone.innerText);

                result.push({
                    testid: row.getAttribute('data-testid') || '',
                    title,
                    timestamp,
                    preview,
                    row_text,
                    // Kept for diagnostics only; direction is never used as a
                    // change gate because icon state is unreliable.
                    outgoing: looksOutgoing(row),
                });

                if (result.length >= 150) break;
            }

            return result;
        }
        """)
    except Exception as e:
        logger.debug("Sidebar passive scan failed: %s", e)
        return []


def _snapshot_operator_chat(whatsapp_account, page):
    """Return the chat currently open in the operator's WhatsApp Web pane."""
    try:
        return get_open_chat_header(page, whatsapp_account)
    except Exception as e:
        logger.debug("[%s] CHAT SNAPSHOT: failed: %s", whatsapp_account, e)
        return None


def _open_chat_for_sync(page, chat_name, timeout_ms=5000):
    """Open a sidebar chat temporarily so the real message DOM can be read."""
    if not chat_name:
        return False

    try:
        if safe_click_chat_row(page, chat_name, timeout_ms=timeout_ms):
            # Let WhatsApp finish rendering the conversation before reading it.
            try:
                page.wait_for_timeout(900)
            except Exception:
                pass

            # Confirm that a real conversation/message area appeared.
            for _ in range(8):
                try:
                    if page.locator(
                        '[data-testid="msg-container"], '
                        'div.message-in, div.message-out, '
                        '[data-pre-plain-text], '
                        '[data-testid*="conversation-panel"]'
                    ).count() > 0:
                        return True
                except Exception:
                    pass
                try:
                    page.wait_for_timeout(250)
                except Exception:
                    pass

            logger.warning(
                "CHAT SYNC: clicked %r but conversation message DOM did not appear",
                chat_name,
            )
            return False
    except Exception as e:
        logger.warning("CHAT SYNC: could not open %r: %s", chat_name, e)

    return False


def _restore_operator_chat(whatsapp_account, page, previous_chat):
    """Restore the operator's previously open chat after a background sync scan."""
    if not previous_chat:
        # There was no conversation open before the temporary scan.
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return

    previous_name = (previous_chat.get("chat_name") or "").strip()
    if not previous_name:
        return

    # If it is already the active chat, nothing needs doing.
    try:
        current = get_open_chat_header(page, whatsapp_account)
        if current and (current.get("chat_name") or "").strip() == previous_name:
            return
    except Exception:
        pass

    if safe_click_chat_row(page, previous_name, timeout_ms=5000):
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass
        logger.info(
            "[%s] CHAT SYNC: restored operator chat %r",
            whatsapp_account,
            previous_name,
        )
    else:
        logger.warning(
            "[%s] CHAT SYNC: could not restore operator chat %r",
            whatsapp_account,
            previous_name,
        )


def monitor_sidebar_for_synced_outgoing(whatsapp_account, page):
    """Detect changed sidebar previews and inspect changed chats via real message DOM."""
    rows = _sidebar_message_rows(page)
    if not rows:
        return {"processed": 0, "changed": 0, "skipped": 0, "failures": 0}

    previous = _sidebar_message_state_get(whatsapp_account)
    updates = {}
    processed = changed = skipped = failures = 0
    changed_rows = []

    for row in rows:
        chat_name = (row.get("title") or "").strip()
        timestamp = (row.get("timestamp") or "").strip()
        preview = (row.get("preview") or "").strip()
        if not chat_name or not preview or is_ignored_chat_title(chat_name):
            continue

        key = normalize_person_name(chat_name) or chat_name.lower()
        # The sidebar is only a change detector. Do not depend on WhatsApp's
        # fragile checkmark/icon markup to classify direction here. Include the
        # full visible row text plus stable accessibility/test-id metadata so a
        # phone-side sync is detectable even when timestamp/preview text alone
        # does not change.
        row_text = (row.get('row_text') or '').strip()
        aria_labels = row.get('aria_labels') or []
        title_attrs = row.get('title_attrs') or []
        testids = row.get('testids') or []
        signature_payload = {
            'chat_name': chat_name,
            'timestamp': timestamp,
            'preview': preview,
            'row_text': row_text,
            'aria_labels': aria_labels,
            'title_attrs': title_attrs,
            'testids': testids,
        }
        signature = hashlib.sha256(
            json.dumps(signature_payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
        ).hexdigest()

        previous_signature = previous.get(key)
        logger.debug(
            "[%s] SIDEBAR ROW DEBUG chat=%r time=%r preview=%r row_text=%r testids=%r aria=%r prev=%s curr=%s changed=%s",
            whatsapp_account, chat_name, timestamp, preview, row_text[:300],
            testids[:12], aria_labels[:8],
            (previous_signature or '')[:12], signature[:12],
            bool(previous_signature is not None and previous_signature != signature),
        )

        if key not in previous:
            updates[key] = signature
            continue
        if previous_signature == signature:
            continue

        changed += 1
        logger.info(
            "[%s] SIDEBAR ROW CHANGED: chat=%r time=%r preview=%r row_text=%r",
            whatsapp_account, chat_name, timestamp, preview, row_text[:300],
        )
        changed_rows.append((row, key, signature))

    previous_chat = _snapshot_operator_chat(whatsapp_account, page)

    for row, key, signature in changed_rows[:5]:
        chat_name = (row.get("title") or "").strip()
        updates[key] = signature

        logger.info(
            "[%s] CHAT SYNC: sidebar row changed chat=%r time=%r preview=%r",
            whatsapp_account, chat_name,
            (row.get("timestamp") or "").strip(),
            (row.get("preview") or "").strip(),
        )

        # IMPORTANT: the sidebar chat name is never used as a client key.
        # Open the changed chat first and resolve its actual WhatsApp phone/JID
        # from the conversation DOM. This prevents similarly named clients from
        # being crossed.
        phone = extract_phone_from_text(chat_name)
        if phone:
            try:
                phone = normalize_phone(phone)
            except Exception:
                phone = None

        contact = None
        opened = False
        try:
            logger.info(
                "[%s] CHAT SYNC: changed sidebar row detected for %r; temporarily opening chat to inspect real identity/messages",
                whatsapp_account, chat_name,
            )
            opened = _open_chat_for_sync(page, chat_name)
            if not opened:
                failures += 1
                continue

            # The opened conversation's phone/JID is authoritative. Do not use
            # a name-based database lookup and do not trust a stale name->phone
            # cache for identity.
            try:
                opened_identity = identify_open_client(whatsapp_account, page)
            except Exception as e:
                opened_identity = None
                logger.debug(
                    "[%s] CHAT SYNC: opened-chat identity lookup failed for %r: %s",
                    whatsapp_account, chat_name, e,
                )

            if opened_identity and opened_identity.get("phone"):
                phone = normalize_phone(opened_identity.get("phone"))
                logger.info(
                    "[%s] CHAT SYNC: authoritative WhatsApp phone for %r = %s",
                    whatsapp_account, chat_name, phone,
                )

            if not phone or phone in WORK_PHONE_NUMBERS:
                skipped += 1
                logger.info(
                    "[%s] CHAT SYNC: changed row %r has no valid client WhatsApp phone after opening",
                    whatsapp_account, chat_name,
                )
                continue

            try:
                contact = lookup_client_by_phone_or_whatsapp(phone)
            except Exception as e:
                logger.debug(
                    "[%s] CHAT SYNC: phone lookup failed for %s: %s",
                    whatsapp_account, phone, e,
                )

            if not contact or (contact.get("contact_type") or "").strip().lower() != "client":
                skipped += 1
                logger.info(
                    "[%s] CHAT SYNC: WhatsApp phone %s did not resolve to a client card; name=%r is not used for matching",
                    whatsapp_account, phone, chat_name,
                )
                continue

            real_messages = get_new_messages(
                whatsapp_account=whatsapp_account,
                phone=phone,
                page=page,
                force_process_newest=True,
            )
            if not real_messages:
                logger.info("[%s] CHAT SYNC: no unprocessed messages found after opening %r", whatsapp_account, chat_name)
                continue

            local_processed = 0
            for message_info in real_messages:
                try:
                    result = process_message(
                        whatsapp_account=whatsapp_account, page=page, phone=phone,
                        message_info=message_info, contact=contact,
                    )
                    if isinstance(result, dict) and (result.get("processed") or result.get("duplicate")):
                        processed += 1
                        local_processed += 1
                    else:
                        failures += 1
                except Exception as e:
                    failures += 1
                    logger.exception("[%s] CHAT SYNC: message processing failed for %s: %s", whatsapp_account, phone, e)

            logger.info("[%s] CHAT SYNC: processed %d real message(s) from %r", whatsapp_account, local_processed, chat_name)
        except Exception as e:
            failures += 1
            logger.exception("[%s] CHAT SYNC: failed processing changed chat %r: %s", whatsapp_account, chat_name, e)
        finally:
            if opened:
                _restore_operator_chat(whatsapp_account=whatsapp_account, page=page, previous_chat=previous_chat)

    _sidebar_message_state_update(whatsapp_account, updates)
    logger.info(
        "[%s] SIDEBAR SYNC: rows=%d changed=%d processed=%d skipped=%d failures=%d",
        whatsapp_account, len(rows), changed, processed, skipped, failures,
    )
    return {"processed": processed, "changed": changed, "skipped": skipped, "failures": failures}


def process_all_unread_chats(account_id, page):
    """
    Scans the left side panel (#pane-side) for unread chat badges using 
    resilient fallback selectors and processes opened chats.
    """
    try:
        logger.info("[%s] Scanning sidebar for unread messages...", account_id)

        # 1. Broad selectors targeting unread indicators
        unread_selectors = [
            '#pane-side div[role="row"] [aria-label*="unread" i]',
            '#pane-side div[role="row"] [aria-label*="Unread"]',
            '#pane-side div[role="row"] span._a8lh',
            '#pane-side div[role="row"] div._ak8i',
            '#pane-side div[role="row"] span[dir="ltr"]',
        ]

        unread_badge = None
        for sel in unread_selectors:
            try:
                loc = page.locator(sel)
                c = loc.count()
                for i in range(c):
                    candidate = loc.nth(i)
                    if not candidate.is_visible():
                        continue

                    txt = (candidate.inner_text() or '').strip()
                    aria = (candidate.get_attribute('aria-label') or '').lower()

                    # Valid badge criteria:
                    # A) Explicit 'unread' in ARIA label
                    # B) Strict digit counts (1-999) without time separators (excludes timestamps like '16:00')
                    is_unread_aria = 'unread' in aria
                    is_numeric_badge = txt.isdigit() and 1 <= int(txt) <= 999 and ':' not in txt

                    if is_unread_aria or is_numeric_badge:
                        unread_badge = candidate
                        break

                if unread_badge:
                    break
            except Exception as e:
                logger.debug("[%s] Selector check failed for %s: %s", account_id, sel, e)

        # 2. Fallback: Only toggle 'Unread' filter if it is NOT currently active
        if not unread_badge:
            try:
                # Look specifically for the unselected Unread tab
                unread_tab = page.locator('button[aria-selected="false"]:has-text("Unread"), button[aria-selected="false"][title*="Unread"]')
                if unread_tab.count() > 0 and unread_tab.first.is_visible():
                    logger.info("[%s] UNREAD SCAN: Toggling Unread tab filter...", account_id)
                    unread_tab.first.click()
                    page.wait_for_timeout(1000)

                    first_row = page.locator('#pane-side div[role="row"]').first
                    if first_row.count() > 0 and first_row.is_visible():
                        unread_badge = first_row
                    else:
                        # Reset back to 'All' tab if no unread chats exist under the filter
                        all_tab = page.locator('button:has-text("All"), button[title*="All"]').first
                        if all_tab.count() > 0 and all_tab.is_visible():
                            all_tab.click()
                            page.wait_for_timeout(500)
            except Exception as filter_err:
                logger.debug("[%s] Filter tab fallback failed: %s", account_id, filter_err)

        if not unread_badge:
            logger.info("[%s] UNREAD SCAN: No unread badges found in sidebar.", account_id)
            return

        # Target parent row container to click
        row_target = unread_badge.locator('xpath=ancestor-or-self::div[@role="row"]').first
        if not row_target.count():
            row_target = unread_badge

        logger.info("[%s] Clicking unread chat row...", account_id)
        row_target.click(timeout=3000)
        page.wait_for_timeout(1500)

        # Validate and process newly opened chat
        header_data = get_open_chat_header(page, account_id)
        if header_data and isinstance(header_data, dict):
            phone = header_data.get("phone")
            chat_name = header_data.get("chat_name")
            logger.info("[%s] Successfully opened unread chat: %s ('%s')", account_id, phone, chat_name)

            new_messages = get_new_messages(
                whatsapp_account=account_id,
                phone=phone,
                page=page,
                recovery_unread=True,
                unread_count=max(1, int(chat.get("unread_count", 1))),
            )
            for msg in new_messages:
                process_message(
                    whatsapp_account=account_id,
                    page=page,
                    phone=phone,
                    message_info=msg,
                    contact=header_data,
                )

    except Exception as e:
        logger.exception("[%s] Error processing unread chats from sidebar: %s", account_id, e)

                        
def forensic_whatsapp_dom(page, account_id="?"):
    """Dump a compact, selector-agnostic snapshot of the visible right-side DOM.

    Used only when conversation-header detection fails. This lets us discover
    the actual DOM used by the current WhatsApp Web build instead of guessing
    selectors.
    """
    try:
        result = page.evaluate(r"""
        () => {
            const vw = window.innerWidth || document.documentElement.clientWidth || 1280;
            const vh = window.innerHeight || document.documentElement.clientHeight || 800;
            const minX = Math.floor(vw * 0.35);
            const out = [];
            const seen = new Set();
            const nodes = Array.from(document.querySelectorAll('body *'));
            for (const el of nodes) {
                const r = el.getBoundingClientRect();
                if (!r.width || !r.height || r.right < minX || r.left > vw || r.bottom < 0 || r.top > vh) continue;
                const st = getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
                const testid = el.getAttribute('data-testid') || '';
                const role = el.getAttribute('role') || '';
                const aria = el.getAttribute('aria-label') || '';
                const title = el.getAttribute('title') || '';
                const dataid = el.getAttribute('data-id') || '';
                const pre = el.getAttribute('data-pre-plain-text') || '';
                const cls = typeof el.className === 'string' ? el.className : '';
                const interesting = testid || role === 'row' || dataid || pre || aria || title ||
                    /message|conversation|chat|header|pane|copyable|selectable|compose/i.test(cls) || text.length > 0;
                if (!interesting) continue;
                const key = [testid, role, aria, title, dataid, pre, Math.round(r.x), Math.round(r.y), text.slice(0,160)].join('|');
                if (seen.has(key)) continue;
                seen.add(key);
                out.push({tag:el.tagName.toLowerCase(), testid:testid.slice(0,120), role:role.slice(0,80),
                    aria:aria.slice(0,180), title:title.slice(0,180), cls:cls.slice(0,300),
                    text:text.slice(0,300), data_id:dataid.slice(0,180), pre:pre.slice(0,180),
                    x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height)});
            }
            out.sort((a,b) => {
                const score = x => (x.data_id ? 100 : 0) + (x.pre ? 90 : 0) +
                    (x.testid ? 50 : 0) + (x.aria ? 20 : 0) + (x.title ? 15 : 0) +
                    (x.role ? 10 : 0) + (x.text ? Math.min(x.text.length,20) : 0);
                return score(b) - score(a) || a.y - b.y;
            });
            return {viewport:[vw,vh], nodes:out.slice(0,250)};
        }
        """)
        logger.warning("[%s] ===== FORENSIC DOM START =====", account_id)
        logger.warning("[%s] FORENSIC viewport=%s nodes=%d", account_id, result.get('viewport'), len(result.get('nodes', [])))
        for i, n in enumerate(result.get('nodes', []), 1):
            logger.warning("[%s] DOM[%03d] tag=%s testid=%r role=%r aria=%r title=%r data_id=%r pre=%r rect=(%s,%s,%s,%s) cls=%r text=%r",
                account_id, i, n.get('tag'), n.get('testid'), n.get('role'), n.get('aria'), n.get('title'),
                n.get('data_id'), n.get('pre'), n.get('x'), n.get('y'), n.get('w'), n.get('h'), n.get('cls'), n.get('text'))
        logger.warning("[%s] ===== FORENSIC DOM END =====", account_id)
        return result
    except Exception as e:
        logger.exception("[%s] FORENSIC DOM failed: %s", account_id, e)
        return None


_LAST_OPEN_CHAT_BY_ACCOUNT = {}
_OPEN_CHAT_STALE_TTL = 8.0
_LAST_CLICKED_SIDEBAR_CHAT = {}
_SIDEBAR_CLICK_TTL = 8.0
_SIDEBAR_CLICK_TRACKER_INSTALLED = set()


def _install_sidebar_click_tracker(page):
    """Record real sidebar chat selections for WhatsApp builds without #main."""
    try:
        page_key = id(page)
        if page_key in _SIDEBAR_CLICK_TRACKER_INSTALLED:
            return
        page.evaluate(r"""() => {
            if (window.__wa_sidebar_click_tracker_installed) return true;
            const clean = v => (v || '').replace(/\s+/g, ' ').trim();
            const isBad = text => {
                const t = clean(text).toLowerCase();
                return !t || t.length > 80 ||
                    /^(menu|close|back|search|settings|new chat|chats|groups|favorites|all|archived|status|channels)$/.test(t) ||
                    /^(?:ic-[a-z0-9-]+|wds-ic-[a-z0-9-]+)(?:\s+(?:ic-[a-z0-9-]+|wds-ic-[a-z0-9-]+))*$/i.test(t);
            };
            const findRow = target => {
                if (!target || !target.closest('#pane-side')) return null;
                let row = target.closest('[data-testid*="cell-frame"], [role="row"], [role="gridcell"]');
                if (!row) {
                    let cur = target;
                    for (let i=0; i<9 && cur && cur.id !== 'pane-side'; i++, cur=cur.parentElement) {
                        const r = cur.getBoundingClientRect();
                        if (r.width >= 180 && r.height >= 35 && r.height <= 120) { row = cur; break; }
                    }
                }
                return row;
            };
            const capture = ev => {
                try {
                    const row = findRow(ev.target);
                    if (!row) return;
                    const titleNode = row.querySelector('[data-testid*="cell-frame-title"], span[title], span[dir="auto"], [aria-label]');
                    const title = clean(
                        (titleNode && (titleNode.getAttribute('title') || titleNode.textContent)) ||
                        row.getAttribute('aria-label') || row.getAttribute('title') ||
                        row.getAttribute('data-name') || row.getAttribute('data-chat-name') || ''
                    );
                    if (isBad(title)) return;
                    const rr = row.getBoundingClientRect();
                    window.__wa_last_clicked_chat = {
                        title: title,
                        ts: Date.now(),
                        x: Math.round(rr.x), y: Math.round(rr.y),
                        w: Math.round(rr.width), h: Math.round(rr.height)
                    };
                } catch (_) {}
            };
            document.addEventListener('pointerdown', capture, true);
            document.addEventListener('click', capture, true);
            window.__wa_sidebar_click_tracker_installed = true;
            return true;
        }""")
        _SIDEBAR_CLICK_TRACKER_INSTALLED.add(page_key)
    except Exception as e:
        logger.debug("[WHATSAPP HEADER DEBUG] sidebar click tracker install failed: %s", e)


def _get_last_clicked_sidebar_chat(page, whatsapp_account=None):
    try:
        item = page.evaluate(r"""() => window.__wa_last_clicked_chat || null""")
        if not item or not item.get('title'):
            return None
        age = (time.time() * 1000 - float(item.get('ts', 0))) / 1000.0
        if age < 0 or age > _SIDEBAR_CLICK_TTL:
            return None
        chat_name = clean_whatsapp_chat_name(item.get('title') or '') or item.get('title', '').strip()
        if not chat_name or is_ignored_chat_title(chat_name):
            return None
        phone = extract_phone_from_text(chat_name)
        if not phone and whatsapp_account:
            try:
                phone = get_cached_phone(whatsapp_account, chat_name)
            except Exception:
                pass
        return {
            'chat_name': chat_name,
            'phone': phone,
            'is_group': is_group_chat_header(chat_name, [chat_name]),
            'locator': None,
            'age': round(age, 1),
        }
    except Exception:
        return None



def _non_chat_drawer_visible(page) -> str | None:
    """Return the visible non-conversation drawer/state, if any.

    WhatsApp can replace the middle pane with Settings, Help, Profile,
    Contact Info, keyboard-shortcuts, etc. Those states must invalidate any
    previously remembered sidebar chat.
    """
    try:
        return page.evaluate(r"""() => {
            const checks = [
                ['settings', '[data-testid="drawer-middle"] [data-testid="empty-state-drawer"], [data-testid="empty-state-drawer"]'],
                ['settings', '[data-testid*="settings"]'],
                ['help', '[data-testid*="help"]'],
                ['profile', '[data-testid*="profile"]'],
                ['keyboard-shortcuts', '[data-testid="li-keyboard-shortcuts"]'],
                ['contact-info', '[data-testid="contact-info-drawer"]'],
                ['drawer-right', '[data-testid="drawer-right"]']
            ];
            const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 100 && r.height > 100 && st.display !== 'none' &&
                       st.visibility !== 'hidden' && st.opacity !== '0';
            };
            for (const [name, sel] of checks) {
                try {
                    for (const el of document.querySelectorAll(sel)) {
                        if (visible(el)) return name;
                    }
                } catch (_) {}
            }
            return null;
        }""")
    except Exception:
        return None


def _whatsapp_dom_state(page):
    try:
        return page.evaluate("""() => ({app:!!document.querySelector('#app'), sidebar:!!document.querySelector('#pane-side'), sidebarVisible:Array.from(document.querySelectorAll('#pane-side')).some(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>20&&r.height>20&&s.display!=='none'&&s.visibility!=='hidden'}), conversation:document.querySelectorAll('[data-testid*="conversation-panel"],[data-testid*="conversation-header"],[data-testid*="chat-panel"],[data-testid*="chat-header"]').length, main:document.querySelectorAll('#main,div[role="main"]').length, ready:document.readyState, title:document.title||''})""")
    except Exception as e:
        logger.debug("[WHATSAPP HEADER DEBUG] DOM state check failed: %s", e)
        return None


def get_open_chat_header(page, whatsapp_account=None) -> dict | None:
    """Identify the currently open WhatsApp conversation without clicking.

    WhatsApp Web changes its DOM frequently.  Do not use the global
    application <header>, and do not treat buttons/icons in the right pane
    as chat names.  The detector uses, in order:

      1. conversation/chat header testids when available;
      2. meaningful non-control text in the top of the right pane;
      3. the currently selected sidebar row;
      4. a cached phone number for the selected chat.

    ``whatsapp_account`` is optional for backwards compatibility.
    """
    try:
        _install_sidebar_click_tracker(page)

        # A contact-info drawer can cover the conversation pane.
        try:
            drawer = page.locator('[data-testid="drawer-right"]').first
            if drawer.count() and drawer.is_visible():
                logger.info(
                    "[WHATSAPP HEADER DEBUG] drawer-right is visible; "
                    "pressing Escape before chat detection"
                )
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
        except Exception as e:
            logger.debug("[WHATSAPP HEADER DEBUG] drawer check failed: %s", e)

        # ------------------------------------------------------------
        # 1. Stable conversation-panel/header testids.
        # ------------------------------------------------------------
        dom_state = _whatsapp_dom_state(page)
        logger.info("[WHATSAPP HEADER DEBUG] DOM state=%r", dom_state)
        for _ in range(3):
            if dom_state and (dom_state.get("sidebarVisible") or dom_state.get("conversation") or dom_state.get("main")):
                break
            try: page.wait_for_timeout(350)
            except Exception: pass
            dom_state = _whatsapp_dom_state(page)
        logger.info("[WHATSAPP HEADER DEBUG] DOM state after wait=%r", dom_state)

        # WhatsApp's intro-panel is the authoritative empty-state: no chat is
        # selected in the middle pane. Never return a stale cached/clicked chat
        # while this panel is visible.
        try:
            empty_state = page.locator('[data-testid="intro-panel"], [data-testid*="intro-panel"]')
            for i in range(min(empty_state.count(), 3)):
                try:
                    if empty_state.nth(i).is_visible():
                        logger.info("[WHATSAPP HEADER DEBUG] intro/empty-state panel is visible; no conversation is open")
                        return None
                except Exception:
                    pass
        except Exception as e:
            logger.debug("[WHATSAPP HEADER DEBUG] intro-panel check failed: %s", e)

        non_chat_state = _non_chat_drawer_visible(page)
        if non_chat_state:
            logger.info(
                "[WHATSAPP HEADER DEBUG] non-chat drawer/state %r is visible; "
                "no operator-opened conversation is active",
                non_chat_state
            )
            if whatsapp_account:
                _LAST_OPEN_CHAT_BY_ACCOUNT.pop(whatsapp_account, None)
            try:
                page.evaluate("() => { window.__wa_last_clicked_chat = null; }")
            except Exception:
                pass
            return None

        panel_info = page.evaluate(r"""() => {
            const vw = window.innerWidth || document.documentElement.clientWidth || 0;
            const vh = window.innerHeight || document.documentElement.clientHeight || 0;

            const visible = el => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 20 && r.height > 15 &&
                    r.right > vw * 0.40 && r.left < vw &&
                    r.top < vh && r.bottom > 0 &&
                    st.display !== 'none' &&
                    st.visibility !== 'hidden' &&
                    st.opacity !== '0';
            };

            const selectors = [
                '[data-testid*="conversation-panel"]',
                '[data-testid*="conversation-header"]',
                '[data-testid*="chat-panel"]',
                '[data-testid*="chat-header"]'
            ];

            const candidates = [];
            for (const sel of selectors) {
                for (const el of document.querySelectorAll(sel)) {
                    if (!visible(el)) continue;
                    const r = el.getBoundingClientRect();
                    candidates.push({
                        selector: sel,
                        testid: el.getAttribute('data-testid') || '',
                        text: (el.innerText || el.textContent || '').trim().slice(0, 300),
                        x: Math.round(r.x),
                        y: Math.round(r.y),
                        w: Math.round(r.width),
                        h: Math.round(r.height)
                    });
                }
            }

            return {vw, vh, candidates: candidates.slice(0, 20)};
        }""")

        logger.info(
            "[WHATSAPP HEADER DEBUG] viewport=%sx%s conversation candidates=%r",
            panel_info.get("vw"),
            panel_info.get("vh"),
            panel_info.get("candidates")
        )

        # ------------------------------------------------------------
        # 2. Authoritative conversation-header extraction.
        #
        # The rendered right pane can contain message dates/times and other
        # text nodes that look like plausible titles.  The conversation-header
        # is the authoritative identity source, so extract its first meaningful
        # line before considering any generic right-pane candidates.
        # ------------------------------------------------------------
        try:
            authoritative = page.evaluate(r"""() => {
                const clean = v => (v || '').replace(/\s+/g, ' ').trim();
                const bad = new Set([
                    'add to list', 'click here for contact info', 'online',
                    'typing...', 'typing…', 'last seen', 'video call', 'voice call', 'call'
                ]);
                const headers = Array.from(document.querySelectorAll(
                    '[data-testid="conversation-header"], [data-testid*="conversation-header"]'
                ));
                for (const el of headers) {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    if (r.width < 100 || r.height < 20 || r.right < window.innerWidth * 0.35 ||
                        r.bottom <= 0 || r.top >= 120 || st.display === 'none' ||
                        st.visibility === 'hidden' || st.opacity === '0') continue;

                    const lines = (el.innerText || el.textContent || '')
                        .split(/\n+/).map(clean).filter(Boolean);
                    const meaningful = lines.filter(v => !bad.has(v.toLowerCase()));
                    if (!meaningful.length) continue;

                    // WhatsApp sometimes renders a name and contact-info label
                    // as separate spans.  The first non-control line is the
                    // actual conversation title.
                    return {
                        name: meaningful[0],
                        lines: meaningful.slice(0, 6),
                        x: r.x, y: r.y, w: r.width, h: r.height
                    };
                }
                return null;
            }""")

            if authoritative and authoritative.get('name'):
                authoritative_name = clean_whatsapp_chat_name(authoritative.get('name') or '') or (authoritative.get('name') or '').strip()
                authoritative_phone = extract_phone_from_text(authoritative_name)
                # Reject date separators (for example 07/07/2026) as titles
                # even if a generic phone parser can turn them into digits.
                if re.fullmatch(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', authoritative_name):
                    authoritative_phone = None
                if (
                    authoritative_name
                    and authoritative_name.strip().lower() not in {
                        'video call', 'voice call', 'call', 'profile details',
                        'add to list', 'close', 'menu', 'back', 'search'
                    }
                    and not is_ignored_chat_title(authoritative_name)
                ):
                    authoritative_group = is_group_chat_header(
                        authoritative_name, authoritative.get('lines') or [authoritative_name]
                    )
                    logger.info(
                        "[WHATSAPP HEADER] Active Chat: name=%r phone=%r is_group=%s source=authoritative-conversation-header",
                        authoritative_name, authoritative_phone, authoritative_group
                    )
                    header = {
                        'chat_name': authoritative_name,
                        'phone': authoritative_phone,
                        'is_group': authoritative_group,
                        'locator': None,
                    }
                    if whatsapp_account:
                        _LAST_OPEN_CHAT_BY_ACCOUNT[whatsapp_account] = {
                            'timestamp': time.time(), 'header': dict(header)
                        }
                    return header
        except Exception as e:
            logger.debug("[WHATSAPP HEADER DEBUG] authoritative conversation-header extraction failed: %s", e)

        # ------------------------------------------------------------
        # 3. Right-pane title detection.
        #
        # IMPORTANT:
        # Generic right-pane candidates are only a fallback after the actual
        # conversation-header has failed. Explicitly reject controls/icons and
        # date-like strings before scoring/returning them.
        # ------------------------------------------------------------
        result = page.evaluate(r"""() => {
            const vw = window.innerWidth || document.documentElement.clientWidth || 0;
            const vh = window.innerHeight || document.documentElement.clientHeight || 0;

            const badExact = new Set([
                'menu', 'close', 'back', 'search', 'online', 'today', 'yesterday', 'video call', 'voice call', 'call',
                'typing...', 'typing…', 'last seen',
                'click here for contact info', 'add to list',
                'business account', 'profile details', 'contact info',
                'updates in status', 'status', 'channels', 'communities',
                'storefront', 'settings', 'new chat', 'unread', 'archived',
                'all', 'groups', 'favorites', 'chats', 'messages', 'you',
                'read', 'more', 'options', 'more options', 'forward',
                'reply', 'react', 'download', 'star', 'delete'
            ]);

            const badPart = [
                'wa-wordmark', 'wds-ic-', 'megaphone',
                'storefront', 'settings-refreshed', 'icon',
                'ic-more', 'ic-close', 'menu'
            ];

            const clean = v => (v || '').replace(/\\s+/g, ' ').trim();

            const visible = el => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 2 && r.height > 2 &&
                    r.left > vw * 0.35 &&
                    r.right <= vw + 5 &&
                    r.top >= 0 &&
                    r.top < Math.min(vh, 220) &&
                    st.display !== 'none' &&
                    st.visibility !== 'hidden' &&
                    st.opacity !== '0';
            };

            const isControl = el => {
                const tag = (el.tagName || '').toUpperCase();
                const role = (el.getAttribute('role') || '').toLowerCase();
                const testid = (el.getAttribute('data-testid') || '').toLowerCase();
                const aria = clean(el.getAttribute('aria-label') || '').toLowerCase();
                const title = clean(el.getAttribute('title') || '').toLowerCase();
                const cls = typeof el.className === 'string'
                    ? el.className.toLowerCase()
                    : '';

                if (['BUTTON', 'SVG', 'PATH', 'INPUT', 'TEXTAREA'].includes(tag))
                    return true;

                if (['button', 'menuitem', 'checkbox', 'radio'].includes(role))
                    return true;

                if (
                    testid.includes('menu') ||
                    testid.includes('icon') ||
                    testid.includes('close') ||
                    testid.includes('search')
                )
                    return true;

                const controlWords = [
                    'menu', 'close', 'back', 'search', 'video call', 'voice call',
                    'call', 'profile details', 'more options', 'options', 'download'
                ];

                if (controlWords.some(word => aria === word || aria.includes(word)))
                    return true;

                if (controlWords.some(word => title === word || title.includes(word)))
                    return true;

                if (
                    cls.includes('icon') ||
                    cls.includes('menu') ||
                    cls.includes('close')
                )
                    return true;

                return false;
            };

            const candidates = [];
            const seen = new Set();

            const nodes = Array.from(document.querySelectorAll(
                'span[dir="auto"], div[dir="auto"], [title], [aria-label]'
            ));

            for (const el of nodes) {
                if (!visible(el) || isControl(el)) continue;

                const title = clean(el.getAttribute('title') || '');
                const aria = clean(el.getAttribute('aria-label') || '');
                const content = clean(el.textContent || '');

                // Prefer the shortest useful value.  A parent's textContent
                // often contains status/control text as well.
                const values = [title, aria, content]
                    .filter(Boolean)
                    .sort((a, b) => a.length - b.length);

                for (const text of values) {
                    if (!text || text.length > 80) continue;

                    const low = text.toLowerCase();

                    if (badExact.has(low)) continue;
                    if (badPart.some(p => low.includes(p))) continue;
                    // Date separators such as 07/07/2026 are never chat titles.
                    if (/^\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}$/.test(text)) continue;
                    if (/^\d+$/.test(text) && text.length < 8) continue;
                    if (/^[^a-zA-Z0-9]+$/.test(text)) continue;

                    // Reject obvious button-like labels even if their
                    // surrounding element is not itself a BUTTON.
                    if (
                        /^(menu|close|back|search|more|options)$/i.test(text)
                    )
                        continue;

                    const r = el.getBoundingClientRect();

                    // Chat names normally occupy a small area near the
                    // upper-left of the conversation pane, not the far
                    // left control strip.
                    let score = 0;

                    if (title) score += 45;
                    if (aria) score += 10;
                    if (el.matches('span[dir="auto"], div[dir="auto"]'))
                        score += 15;

                    if (r.y < 80) score += 35;
                    else if (r.y < 140) score += 15;

                    if (r.x > vw * 0.45) score += 15;
                    else if (r.x > vw * 0.40) score += 5;

                    if (text.length >= 2 && text.length <= 50) score += 10;

                    // Phone-number candidates are much stronger evidence
                    // of a real conversation title than date separators such
                    // as 'Today'. Prefer them decisively.
                    if (/(?:\+?\d[\d\s().-]{7,}\d)/.test(text)) score += 100;

                    // Real chat names are usually wider than tiny icon
                    // labels.  Give them a small preference.
                    if (r.width >= 30) score += 5;

                    const key = `${text}|${Math.round(r.x)}|${Math.round(r.y)}`;
                    if (seen.has(key)) continue;
                    seen.add(key);

                    candidates.push({
                        text,
                        x: r.x,
                        y: r.y,
                        w: r.width,
                        h: r.height,
                        tag: el.tagName,
                        title,
                        aria,
                        score
                    });
                }
            }

            candidates.sort((a, b) =>
                (b.score - a.score) ||
                (a.y - b.y) ||
                (a.x - b.x)
            );

            return {
                vw,
                vh,
                candidates: candidates.slice(0, 40)
            };
        }""")

        logger.info(
            "[WHATSAPP HEADER DEBUG] right-pane title candidates=%r",
            result.get("candidates")
        )

        candidates = result.get("candidates") or []

        # ------------------------------------------------------------
        # 2a. Accept a right-pane candidate only when it is not a UI title.
        # ------------------------------------------------------------
        for chosen in candidates:
            raw_text = (chosen.get("text") or "").strip()
            chat_name = clean_whatsapp_chat_name(raw_text) or raw_text

            chat_name = re.sub(
                r'^(Add to list|online|typing…?|last seen.*)$',
                '',
                chat_name,
                flags=re.I
            ).strip()

            if not chat_name:
                continue

            if is_ignored_chat_title(chat_name):
                continue

            # Do not mistake message date separators for chat titles.
            if re.fullmatch(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', chat_name):
                logger.debug(
                    "[WHATSAPP HEADER DEBUG] Rejecting date-like candidate %r",
                    chat_name,
                )
                continue

            # Bare numeric chat titles are only valid when they contain a
            # plausible full phone number. This prevents values such as the
            # 8-digit digits produced from 07/07/2026 from becoming identities.
            if chat_name.isdigit() and len(chat_name) < 10:
                continue

            # A second defensive check against the exact failure seen in
            # the logs: "Menu" was being returned as the active chat.
            if chat_name.lower() in {
                "menu", "close", "back", "search", "more", "options",
                "today", "yesterday"
            }:
                logger.debug(
                    "[WHATSAPP HEADER DEBUG] Rejecting control candidate %r",
                    chat_name
                )
                continue

            phone = extract_phone_from_text(raw_text)
            header_lines = [chat_name]
            is_group = is_group_chat_header(chat_name, header_lines)

            logger.info(
                "[WHATSAPP HEADER] Active Chat: name=%r phone=%r "
                "is_group=%s source=right-pane-candidate score=%s",
                chat_name,
                phone,
                is_group,
                chosen.get("score"),
            )

            header = {
                'chat_name': chat_name,
                'phone': phone,
                'is_group': is_group,
                'locator': None,
            }
            if whatsapp_account:
                _LAST_OPEN_CHAT_BY_ACCOUNT[whatsapp_account] = {'timestamp': time.time(), 'header': dict(header)}
            return header

        # ------------------------------------------------------------
        # 2b. Generic right-pane DOM fallback.
        #
        # Some current WhatsApp builds do not expose conversation-panel,
        # #main, aria-selected, or dir="auto" markers at all.  In that
        # situation inspect the actual visible DOM geometry instead of
        # concluding that there is no open chat.  The chat title is normally
        # a short text node in the upper part of the right half of the page.
        # ------------------------------------------------------------
        generic_info = page.evaluate(r"""() => {
            const vw = window.innerWidth || document.documentElement.clientWidth || 0;
            const vh = window.innerHeight || document.documentElement.clientHeight || 0;
            const clean = v => (v || '').replace(/\s+/g, ' ').trim();
            const bad = new Set([
                'menu','close','back','search','more','options','online',
                'typing...','typing…','add to list','today','yesterday',
                'settings','new chat','status','channels','communities',
                'unread','archived','chats','groups','favorites','all'
            ]);
            const visible = el => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width >= 8 && r.height >= 8 &&
                    r.left > vw * 0.32 && r.right <= vw + 5 &&
                    r.top >= 0 && r.top < Math.min(vh, 180) &&
                    st.display !== 'none' && st.visibility !== 'hidden' &&
                    st.opacity !== '0';
            };
            const control = el => {
                const tag = (el.tagName || '').toUpperCase();
                const role = (el.getAttribute('role') || '').toLowerCase();
                const aria = clean(el.getAttribute('aria-label') || '').toLowerCase();
                const title = clean(el.getAttribute('title') || '').toLowerCase();
                const testid = (el.getAttribute('data-testid') || '').toLowerCase();
                const cls = typeof el.className === 'string' ? el.className.toLowerCase() : '';
                const ownText = clean(el.textContent || '').toLowerCase();
                if (['BUTTON','INPUT','TEXTAREA','SVG','PATH'].includes(tag)) return true;
                if (['button','menuitem','checkbox','radio'].includes(role)) return true;
                if (bad.has(aria) || bad.has(title)) return true;
                if (testid.includes('icon') || testid.includes('menu') || testid.includes('close') || testid.includes('search')) return true;
                if (/(^|[ _-])(icon|menu|close|search|more|options)([ _-]|$)/.test(cls)) return true;
                if (/(^|[ _-])wds-ic-[a-z0-9-]+([ _-]|$)/.test(cls) || /(^|[ _-])ic-[a-z0-9-]+([ _-]|$)/.test(cls)) return true;
                if (/^(?:ic-[a-z0-9-]+|wds-ic-[a-z0-9-]+)(?:\s+(?:ic-[a-z0-9-]+|wds-ic-[a-z0-9-]+))*$/i.test(ownText)) return true;
                return false;
            };
            const out = [];
            const seen = new Set();
            for (const el of document.querySelectorAll('h1,h2,h3,p,span,div,a,[role="heading"],[title],[aria-label]')) {
                if (!visible(el) || control(el)) continue;
                const text = clean(el.innerText || el.textContent || el.getAttribute('title') || el.getAttribute('aria-label') || '');
                const lowText = text.toLowerCase();
                if (text.length < 2 || text.length > 60 || bad.has(lowText)) continue;
                if (/^(?:ic-[a-z0-9-]+|wds-ic-[a-z0-9-]+)(?:\s+(?:ic-[a-z0-9-]+|wds-ic-[a-z0-9-]+))*$/i.test(text)) continue;
                if (/^(?:ic-[a-z0-9-]+wds-ic-[a-z0-9-]+)$/i.test(text)) continue;
                if (/^[^a-zA-Z0-9]+$/.test(text)) continue;
                const r = el.getBoundingClientRect();
                // Reject large containers whose text contains many UI labels.
                if (text.split(' ').length > 8) continue;
                const key = text + '|' + Math.round(r.x) + '|' + Math.round(r.y);
                if (seen.has(key)) continue;
                seen.add(key);
                let score = 0;
                if (r.y < 75) score += 60; else if (r.y < 120) score += 25;
                if (r.x > vw * 0.45) score += 20;
                if (r.width >= 40 && r.width <= 400) score += 10;
                if (['H1','H2','H3'].includes(el.tagName)) score += 25;
                if (el.getAttribute('role') === 'heading') score += 25;
                if (el.children.length === 0) score += 10;
                out.push({text, x:r.x, y:r.y, w:r.width, h:r.height, tag:el.tagName, score});
            }
            out.sort((a,b) => b.score-a.score || a.y-b.y || a.x-b.x);
            return out.slice(0, 30);
        }""")
        logger.info("[WHATSAPP HEADER DEBUG] generic right-pane candidates=%r", generic_info)

        # page.evaluate above returns a list.
        for chosen in generic_info if isinstance(generic_info, list) else []:
            chat_name = clean_whatsapp_chat_name((chosen.get('text') or '').strip()) or (chosen.get('text') or '').strip()
            if not chat_name or is_ignored_chat_title(chat_name):
                continue
            if chat_name.lower() in {'menu','close','back','search','more','options','today','online'}:
                continue
            # Never treat WhatsApp date separators as conversation titles.
            if re.fullmatch(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', chat_name):
                logger.debug('[WHATSAPP HEADER DEBUG] rejecting date-like generic candidate=%r', chat_name)
                continue
            phone = extract_phone_from_text(chat_name)
            header = {'chat_name': chat_name, 'phone': phone, 'is_group': False, 'locator': None}
            logger.info("[WHATSAPP HEADER] Active Chat: name=%r phone=%r source=generic-right-pane score=%s", chat_name, phone, chosen.get('score'))
            if whatsapp_account:
                _LAST_OPEN_CHAT_BY_ACCOUNT[whatsapp_account] = {'timestamp': time.time(), 'header': dict(header)}
            return header

        # ------------------------------------------------------------
        # 3. Selected-sidebar fallback.
        #
        # aria-selected is not guaranteed on every WhatsApp build, so also
        # inspect common active-row markers and rows with a selected/focused
        # descendant.
        # ------------------------------------------------------------
        selected_info = page.evaluate(r"""() => {
            const pane = document.querySelector('#pane-side');
            if (!pane) return [];
            const clean = v => (v || '').replace(/\s+/g, ' ').trim();
            const bad = new Set(['menu','close','back','search','settings','new chat','chats','groups','favorites','all','archived','status','channels']);
            const rows = Array.from(pane.querySelectorAll('[data-testid*="cell-frame"], [role="row"], [role="gridcell"], [tabindex="0"]'));
            const active = [];
            for (const row of rows) {
                const r = row.getBoundingClientRect();
                const st = getComputedStyle(row);
                if (r.width < 140 || r.height < 25 || r.top < 0 || r.bottom > (window.innerHeight || 800) || st.display === 'none' || st.visibility === 'hidden') continue;
                const attr = [
                    row.getAttribute('aria-selected'), row.getAttribute('aria-current'),
                    row.getAttribute('data-selected'), row.getAttribute('data-active'),
                    row.getAttribute('tabindex')
                ].map(v => (v || '').toLowerCase());
                const cls = typeof row.className === 'string' ? row.className.toLowerCase() : '';
                let score = 0;
                if (attr.includes('true') || attr.includes('page')) score += 100;
                if (attr.includes('0')) score += 35;
                if (cls.includes('selected') || cls.includes('active') || cls.includes('focus')) score += 75;
                if (row.matches(':focus-within')) score += 60;
                const titleNode = row.querySelector('[data-testid*="cell-frame-title"], [data-testid*="title"], span[title], span[dir="auto"], [aria-label]');
                const values = [
                    titleNode && (titleNode.getAttribute('title') || titleNode.textContent),
                    row.getAttribute('aria-label'), row.getAttribute('title'),
                    row.getAttribute('data-name'), row.getAttribute('data-chat-name')
                ].map(clean).filter(Boolean);
                values.sort((a,b) => a.length-b.length);
                const value = values.find(v => v.length >= 2 && v.length <= 80 && !bad.has(v.toLowerCase()) && !/^(?:ic-[a-z0-9-]+|wds-ic-[a-z0-9-]+)(?:\s+(?:ic-[a-z0-9-]+|wds-ic-[a-z0-9-]+))*$/i.test(v));
                if (!value) continue;
                if (score <= 0) {
                    const bg = st.backgroundColor || '';
                    if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') score += 10;
                }
                active.push({text:value, x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height), score, cls:cls.slice(0,120)});
            }
            active.sort((a,b) => b.score-a.score || a.y-b.y);
            return active.slice(0,20);
        }""")

        logger.info(
            "[WHATSAPP HEADER DEBUG] selected-sidebar candidates=%r",
            selected_info
        )

        for candidate in selected_info or []:
            raw_text = (candidate.get("text") or "").strip()
            chat_name = clean_whatsapp_chat_name(raw_text) or raw_text

            chat_name = re.sub(
                r'^(Add to list|online|typing…?|last seen.*)$',
                '',
                chat_name,
                flags=re.I
            ).strip()

            if not chat_name:
                continue

            if is_ignored_chat_title(chat_name):
                continue

            if len(chat_name) <= 3 and chat_name.isdigit():
                continue

            if chat_name.lower() in {
                "menu", "close", "back", "search", "more", "options",
                "today", "yesterday"
            }:
                continue

            phone = extract_phone_from_text(raw_text)

            if not phone and whatsapp_account:
                try:
                    phone = get_cached_phone(whatsapp_account, chat_name)
                except Exception as e:
                    logger.debug(
                        "[WHATSAPP HEADER DEBUG] phone-cache lookup failed "
                        "for %r: %s",
                        chat_name,
                        e
                    )

            is_group = is_group_chat_header(chat_name, [chat_name])

            logger.info(
                "[WHATSAPP HEADER] Active Chat: name=%r phone=%r "
                "is_group=%s source=selected-sidebar",
                chat_name,
                phone,
                is_group,
            )

            header = {
                'chat_name': chat_name,
                'phone': phone,
                'is_group': is_group,
                'locator': None,
            }
            if whatsapp_account:
                _LAST_OPEN_CHAT_BY_ACCOUNT[whatsapp_account] = {'timestamp': time.time(), 'header': dict(header)}
            return header

        # ------------------------------------------------------------
        # 4. Targeted forensic fallback.  When WhatsApp renders a conversation
        # without any of the historical markers, record only the small set of
        # visible text-bearing nodes in the likely conversation/header areas.
        # This gives us the actual DOM shape without dumping the whole page.
        # ------------------------------------------------------------
        try:
            forensic = page.evaluate(r"""() => {
                const vw = window.innerWidth || document.documentElement.clientWidth || 0;
                const vh = window.innerHeight || document.documentElement.clientHeight || 0;
                const clean = v => (v || '').replace(/\s+/g, ' ').trim();
                const out = [];
                const nodes = Array.from(document.querySelectorAll(
                    'body *[aria-label], body *[title], body *[data-testid], body span, body div, body h1, body h2, body h3, body p, body a'
                ));
                for (const el of nodes) {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    if (r.width < 8 || r.height < 8 || r.left < vw * 0.25 || r.right > vw + 5 ||
                        r.top < 0 || r.top > Math.min(vh, 360) ||
                        st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
                    const text = clean(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                    const aria = clean(el.getAttribute('aria-label') || '');
                    const title = clean(el.getAttribute('title') || '');
                    const testid = el.getAttribute('data-testid') || '';
                    const cls = typeof el.className === 'string' ? el.className : '';
                    if (text.length < 2 || text.length > 100) continue;
                    if (/^(?:ic-[a-z0-9-]+|wds-ic-[a-z0-9-]+)(?:\s+(?:ic-[a-z0-9-]+|wds-ic-[a-z0-9-]+))*$/i.test(text)) continue;
                    out.push({tag:el.tagName, text:text.slice(0,100), aria, title, testid, cls:cls.slice(0,180), x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height)});
                }
                const seen = new Set();
                return out.filter(n => {
                    const k = [n.tag,n.text,n.aria,n.title,n.testid,n.x,n.y,n.w,n.h].join('|');
                    if (seen.has(k)) return false; seen.add(k); return true;
                }).sort((a,b) => a.y-b.y || a.x-b.x).slice(0,80);
            }""")
            logger.info("[WHATSAPP HEADER DEBUG] forensic upper-right nodes=%r", forensic)
        except Exception as e:
            logger.debug("[WHATSAPP HEADER DEBUG] forensic DOM probe failed: %s", e)

        clicked = _get_last_clicked_sidebar_chat(page, whatsapp_account)
        if clicked:
            header = {
                'chat_name': clicked.get('chat_name'),
                'phone': clicked.get('phone'),
                'is_group': clicked.get('is_group', False),
                'locator': None,
            }
            logger.info(
                "[WHATSAPP HEADER] Active Chat: name=%r phone=%r source=sidebar-click age=%ss",
                header.get('chat_name'), header.get('phone'), clicked.get('age')
            )
            if whatsapp_account:
                _LAST_OPEN_CHAT_BY_ACCOUNT[whatsapp_account] = {'timestamp': time.time(), 'header': dict(header)}
            return header

        logger.info(
            "[WHATSAPP HEADER DEBUG] No currently open chat detected; "
            "stale last-known chat will NOT be reused"
        )
        return None

    except Exception as e:
        logger.warning(
            "[WHATSAPP HEADER DEBUG] Robust header detection failed: %s",
            e
        )
        return None

def get_currently_open_chat(page):
    """Retrieves information for the chat currently open in the right-hand panel (#main header)."""
    try:
        main_header = page.locator("#main header")
        if main_header.count() == 0 or not main_header.is_visible():
            return None

        header_text = main_header.inner_text(timeout=1000).strip()
        chat_name = clean_whatsapp_chat_name(header_text)
        phone = extract_phone_from_text(
            header_text
        ) or extract_phone_from_text(chat_name)

        if not chat_name and not phone:
            return None

        return {"chat_name": chat_name, "phone": phone}
    except Exception as e:
        logger.debug("Could not resolve open chat header: %s", e)
        return None


# ============================================================
# LOOKUP CONTACT
# ============================================================


def lookup_contact(phone):
    if not phone:
        return None

    return lookup_client_by_phone_or_whatsapp(phone)


# ============================================================
# IDENTIFY CLIENT
# ============================================================


def identify_client_from_unread_chat(whatsapp_account, chat):
    whatsapp_phone = (chat.get("phone") or "").strip()

    if not whatsapp_phone:
        logger.warning(
            "[%s] Unread chat has no WhatsApp phone number. Cannot identify"
            " client.",
            whatsapp_account,
        )

        return None

    try:
        whatsapp_phone = normalize_phone(whatsapp_phone)

    except Exception as e:
        logger.warning(
            "[%s] Could not normalize WhatsApp phone %r: %s",
            whatsapp_account,
            whatsapp_phone,
            e,
        )

        return None

    if not whatsapp_phone:
        logger.warning(
            "[%s] WhatsApp phone became empty after normalization.",
            whatsapp_account,
        )

        return None

    logger.info(
        "[%s] Looking up client by WhatsApp phone %s",
        whatsapp_account,
        whatsapp_phone,
    )

    contact = lookup_client_by_phone_or_whatsapp(whatsapp_phone)

    if not contact:
        logger.info(
            "[%s] No client found for WhatsApp phone %s.",
            whatsapp_account,
            whatsapp_phone,
        )

        return None

    contact_type = (contact.get("contact_type") or "").strip().lower()

    if contact_type != "client":
        logger.info(
            "[%s] WhatsApp phone %s belongs to non-client contact.",
            whatsapp_account,
            whatsapp_phone,
        )

        return None

    contact["whatsapp_phone"] = whatsapp_phone

    logger.info(
        "[%s] Client identified by WhatsApp phone %s: %s (normal phone=%s,"
        " card=%s)",
        whatsapp_account,
        whatsapp_phone,
        contact.get("fullname"),
        contact.get("phone"),
        contact.get("trello_card_id"),
    )

    return contact


# ============================================================
# GET or FETCH TRELLO CARD
# ============================================================


def get_or_fetch_trello_card_id(phone, fullname):
    """Resolve a Trello card by WhatsApp phone only.

    ``fullname`` is retained in the signature for compatibility, but is never
    used as the identity key. This prevents similarly named clients from being
    mapped to the wrong Trello card.
    """
    if not phone:
        logger.warning(
            "Cannot resolve Trello card without a WhatsApp phone; name matching disabled: %r",
            fullname,
        )
        return None

    phone = normalize_phone(phone)
    contact = get_contact_by_phone(phone)
    if contact and contact.get("trello_card_id"):
        return contact["trello_card_id"]

    query = phone
    if not query:
        return None

    search_url = "https://api.trello.com/1/search"
    params = {
        "query": query,
        "modelTypes": "cards",
        "key": TRELLO_API_KEY,
        "token": TRELLO_TOKEN,
    }

    try:
        response = requests.get(search_url, params=params, timeout=10)
        if response.status_code == 200:
            cards = response.json().get("cards", [])
            if cards:
                card_id = cards[0]["id"]
                update_contact_trello_card_id(phone, card_id)
                return card_id
    except Exception as e:
        logger.error("Trello API search error for '%s': %s", query, e)

    return None


# ============================================================
# WHATSAPP CHAT STATE HELPERS
# ============================================================


def get_chat_row_by_name(page, chat_name):
    if not chat_name:
        return None

    selectors = [
        '[data-testid="cell-frame-title"]',
        '[data-testid="cell-frame-title-name"]',
    ]

    for selector in selectors:
        try:
            titles = page.locator(selector)

            for i in range(titles.count()):
                try:
                    title = titles.nth(i)

                    text = title.inner_text(timeout=1000).strip()

                    if text == chat_name:
                        container = title.locator(
                            'xpath=ancestor::*[@data-testid="cell-frame-container"][1]'
                        )

                        if container.count() > 0:
                            return container

                except Exception:
                    continue

        except Exception:
            continue

    return None


def find_mark_as_unread_menu_item(page):
    selectors = [
        '[role="menuitem"]',
        '[role="button"]',
        '[data-testid="mi-mark-unread"]',
        '[data-testid*="unread"]',
    ]

    for selector in selectors:
        try:
            elements = page.locator(selector)

            for i in range(elements.count()):
                try:
                    element = elements.nth(i)

                    if not element.is_visible():
                        continue

                    text = ""

                    try:
                        text = element.inner_text(timeout=500).strip()
                    except Exception:
                        pass

                    aria = (element.get_attribute("aria-label") or "").strip()

                    title = (element.get_attribute("title") or "").strip()

                    combined = f"{text} {aria} {title}".lower()

                    if (
                        "mark as unread" in combined
                        or "mark unread" in combined
                    ):
                        return element

                except Exception:
                    continue

        except Exception:
            continue

    return None


def mark_chat_as_unread(page, chat_name):
    try:
        page.keyboard.press("Escape")

        page.wait_for_timeout(300)

    except Exception:
        pass

    row = get_chat_row_by_name(page, chat_name)

    if not row:
        return False

    try:
        row.click(button="right", timeout=3000)

        page.wait_for_timeout(500)

    except Exception as e:
        logger.warning("Right-click failed for '%s': %s", chat_name, e)

        return False

    menu_item = find_mark_as_unread_menu_item(page)

    if not menu_item:
        page.keyboard.press("Escape")

        return False

    try:
        menu_item.click(timeout=3000)

        page.wait_for_timeout(700)

        return True

    except Exception as e:
        logger.warning("Could not mark '%s' as unread: %s", chat_name, e)

        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

        return False


def return_to_chat_list(page):
    try:
        page.keyboard.press("Escape")

        page.wait_for_timeout(300)

    except Exception:
        pass


# ============================================================
# OPEN CHAT
# ============================================================


def safe_click_chat_row(page, chat_name: str, timeout_ms: int = 3000) -> bool:
    """
    Resiliently clicks a chat row in #pane-side using substring matching
    and JS event dispatching.
    """
    if not chat_name:
        return False

    escaped_name = chat_name.replace('"', '\\"').replace("'", "\\'")

    # Primary Playwright locators using flexible matching
    locators = [
        page.locator(f'#pane-side span[title="{escaped_name}"]'),
        # Avoid exact=True because unread counts or timestamps pollute node innerText
        page.locator('#pane-side div[role="row"], #pane-side div[role="gridcell"]').filter(has_text=chat_name),
        page.locator(f'#pane-side [aria-label*="{escaped_name}"]'),
    ]

    for loc in locators:
        try:
            if loc.count() > 0:
                target = loc.first
                target.scroll_into_view_if_needed(timeout=1000)
                target.click(timeout=timeout_ms, force=True)
                return True
        except Exception:
            continue

    # Native JS Fallback
    try:
        clicked = page.evaluate(
            """(targetName) => {
            const spans = Array.from(document.querySelectorAll('#pane-side span[title], #pane-side span[dir="auto"]'));
            const match = spans.find(s => {
                const txt = s.getAttribute('title') || s.textContent || '';
                return txt.trim().includes(targetName) || targetName.includes(txt.trim());
            });
            if (match) {
                const row = match.closest('[role="gridcell"], [role="row"], div._ak8l') || match;
                row.scrollIntoView({ block: 'center' });
                row.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                row.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                row.click();
                return true;
            }
            return false;
        }""",
            chat_name,
        )
        return bool(clicked)
    except Exception as js_err:
        logger.warning("JS fallback click failed for '%s': %s", chat_name, js_err)

    return False


def identify_open_client(account_id, page) -> dict | None:
    """Identify ONLY the conversation currently opened by the operator."""
    try:
        logger.info("[%s] OPEN CHAT DEBUG: identify_open_client entered", account_id)

        # 1. Check WhatsApp readiness. #main is diagnostic only.
        try:
            for attempt in range(4):
                main_count = page.locator('#main').count()
                role_main_count = page.locator('div[role="main"]').count()
                shell_count = page.locator(
                    '#app, #pane-side, [data-testid*="conversation-panel"], '
                    '[data-testid*="conversation-header"]'
                ).count()
                logger.info(
                    "[%s] OPEN CHAT DEBUG: readiness attempt=%d #main=%d role-main=%d shell=%d",
                    account_id, attempt + 1, main_count, role_main_count, shell_count
                )
                if shell_count > 0:
                    break
                page.wait_for_timeout(500)
        except Exception as e:
            logger.info("[%s] OPEN CHAT DEBUG: readiness inspection failed: %s", account_id, e)

        # 2. Retrieve open chat header
        header_data = get_open_chat_header(page, account_id)
        logger.info("[%s] OPEN CHAT DEBUG: get_open_chat_header returned=%r", account_id, header_data)

        # 3. STRICT OPERATOR-OPEN MODE:
        # This listener monitors ONLY the conversation the operator currently has
        # open. A missing header is never a reason to inspect unread chats or
        # click the sidebar.
        if not header_data or not isinstance(header_data, dict):
            logger.info(
                "[%s] OPEN CHAT DEBUG: No currently open operator chat; "
                "NOT switching/selecting any other chat", account_id
            )
            return None

        # Direct exit if still no valid header pane
        if not header_data or not isinstance(header_data, dict):
            return None

        chat_name = (header_data.get('chat_name') or '').strip()
        phone = header_data.get('phone')

        # 4. EARLY EXIT: Check ignored titles BEFORE expensive DOM lookups
        if not chat_name or is_ignored_chat_title(chat_name):
            logger.info("[%s] OPEN CHAT DEBUG: Ignored or empty chat title %r", account_id, chat_name)
            return None

        if header_data.get('is_group'):
            logger.info("[%s] OPEN CHAT DEBUG: Group chat detected; skipping", account_id)
            return None

        logger.info("[%s] OPEN CHAT DEBUG: Processing active client chat_name=%r initial_phone=%r", account_id, chat_name, phone)

        # 5. Phone Normalization & Fallbacks
        if phone:
            phone = normalize_phone(phone)

        # Fallback 0: resolve an open chat by its verified WhatsApp header name
        # BEFORE doing expensive JID/drawer probing. This is safe because this
        # path runs only after the authoritative currently-open chat header has
        # been established and only matches an existing Trello/DB client.
        # It fixes clients such as 'Zewdu Kebede Araya' whose WhatsApp header
        # does not expose the phone number in the DOM, while still refusing
        # ambiguous/non-client matches.
        if not phone and chat_name:
            try:
                named_client = lookup_client_by_verified_name(chat_name)
            except Exception as e:
                named_client = None
                logger.warning(
                    "[%s] OPEN CHAT DEBUG: verified-name lookup raised for %r: %s",
                    account_id, chat_name, e,
                )

            if named_client:
                resolved_phone = named_client.get("whatsapp_phone")
                try:
                    resolved_phone = normalize_phone(resolved_phone) if resolved_phone else None
                except Exception:
                    resolved_phone = None

                if resolved_phone and resolved_phone not in WORK_PHONE_NUMBERS:
                    logger.info(
                        "[%s] OPEN CHAT DEBUG: Matched existing Trello client by verified name FIRST: "
                        "name=%r phone=%r card=%r",
                        account_id,
                        named_client.get("fullname") or chat_name,
                        resolved_phone,
                        named_client.get("trello_card_id"),
                    )
                    try:
                        save_chat_phone(account_id, chat_name, resolved_phone)
                    except Exception as e:
                        logger.debug(
                            "[%s] OPEN CHAT DEBUG: Could not save verified-name phone cache: %s",
                            account_id, e,
                        )
                    return {
                        "chat_name": chat_name,
                        "phone": resolved_phone,
                        "whatsapp_phone": named_client.get("whatsapp_phone") or resolved_phone,
                        "is_group": False,
                        "fullname": named_client.get("fullname"),
                        "trello_card_id": named_client.get("trello_card_id"),
                        "contact_type": named_client.get("contact_type"),
                        "airtable_id": named_client.get("airtable_id"),
                        "resolved_by": "verified_name_first",
                    }

        # Fallback A: Chat row JID extraction
        if not phone:
            try:
                phone = get_cached_phone(account_id, chat_name)
                if phone:
                    phone = normalize_phone(phone)
                    logger.info("[%s] OPEN CHAT DEBUG: phone resolved from cache: %s", account_id, phone)
            except Exception as e:
                logger.debug("[%s] OPEN CHAT DEBUG: cache lookup failed: %s", account_id, e)

        # Fallback A: Chat row JID extraction
        if not phone:
            logger.info("[%s] OPEN CHAT DEBUG: Trying chat-row/JID lookup for %r", account_id, chat_name)
            try:
                row = get_chat_row_by_name(page, chat_name)
                if row:
                    candidates = []
                    try: candidates.append(row.evaluate('(el) => el.outerHTML') or '')
                    except Exception: pass
                    for attr in ('data-id', 'aria-label', 'title'):
                        try:
                            v = row.get_attribute(attr)
                            if v: candidates.append(v)
                        except Exception: pass
                    try:
                        v = row.inner_text(timeout=1000)
                        if v: candidates.append(v)
                    except Exception: pass
                    for candidate in candidates:
                        # First look for canonical WhatsApp JIDs.
                        jid_matches = re.findall(
                            r'(?<!\d)(\d{8,15})(?::\d+)?@(?:c\.us|s\.whatsapp\.net)',
                            candidate,
                            flags=re.I,
                        )
                        for jid in jid_matches:
                            p = normalize_phone(jid)
                            if p and p not in WORK_PHONE_NUMBERS:
                                phone = p
                                logger.info(
                                    "[%s] OPEN CHAT DEBUG: extracted phone %s from WhatsApp JID in chat row",
                                    account_id, p,
                                )
                                break
                        if phone:
                            break

                        # Some WhatsApp builds expose the JID as a bare numeric
                        # data-id (for example true_447... / false_447...) or
                        # inside an href/aria/title rather than an @c.us JID.
                        # Inspect attribute-shaped fragments before considering
                        # looser text matches.
                        for bare in re.findall(
                            r'(?<!\d)(?:true_|false_|status_)?(\d{10,15})(?!\d)',
                            candidate,
                            flags=re.I,
                        ):
                            p = normalize_phone(bare)
                            if p and p not in WORK_PHONE_NUMBERS and len(p) >= 11:
                                phone = p
                                logger.info(
                                    "[%s] OPEN CHAT DEBUG: extracted phone %s from numeric WhatsApp row identifier",
                                    account_id, p,
                                )
                                break
                        if phone:
                            break
            except Exception as e:
                logger.info("[%s] OPEN CHAT DEBUG: Chat-row lookup failed: %s", account_id, e)

        # Fallback B: Direct title phone string
        if not phone:
            # Only treat the title itself as a phone when it actually contains
            # a plausible full phone number. In particular, never turn dates
            # such as 07/07/2026 into the bogus phone 07072026.
            if not re.fullmatch(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', chat_name):
                digits = re.sub(r"\D", "", chat_name or "")
                if len(digits) >= 10:
                    direct = normalize_phone(chat_name)
                else:
                    direct = None
            else:
                direct = None
            if direct:
                phone = direct
                logger.info("[%s] OPEN CHAT DEBUG: Title resolved directly as phone=%s", account_id, phone)

        # Fallback C: Profile Drawer
        if not phone:
            logger.info("[%s] OPEN CHAT DEBUG: Trying contact-info drawer lookup", account_id)
            try:
                # On some builds the clickable title is the only reliable way
                # to open contact info.  Do not use a generic header button:
                # that can be the menu/actions button.
                title_button = page.locator(
                    '[data-testid="conversation-header"] span[dir="auto"]'
                ).first
                if title_button.count() and title_button.is_visible():
                    try:
                        title_button.click(timeout=1500)
                        page.wait_for_timeout(700)
                    except Exception:
                        pass
                phone = extract_phone_from_drawer(page)
            except Exception as e:
                logger.info("[%s] OPEN CHAT DEBUG: Drawer lookup failed: %s", account_id, e)

        # Fallback D intentionally removed: SQLite name lookup is unsafe for
        # WhatsApp identity. A phone/JID must come from WhatsApp itself.

        if not phone:
            logger.warning(
                "[%s] OPEN CHAT DEBUG: Chat=%r opened but NO AUTHORITATIVE WHATSAPP PHONE/JID "
                "could be resolved; refusing name-based client matching",
                account_id,
                chat_name,
            )
            return None

        logger.info("[%s] Successfully identified WhatsApp client: name=%r phone=%r", account_id, chat_name, phone)
        return {'chat_name': chat_name, 'phone': phone}

    except Exception as e:
        logger.exception("[%s] Error identifying open client: %s", account_id, e)
        return None


def open_unread_chat(page, chat: dict) -> bool:
    """
    Opens an unread chat using container references or safe row clicking.
    """
    chat_name = (chat.get("chat_name") or "").strip()
    container = chat.get("container")

    if _is_unread_filter_control(chat_name, chat.get("raw_text") or chat_name):
        logger.info("UNREAD OPEN GUARD: refusing filter-control/status row %r", chat_name)
        return False

    # 1. Try provided Playwright container locator if available
    if container:
        try:
            container.scroll_into_view_if_needed(timeout=1000)
            container.click(timeout=3000, force=True)
            return True
        except Exception as e:
            logger.warning(
                "Direct container click failed for '%s', switching to row fallback: %s",
                chat_name, e
            )

    # 2. Use resilient row clicker
    if chat_name:
        return safe_click_chat_row(page, chat_name)

    return False


def _safe_mark_unread(whatsapp_account, page, chat_name: str):
    """
    Restores unread state via context menu if processing fails or client is ignored.
    """
    if not chat_name:
        return

    try:
        # Locate chat element
        loc = page.locator(f'#pane-side span[title="{chat_name}"]').first
        if loc.count() == 0:
            return

        # Right click to open context menu
        loc.click(button="right", timeout=2000, force=True)
        page.wait_for_timeout(300)

        # Click "Mark as unread" option
        unread_btn = page.locator(
            'li[data-testid="mi-mark-unread"], li:has-text("Mark as unread")'
        ).first
        if unread_btn.count() > 0:
            unread_btn.click(timeout=2000)
            logger.info("[%s] Marked chat '%s' back as unread.", whatsapp_account, chat_name)
    except Exception as e:
        logger.warning("[%s] Could not mark chat '%s' as unread: %s", whatsapp_account, chat_name, e)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

def get_message_timestamp(message_element):
    try:
        pre_plain = message_element.locator('[data-pre-plain-text]').first

        if pre_plain.count() > 0:
            return pre_plain.get_attribute("data-pre-plain-text")

    except Exception:
        pass

    return None


# ============================================================
# GET UNREAD INCOMING MESSAGES
# ============================================================


def get_unread_incoming_messages(page, expected_count: int = 1):
    """Scans active chat pane for incoming messages, correctly extracting message bodies and direction."""
    try:
        main_pane = page.locator('#main, div[role="main"]').first
        if not main_pane.is_visible():
            logger.warning("get_unread_incoming_messages: #main pane not visible.")
            return []

        # Target rows within the main pane using resilient DOM selectors
        msg_locators = main_pane.locator('div[role="row"], div[data-id], div.message-in, div.message-out')
        count = msg_locators.count()
        logger.info("DEBUG: message locator count = %d", count)

        if count == 0:
            return []

        extracted_messages = []
        start_idx = max(0, count - max(expected_count, 10))

        for idx in range(start_idx, count):
            loc = msg_locators.nth(idx)
            
            # Extract content directly from the DOM row element
            msg_data = page.evaluate("""(el) => {
                // Find parent container or self
                const container = el.closest('div.message-in, div.message-out') || el;
                const isIncoming = container.classList.contains('message-in') || !!container.querySelector('div.message-in');
                const isOutgoing = container.classList.contains('message-out') || !!container.querySelector('div.message-out');

                // Determine direction
                let direction = 'unknown';
                if (isIncoming) direction = 'in';
                else if (isOutgoing) direction = 'out';

                // Extract text body
                const textSpan = container.querySelector('span.selectable-text, span._ao3e, span[dir="ltr"]');
                const text = textSpan ? textSpan.innerText.trim() : container.innerText.trim();

                // Extract timestamp metadata
                const timeNode = container.querySelector('div[data-pre-plain-text], span[data-testid="msg-meta"]');
                const timestamp = timeNode ? (timeNode.getAttribute('data-pre-plain-text') || timeNode.innerText) : '';

                return { text, timestamp, direction };
            }""", loc.element_handle())

            if not msg_data:
                continue

            # Allow incoming messages ('in')
            # If sending messages from your phone and testing, change `== "in"` to `in ("in", "out")`
            if msg_data.get("direction") == "in" and msg_data.get("text"):
                extracted_messages.append({
                    "text": msg_data["text"],
                    "timestamp": msg_data.get("timestamp", ""),
                    "direction": "in"
                })

        logger.info("get_unread_incoming_messages(): detected %d valid message(s).", len(extracted_messages))
        return extracted_messages

    except Exception as e:
        logger.exception("Error extracting chat messages: %s", e)
        return []


# ============================================================
# PROCESS ONE MESSAGE
# ============================================================


def process_message(
    whatsapp_account, page, phone, message_info, contact=None
):
    """Process one exact WhatsApp message with persistent idempotency."""
    if not phone or not message_info:
        return {"processed": False, "client": False}

    canonical_phone = canonical_whatsapp_phone(contact, phone)
    if not canonical_phone:
        return {"processed": False, "client": False}

    message = message_info.get("text")
    timestamp = message_info.get("timestamp")
    direction = message_info.get("direction", "incoming")
    message_element = message_info.get("element")
    if not message and not message_element:
        return {"processed": False, "client": False}

    message_id = message_info.get("message_id")
    attachment_identity = message_info.get("attachment_identity") or ""
    message_key = message_info.get("message_key") or build_message_identity(
        whatsapp_account=whatsapp_account,
        phone=canonical_phone,
        message=message,
        direction=direction,
        message_id=message_id,
        timestamp=timestamp,
        attachment_identity=attachment_identity,
    )
    message_hash = build_hash(
        whatsapp_account, canonical_phone, message, timestamp, extra=direction,
        message_id=message_id,
        attachment_identity=attachment_identity,
        message_key=message_key,
    )

    recovery_unread = bool(message_info.get("recovery_unread"))

    # V22 stable identity guard: a legacy V20 hash may differ when WhatsApp
    # later reveals a full data-pre-plain-text timestamp. Reconcile against the
    # stable identity/content record before considering a message new.
    stable_already_done = message_identity_exists(message_key)
    if not stable_already_done and not recovery_unread:
        stable_already_done = legacy_message_content_exists(
            whatsapp_account, canonical_phone, message, direction, timestamp
        )

    if stable_already_done and not recovery_unread and not message_has_attachment(message_element) :
        logger.info(
            "[%s] V22 MESSAGE IDENTITY: previously persisted logical message; skipping duplicate even though DOM hash changed: key=%s text=%r",
            whatsapp_account, message_key, message
        )
        return {"processed": True, "client": True, "duplicate": True}

    if already_processed(message_hash) and not recovery_unread:
        has_attachment = bool(
            message_element and message_has_attachment(message_element)
        )
        attachment_complete = (
            has_attachment
            and attachment_upload_completed_for_message(
                whatsapp_account, message_hash
            )
        )

        if not (has_attachment and not attachment_complete):
            logger.info(
                "[%s] MESSAGE DUPLICATE: already processed; skipping Trello write: %r",
                whatsapp_account, message_hash,
            )
            return {"processed": True, "client": True, "duplicate": True}

        # The message was previously marked processed, but its attachment was
        # never completed. Remove that stale terminal marker BEFORE attempting
        # a new claim; otherwise claim_message_for_processing() will reject the
        # retry forever.
        logger.info(
            "[%s] Existing processed message has incomplete attachment state; "
            "clearing stale processed marker and forcing attachment retry: %r",
            whatsapp_account, message_hash,
        )
        clear_processed_marker(message_hash)

    elif recovery_unread:
        # The operator deliberately marked this chat/message unread to request
        # reconciliation.  Do not let the old processed marker suppress the
        # recovery attempt.  Trello comment deduplication and Drive attachment
        # hashing remain the safety nets for already-completed work.
        logger.info(
            "[%s] UNREAD RECOVERY: overriding processed marker for explicit "
            "recovery request: %r",
            whatsapp_account, message_hash,
        )
        clear_processed_marker(message_hash)

    if not claim_message_for_processing(message_hash, whatsapp_account):
        if already_processed(message_hash):
            logger.info(
                "[%s] MESSAGE CLAIM: message completed by another worker; "
                "skipping duplicate Trello write: %r",
                whatsapp_account, message_hash,
            )
            return {"processed": True, "client": True, "duplicate": True}

        # A live processing_messages row means another poller currently owns
        # this attempt. This is NOT success and must remain retryable so that a
        # failed attachment can be retried on a later poll.
        logger.info(
            "[%s] MESSAGE CLAIM: attachment/message is currently in-flight; "
            "leaving retryable and skipping this concurrent attempt: %r",
            whatsapp_account, message_hash,
        )
        return {
            "processed": False,
            "client": True,
            "duplicate": False,
            "in_progress": True,
            "retryable": True,
        }

    try:
        return _process_message_claimed(
            whatsapp_account=whatsapp_account,
            page=page,
            phone=canonical_phone,
            message_info=message_info,
            contact=contact,
            message_hash=message_hash,
        )
    finally:
        release_message_processing(message_hash)


def _process_message_claimed(
    whatsapp_account, page, phone, message_info, contact=None, message_hash=None
):
    if not phone or not message_info:
        return {"processed": False, "client": False}

    canonical_phone = canonical_whatsapp_phone(contact, phone)
    if not canonical_phone:
        return {"processed": False, "client": False}
    phone = canonical_phone

    message = message_info.get("text")
    timestamp = message_info.get("timestamp")
    direction = message_info.get("direction", "incoming")
    message_element = message_info.get("element")
    attachment_detected_hint = bool(message_info.get("attachment_detected"))

    if not message and not message_element:
        return {"processed": False, "client": False}

    if not message_hash:
        message_hash = build_hash(
            whatsapp_account, phone, message, timestamp, extra=direction,
            message_id=message_info.get("message_id"),
            attachment_identity=message_info.get("attachment_identity") or "",
            message_key=message_info.get("message_key"),
        )

    if contact is None:
        contact = lookup_client_by_phone_or_whatsapp(phone)

        logger.info(
            "[%s] Client lookup for WHATSAPP phone %s returned: %r",
            whatsapp_account,
            phone,
            contact,
        )

        if not contact:
            logger.warning(
                "[%s] No client found for phone %s. Message will NOT be added "
                "to Trello.",
                whatsapp_account,
                phone,
            )
            return {"processed": False, "client": False}

    contact_type = (contact.get("contact_type") or "").strip().lower()
    if contact_type != "client":
        return {"processed": False, "client": False}

    # Preserve both fields distinctly:
    #   phone           = CRM/UK number
    #   whatsapp_phone  = canonical WhatsApp identity
    contact["whatsapp_phone"] = phone

    card_id = contact.get("trello_card_id")
    if not card_id:
        logger.warning(
            "[%s] Client %s has no Trello card.", whatsapp_account, phone
        )
        return {"processed": False, "client": True, "no_card": True}

    fullname = contact.get("fullname") if contact else None

    # IMPORTANT: do NOT create/ensure a Drive folder here.
    # A Drive folder must only be created as part of a successful attachment
    # upload. Otherwise text messages or failed attachment retries can leave
    # empty client folders behind.

    # ------------------------------------------------------------
    # Attachment processing
    # ------------------------------------------------------------
    attachment_result = {
        "success": True,
        "attachment_detected": False,
        "attachments": [],
    }

    try:
        if message_element:
            attachment_result = process_message_attachments(
                whatsapp_account=whatsapp_account,
                page=page,
                phone=phone,
                contact=contact,
                message_info=message_info,
                message_element=message_element,
                message_hash=message_hash,
            )

            # HARD GATE:
            # attachment detected + no confirmed Drive upload/link
            # => do not write Trello, do not save message, do not mark processed.
            if (
                attachment_result.get("attachment_detected")
                and not attachment_result.get("success")
            ):
                logger.warning(
                    "[%s] Attachment detected for message_hash=%s but "
                    "Drive upload/link is incomplete; NOT writing Trello "
                    "comment. Message remains retryable.",
                    whatsapp_account,
                    message_hash,
                )
                return {
                    "processed": False,
                    "client": True,
                    "attachment_retry": True,
                    "attachment_error": True,
                }

    except Exception as e:
        logger.exception(
            "[%s] Attachment processing failed for client %s: %s",
            whatsapp_account,
            phone,
            e,
        )
        return {
            "processed": False,
            "client": True,
            "attachment_retry": True,
            "attachment_error": True,
        }

    attachments = attachment_result.get("attachments") or []

    # Only now, after a confirmed attachment upload, expose the Drive folder
    # link on the Trello card. The folder URL comes from the actual upload
    # result, so an empty/pre-created folder can never be advertised here.
    if attachments:
        uploaded_folder_url = next(
            (item.get("folder_url") for item in attachments if item.get("folder_url")),
            None,
        )

        # A duplicate attachment may have been uploaded previously and its
        # current attachment record may not carry the folder URL. In that case
        # do not create a new folder merely to populate Trello. The existing
        # attachment/file link remains sufficient.
        if uploaded_folder_url:
            logger.info(
                "[%s] ATTACHMENT UPLOAD CONFIRMED: exposing canonical Drive folder on Trello: %s",
                whatsapp_account,
                uploaded_folder_url,
            )
            uploaded_folder_id = next(
                (item.get("folder_id") for item in attachments if item.get("folder_id")),
                None,
            )
            add_drive_folder_link_to_trello_card(
                card_id=card_id,
                folder_url=uploaded_folder_url,
                phone=phone,
                folder_id=uploaded_folder_id,
            )

    # Final defensive gate: never let a detected attachment reach Trello with
    # an empty attachment payload.
    if (
        ((message_element and message_has_attachment(message_element))
         or attachment_detected_hint)
        and not attachments
    ):
        logger.warning(
            "[%s] Attachment message reached Trello stage without a "
            "confirmed Drive attachment; refusing to write.",
            whatsapp_account,
        )
        return {
            "processed": False,
            "client": True,
            "attachment_retry": True,
            "attachment_error": True,
        }

    logger.info(
        "[%s] ABOUT TO WRITE WHATSAPP MESSAGE TO TRELLO: phone=%s "
        "card_id=%s direction=%s message=%r attachments=%d",
        whatsapp_account,
        phone,
        card_id,
        direction,
        message,
        len(attachments),
    )

    trello_updated = update_client_trello_card_from_whatsapp(
        whatsapp_account=whatsapp_account,
        contact=contact,
        message=message,
        direction=direction,
        attachments=attachments,
        timestamp=timestamp,
    )

    logger.info(
        "[%s] TRELLO WRITE RESULT: phone=%s card_id=%s result=%s",
        whatsapp_account,
        phone,
        card_id,
        trello_updated,
    )

    if not trello_updated:
        return {"processed": False, "client": True, "trello_error": True}

    save_message(
        whatsapp_account=whatsapp_account,
        phone=phone,
        message=message,
        direction=direction,
        whatsapp_timestamp=timestamp,
        identity_key=message_info.get("message_key"),
        message_id=message_info.get("message_id"),
    )

    mark_processed(message_hash, whatsapp_account)

    return {
        "processed": True,
        "client": True,
        "attachments": len(attachments),
    }


# ============================================================
# SAFE CLICK CHAT ROW
# ============================================================
def process_unread_chat(whatsapp_account, page, chat):
    """
    Process an unread WhatsApp chat using phone number as primary client identifier.
    
    Ignores non-client group chats, staff channels, and system titles early.
    Only processes messages for clients existing in the Trello SQLite database.
    """
    chat_name = (chat.get("chat_name") or "").strip()
    logger.info("UNREAD RECOVERY ENTER: chat=%r phone=%r unread_count=%s", chat_name, chat.get("phone"), chat.get("unread_count", 1))

    if _is_unread_filter_control(chat_name, chat.get("raw_text") or chat_name):
        logger.info("[%s] UNREAD RECOVERY GUARD: ignoring filter-control/status chat=%r", whatsapp_account, chat_name)
        return {"client": False, "ignored_filter_control": True}

    # 1. Early Group & Non-Client Title Exclusion
    if is_ignored_chat_title(chat_name):
        logger.info(
            "[%s] Chat '%s' matches ignored group/staff/system title. Skipping.",
            whatsapp_account, chat_name
        )
        return {"client": False, "ignored_group": True}

    current_phone = (chat.get("phone") or "").strip()
    unread_count = max(1, int(chat.get("unread_count", 1)))

    logger.info(
        "[%s] Checking unread chat '%s' (%d unread)",
        whatsapp_account, chat_name, unread_count
    )

    phone = normalize_phone(current_phone) if current_phone else None
    chat_is_opened = False

    # 2. Resolve phone number if missing from list metadata
    if not phone:
        logger.warning(
            "[%s] Unread chat '%s' has no phone number. Opening chat to resolve.",
            whatsapp_account, chat_name
        )

        if not open_unread_chat(page, chat):
            logger.error("[%s] Could not open unread chat '%s'.", whatsapp_account, chat_name)
            return {"client": False, "open_failed": True}

        chat_is_opened = True
        page.wait_for_timeout(1000)  # Wait for header rendering

        # Attempt 1: Scrape header title bar
        try:
            open_contact = identify_open_client(whatsapp_account, page)
            if isinstance(open_contact, dict):
                resolved_phone = (open_contact.get("phone") or "").strip()
                if resolved_phone:
                    phone = normalize_phone(resolved_phone)
                    logger.info(
                        "[%s] Resolved phone from header for '%s': %s",
                        whatsapp_account, chat_name, phone
                    )
        except Exception as e:
            logger.exception("[%s] Error identifying opened chat header for '%s': %s", whatsapp_account, chat_name, e)

        # Attempt 2: Open Contact Info Drawer if header extraction returned no phone
        if not phone:
            logger.info(
                "[%s] Phone missing from header for '%s'. Attempting Contact Drawer scan...",
                whatsapp_account, chat_name
            )
            phone = resolve_chat_phone_with_cache(page, chat_name)
            if phone:
                logger.info(
                    "[%s] Resolved phone from Contact Drawer for '%s': %s",
                    whatsapp_account, chat_name, phone
                )

    # 3. Abort if phone could not be resolved or belongs to work phone list
    if not phone:
        logger.warning(
            "[%s] Could not resolve phone for unread chat '%s'. Leaving untouched.",
            whatsapp_account, chat_name
        )
        _safe_mark_unread(whatsapp_account, page, chat_name)
        return {"client": False, "phone_missing": True}

    if phone in WORK_PHONE_NUMBERS:
        logger.info(
            "[%s] Phone %s for chat '%s' is an internal work line. Skipping.",
            whatsapp_account, phone, chat_name
        )
        return {"client": False, "internal_work_phone": True}

    # 4. Perform Trello/Client Lookup in SQLite Database
    try:
        contact = lookup_client_by_phone_or_whatsapp(phone)
    except Exception as e:
        logger.exception("[%s] Client lookup failed for phone %s: %s", whatsapp_account, phone, e)
        return {"client": False, "lookup_error": True}

    if not contact:
        logger.warning(
            "[%s] No Trello client found in SQLite DB for phone %s ('%s'). Leaving untouched.",
            whatsapp_account, phone, chat_name
        )
        _safe_mark_unread(whatsapp_account, page, chat_name)
        return {"client": False, "phone": phone, "not_in_trello": True}

    # 5. Verify Contact Type
    contact_type = (contact.get("contact_type") or "").strip().lower()
    if contact_type != "client":
        logger.info(
            "[%s] Phone %s belongs to type '%s' (not 'client'). Skipping.",
            whatsapp_account, phone, contact_type
        )
        return {"client": False, "wrong_contact_type": True, "phone": phone}

    # Lock resolved phone to contact dictionary
    contact["whatsapp_phone"] = phone
    logger.info(
        "[%s] Identified WhatsApp client '%s' (%s). Trello Card: %s",
        whatsapp_account, contact.get("fullname"), phone, contact.get("trello_card_id")
    )

    # 6. Open chat if not already opened in Step 2
    if not chat_is_opened:
        if not open_unread_chat(page, chat):
            logger.error("[%s] Could not open client chat '%s'.", whatsapp_account, chat_name)
            return {"client": True, "open_failed": True}

    # 7. Extract the explicit unread/recovery portion from the real message DOM.
    # This supports both incoming and outgoing messages and, unlike the legacy
    # incoming-only extractor, can recover attachments as well.
    logger.info("UNREAD RECOVERY SCAN START: chat=%r phone=%s unread_count=%s", chat_name, phone, unread_count)
    messages = get_new_messages(
        whatsapp_account=whatsapp_account,
        phone=phone,
        page=page,
        recovery_unread=True,
        unread_count=unread_count,
    )
    logger.info("UNREAD RECOVERY SCAN END: chat=%r messages=%d", chat_name, len(messages or []))
    if not messages:
        logger.warning("[%s] No unread/recovery messages extracted from '%s'.", whatsapp_account, chat_name)
        _safe_mark_unread(whatsapp_account, page, chat_name)
        return {"client": True, "phone": phone, "extraction_failed": True}

    # 8. Process Incoming Messages
    successful, duplicates, failures = 0, 0, 0

    for index, message_info in enumerate(messages, start=1):
        logger.info(
            "[%s] Processing message %d/%d from '%s' (%s)",
            whatsapp_account, index, len(messages), chat_name, phone
        )
        try:
            result = process_message(
                whatsapp_account=whatsapp_account,
                page=page,
                phone=phone,
                message_info=message_info,
                contact=contact
            )
            
            if result.get("processed"):
                if result.get("duplicate"):
                    duplicates += 1
                else:
                    successful += 1
            else:
                failures += 1

        except Exception as e:
            logger.exception("[%s] Exception on message %d from '%s': %s", whatsapp_account, index, chat_name, e)
            failures += 1

    # 9. Restore Unread State on Failure
    if failures > 0:
        logger.warning("[%s] %d message(s) failed for '%s'. Restoring unread state.", whatsapp_account, failures, chat_name)
        _safe_mark_unread(whatsapp_account, page, chat_name)
    else:
        logger.info("[%s] All messages successfully processed for '%s'.", whatsapp_account, chat_name)

    return {
        "client": True,
        "phone": phone,
        "successful": successful,
        "duplicates": duplicates,
        "failures": failures
    }

# ============================================================
# START ONE WHATSAPP ACCOUNT
# ============================================================

def run_whatsapp_account(
    account_id,
    account_config
):
    account_name = account_config["name"]
    profile_dir = account_config["profile"]

    restart_delay = ACCOUNT_RESTART_DELAY_SECONDS

    update_account_status(
        account_id,
        "STARTING"
    )

    logger.info(
        "[%s] Starting account: %s",
        account_id,
        account_name
    )

    os.makedirs(
        profile_dir,
        exist_ok=True
    )

    while not SHUTDOWN_EVENT.is_set():

        context = None

        try:

            with sync_playwright() as p:

                logger.info(
                    "[%s] Launching persistent browser.",
                    account_id
                )

                update_account_status(
                    account_id,
                    "STARTING"
                )

                profile_path = f"/home/springvolunteer/Airtable2Trello/whatsapp_profiles/{account_id}"

                # 1. Launch a visible persistent browser for operator-open-chat mode.
                # The listener must inspect the same browser window that the operator can
                # manually use to open a WhatsApp conversation. Set WHATSAPP_HEADLESS=true
                # only for unattended/background operation where no operator interaction
                # is required.
                headless_env = os.getenv("WHATSAPP_HEADLESS", "false").strip().lower()
                whatsapp_headless = headless_env in {"1", "true", "yes", "y", "on"}
                logger.info(
                    "[%s] Browser mode: %s (WHATSAPP_HEADLESS=%r)",
                    account_id, "headless" if whatsapp_headless else "headed/operator", headless_env
                )
                context = p.chromium.launch_persistent_context(
                    user_data_dir=profile_path,
                    headless=whatsapp_headless,
                    timeout=600000,
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 800},
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-software-rasterizer",
                        "--blink-settings=imagesEnabled=true",
                    ]
                )

                attach_playwright_monitoring(
                    account_id,
                    context
                )

                # 2. Reuse default tab
                page = context.pages[0] if context.pages else context.new_page()

                page.on(
                    "pageerror",
                    lambda error: logger.error(
                        "[%s] Page error: %s",
                        account_id,
                        error
                    )
                )

                logger.info(
                    "[%s] Opening WhatsApp Web...",
                    account_id
                )

                page.goto(
                    "https://web.whatsapp.com",
                    wait_until="domcontentloaded",
                    timeout=WHATSAPP_LOAD_TIMEOUT_MS
                )

                logger.info(
                    "[%s] Waiting for WhatsApp...",
                    account_id
                )

                # 3. Robust startup wait check
                try:
                    page.wait_for_selector(
                        "#pane-side, [data-testid='chat-list'], div[role='grid'], canvas[aria-label*='Scan'], [data-testid='qrcode']",
                        timeout=WHATSAPP_LOAD_TIMEOUT_MS
                    )

                    # Check for QR Code login state
                    if page.locator("canvas, [data-testid='qrcode']").count() > 0:
                        qr_filename = f"/tmp/whatsapp_{account_id}_{account_name.replace(' ', '_')}_QR.png"
                        
                        logger.warning(
                            "[%s - %s] ACTION REQUIRED: Please scan the QR code for account '%s'!",
                            account_id,
                            account_name,
                            account_name
                        )
                        
                        page.screenshot(path=qr_filename)
                        logger.info("[%s] Saved explicit QR screenshot to: %s", account_id, qr_filename)
                        
                        update_account_status(account_id, "AWAITING_QR_SCAN")
                        
                        page.wait_for_selector(
                            "#pane-side, [data-testid='chat-list'], div[role='grid']",
                            timeout=300000
                        )
                        logger.info("[%s] QR code scanned for '%s'!", account_id, account_name)

                    else:
                        logger.info("[%s] WhatsApp side pane loaded successfully for '%s'!", account_id, account_name)

                    # Account Phone Number Verification
                    expected_phone = account_config.get("phone")
                    if expected_phone:
                        logger.info("[%s] Verifying connected phone number...", account_id)
                        
                        page.click("header [role='button'][title*='Profile'], header [data-testid='default-user']")
                        
                        phone_selector = "span:has-text('+'), div[data-testid='profile-phone-number']"
                        page.wait_for_selector(phone_selector, timeout=5000)
                        
                        raw_number = page.locator(phone_selector).first.inner_text()
                        connected_phone = "".join(filter(str.isdigit, raw_number))
                        clean_expected = "".join(filter(str.isdigit, expected_phone))

                        page.keyboard.press("Escape")

                        if clean_expected not in connected_phone:
                            raise ValueError(
                                f"Account Mismatch! Config expected {clean_expected}, "
                                f"but browser is logged into {connected_phone}"
                            )
                        
                        logger.info("[%s] Account identity verified successfully (%s)!", account_id, connected_phone)

                except Exception as e:
                    logger.error("[%s] Startup or identity check failed for '%s': %s", account_id, account_name, e)
                    page.screenshot(path=f"/tmp/whatsapp_{account_id}_error.png")
                    update_account_status(account_id, "DISCONNECTED")
                    raise e

                restart_delay = ACCOUNT_RESTART_DELAY_SECONDS

                logger.info(
                    "[%s] Starting message monitor.",
                    account_id
                )

                # ============================================================
                # ACCOUNT MAIN LOOP
                # ============================================================

                while not SHUTDOWN_EVENT.is_set():

                    try:

                        logger.info("[%s] ACCOUNT LOOP HEARTBEAT: polling WhatsApp", account_id)

                        mark_account_check(
                            account_id
                        )

                        # ====================================================
                        # WHATSAPP HEALTH
                        # ====================================================

                        if not is_whatsapp_logged_in(page):

                            logger.warning(
                                "[%s] WhatsApp session is no longer healthy.",
                                account_id
                            )

                            update_account_status(
                                account_id,
                                "DISCONNECTED"
                            )

                            break

                        # ====================================================
                        # INCOMING / UNREAD CHAT PROCESSING (OPT-IN)
                        # IMPORTANT: run this BEFORE passive sidebar sync.
                        # Passive sync may temporarily open changed chats, which
                        # clears WhatsApp's unread marker before manual recovery
                        # can discover it. Manual unread is an explicit operator
                        # recovery signal and therefore gets first priority.
                        # ====================================================

                        if WHATSAPP_AUTO_UNREAD or WHATSAPP_MANUAL_UNREAD_RECOVERY:
                            logger.info("UNREAD RECOVERY GATE enabled auto=%s manual=%s", WHATSAPP_AUTO_UNREAD, WHATSAPP_MANUAL_UNREAD_RECOVERY)
                            try:
                                unread_chats = get_unread_chats(page)
                            
                                if unread_chats:
                                    logger.info("[%s] Found %d unread chats.", account_id, len(unread_chats))

                                    for chat in unread_chats:
                                        if SHUTDOWN_EVENT.is_set():
                                            break

                                        if not isinstance(chat, dict):
                                            logger.warning(
                                                "[%s] Ignoring invalid unread chat entry: %r",
                                                account_id,
                                                chat
                                            )
                                            continue

                                        chat_name = chat.get("chat_name")

                                        try:
                                            # 1. Click unread chat with fast-fail timeout & JS fallback
                                            try:
                                                chat["container"].click(timeout=3000, force=True)
                                            except Exception as click_err:
                                                logger.warning(
                                                    "[%s] Playwright click timed out for '%s', using JS fallback: %s",
                                                    account_id,
                                                    chat_name,
                                                    click_err
                                                )
                                                title_escaped = chat_name.replace('"', '\\"')
                                                page.evaluate(f'''() => {{
                                                    const span = document.querySelector('#pane-side span[title="{title_escaped}"]');
                                                    if (span) {{
                                                        const row = span.closest('[role="row"]');
                                                        if (row) {{
                                                            row.scrollIntoView({{ block: 'center' }});
                                                            row.click();
                                                        }}
                                                    }}
                                                }}''')

                                            page.wait_for_timeout(500)

                                            # 2. Process chat
                                            result = process_unread_chat(
                                                account_id,
                                                page,
                                                chat
                                            )

                                            logger.info(
                                                "[%s] Unread chat '%s' result: %r",
                                                account_id,
                                                chat_name,
                                                result
                                            )

                                        except Exception as e:
                                            logger.exception(
                                                "[%s] Error processing unread chat '%s': %s",
                                                account_id,
                                                chat_name,
                                                e
                                            )
                                            try:
                                                page.keyboard.press("Escape")
                                            except Exception:
                                                pass

                            except Exception as e:
                                logger.exception(
                                    "[%s] Incoming unread-chat processing failed: %s",
                                    account_id,
                                    e
                                )

                        # ====================================================
                        # PASSIVE PHONE-SYNC MONITOR (runs AFTER unread recovery)
                        # ====================================================

                        try:
                            monitor_sidebar_for_synced_outgoing(
                                whatsapp_account=account_id,
                                page=page,
                            )
                        except Exception as e:
                            logger.exception(
                                "[%s] Passive sidebar sync monitor failed: %s",
                                account_id,
                                e,
                            )

                        # ====================================================
                        # OPEN CHAT MESSAGE PROCESSING
                        # ====================================================

                        try:

                            current_contact = None
                            current_phone = None

                            logger.info("[%s] OPEN CHAT POLL: identifying currently open chat", account_id)

                            # Identify currently open client
                            try:
                                logger.info(
                                    "[%s] OPEN CHAT DEBUG: page URL=%s title=%r",
                                    account_id,
                                    page.url,
                                    page.title(),
                                )

                                current_contact = identify_open_client(
                                    account_id,
                                    page
                                )

                                logger.debug(
                                    "[%s] OPEN CHAT DEBUG: msg-container count=%d",
                                    account_id,
                                    page.locator('[data-testid="msg-container"]').count()
                                )

                                logger.debug(
                                    "[%s] OPEN CHAT DEBUG: message-in count=%d message-out count=%d",
                                    account_id,
                                    page.locator('div.message-in').count(),
                                    page.locator('div.message-out').count()
                                )

                                logger.debug(
                                    "[%s] OPEN CHAT DEBUG: identify_open_client returned=%r",
                                    account_id,
                                    current_contact
                                )

                            except Exception as e:

                                logger.warning(
                                    "[%s] identify_open_client() failed: %s",
                                    account_id,
                                    e
                                )

                                current_contact = None

                            if current_contact:
                                logger.info(
                                    "[%s] OPEN CHAT CLIENT: %r",
                                    account_id,
                                    current_contact
                                )

                            # Validate returned contact
                            if isinstance(current_contact, dict):

                                current_phone = current_contact.get("phone")

                                if current_phone:
                                    try:
                                        current_phone = normalize_phone(current_phone)
                                    except Exception as e:
                                        logger.warning(
                                            "[%s] Could not normalize open-chat phone %r: %s",
                                            account_id,
                                            current_phone,
                                            e
                                        )
                                        current_phone = None

                                if current_phone:
                                    real_contact = lookup_client_by_phone_or_whatsapp(
                                        current_phone
                                    )

                                    if real_contact:
                                        real_contact["chat_name"] = (
                                            current_contact.get("chat_name")
                                        )
                                        current_contact = real_contact

                                        logger.info(
                                            "[%s] Resolved full open-chat contact: "
                                            "name=%r phone=%s whatsapp_phone=%s "
                                            "card=%s type=%s",
                                            account_id,
                                            current_contact.get("fullname"),
                                            current_contact.get("phone"),
                                            current_contact.get("whatsapp_phone"),
                                            current_contact.get("trello_card_id"),
                                            current_contact.get("contact_type")
                                        )
                                    else:
                                        logger.warning(
                                            "[%s] No database client found for open-chat phone %s; "
                                            "WILL STILL SCAN CHAT FOR MESSAGE EVENTS",
                                            account_id,
                                            current_phone
                                        )
                                        # Keep a provisional contact so message detection is not
                                        # disabled by a missing/stale DB mapping. Trello writing
                                        # remains protected below by the valid-card check.
                                        current_contact = {
                                            "chat_name": current_contact.get("chat_name"),
                                            "phone": current_phone,
                                            "contact_type": "unknown",
                                            "trello_card_id": None,
                                        }

                            else:
                                current_contact = None
                                current_phone = None

                            # =================================================
                            # PROCESS OPEN CHAT
                            # =================================================

                            if current_contact and current_phone:

                                logger.info(
                                    "[%s] Monitoring open client: %s (%r)",
                                    account_id,
                                    current_phone,
                                    current_contact.get("chat_name")
                                )

                                try:

                                    new_messages = get_new_messages(
                                        whatsapp_account=account_id,
                                        phone=current_phone,
                                        page=page
                                    )

                                except Exception as e:

                                    logger.exception(
                                        "[%s] get_new_messages() failed for %s: %s",
                                        account_id,
                                        current_phone,
                                        e
                                    )

                                    new_messages = []

                                if not isinstance(new_messages, list):

                                    logger.warning(
                                        "[%s] get_new_messages() returned unexpected value: %r",
                                        account_id,
                                        new_messages
                                    )

                                    new_messages = []

                                logger.info(
                                    "[%s] Open client %s: detected %d new message(s).",
                                    account_id,
                                    current_phone,
                                    len(new_messages)
                                )

                                successful = 0
                                duplicates = 0
                                failures = 0

                                can_write_to_trello = (
                                    isinstance(current_contact, dict)
                                    and current_contact.get("trello_card_id")
                                    and (current_contact.get("contact_type") or "").strip().lower() == "client"
                                )

                                if not can_write_to_trello and new_messages:
                                    logger.warning(
                                        "[%s] MESSAGE EVENT(S) DETECTED but no valid Trello client/card "
                                        "was resolved for phone %s. Events logged only; Trello write skipped.",
                                        account_id,
                                        current_phone,
                                    )
                                    new_messages = []

                                for message_info in new_messages:

                                    if SHUTDOWN_EVENT.is_set():
                                        break

                                    if not isinstance(message_info, dict):

                                        logger.warning(
                                            "[%s] Ignoring invalid message_info: %r",
                                            account_id,
                                            message_info
                                        )

                                        failures += 1
                                        continue

                                    direction = message_info.get("direction")
                                    text = message_info.get("text")

                                    logger.info(
                                        "[%s] Processing open-chat %s message for %s: %r",
                                        account_id,
                                        direction,
                                        current_phone,
                                        text
                                    )

                                    try:

                                        result = process_message(
                                            whatsapp_account=account_id,
                                            page=page,
                                            phone=current_phone,
                                            message_info=message_info,
                                            contact=current_contact
                                        )

                                        logger.info(
                                            "[%s] Open chat %s message result: %r",
                                            account_id,
                                            direction,
                                            result
                                        )

                                        if not isinstance(result, dict):

                                            logger.warning(
                                                "[%s] process_message() returned non-dict result: %r",
                                                account_id,
                                                result
                                            )

                                            failures += 1
                                            continue

                                        if result.get("duplicate"):
                                            duplicates += 1
                                        elif result.get("processed"):
                                            successful += 1
                                        else:
                                            failures += 1

                                    except Exception as e:

                                        logger.exception(
                                            "[%s] Failed processing open-chat %s message for %s: %s",
                                            account_id,
                                            direction,
                                            current_phone,
                                            e
                                        )

                                        failures += 1

                                logger.info(
                                    "[%s] Open chat message monitor: %d processed, %d duplicates, %d failures.",
                                    account_id,
                                    successful,
                                    duplicates,
                                    failures
                                )

                            elif current_contact and not current_phone:

                                logger.warning(
                                    "[%s] Open chat contact has no resolvable phone: %r",
                                    account_id,
                                    current_contact
                                )

                        except Exception as e:

                            logger.exception(
                                "[%s] Could not monitor open chat for messages: %s",
                                account_id,
                                e
                            )

                        # Poll interval delay
                        SHUTDOWN_EVENT.wait(
                            CHECK_INTERVAL_SECONDS
                        )

                    except Exception as e:

                        logger.exception(
                            "[%s] Account loop error: %s",
                            account_id,
                            e
                        )

                        update_account_status(
                            account_id,
                            "ERROR",
                            e
                        )

                        break

                # Account Loop Exit
                if SHUTDOWN_EVENT.is_set():

                    update_account_status(
                        account_id,
                        "STOPPING"
                    )

                else:

                    logger.warning(
                        "[%s] Account loop exited. Browser will be restarted.",
                        account_id
                    )

                try:

                    context.close()

                except Exception as e:

                    logger.warning(
                        "[%s] Error closing browser: %s",
                        account_id,
                        e
                    )

                context = None

        except KeyboardInterrupt:

            request_shutdown()
            break

        except Exception as e:

            logger.exception(
                "[%s] WhatsApp account crashed: %s",
                account_id,
                e
            )

            update_account_status(
                account_id,
                "ERROR",
                e
            )

        finally:

            if context is not None:

                try:

                    context.close()

                except Exception:
                    pass

        if SHUTDOWN_EVENT.is_set():
            break

        logger.warning(
            "[%s] Restarting WhatsApp in %d seconds.",
            account_id,
            restart_delay
        )

        SHUTDOWN_EVENT.wait(
            restart_delay
        )

        restart_delay = min(
            restart_delay * 2,
            MAX_ACCOUNT_RESTART_DELAY_SECONDS
        )

    update_account_status(
        account_id,
        "STOPPED"
    )

    logger.info(
        "[%s] Account worker stopped.",
        account_id
    )

def verify_connected_account(page, expected_phone):
    """Navigates to WhatsApp Web settings to check the connected phone number."""
    # Open Profile/Settings tab
    page.click("div[title='Profile'], [data-testid='default-user']")
    
    # Extract the phone number element
    phone_element = page.wait_for_selector("div._1qB3z, [data-testid='profile-phone-number']")
    connected_phone = phone_element.inner_text()
    
    # Normalize and verify
    if expected_phone not in connected_phone.replace(" ", ""):
        raise ValueError(f"Account Mismatch! Expected {expected_phone}, but connected to {connected_phone}")


# ============================================================
# WHATSAPP HEALTH
# ============================================================

def is_whatsapp_logged_in(
    page
):

    try:

        if page.is_closed():
            return False

        url = page.url.lower()

        if "web.whatsapp.com" not in url:
            return False

        qr_selectors = [
            '[data-testid="qrcode"]',
            'canvas[aria-label*="QR"]',
        ]

        for selector in qr_selectors:

            try:

                if page.locator(
                    selector
                ).count() > 0:

                    return False

            except Exception:
                pass

        try:

            if page.locator(
                'div[role="grid"]'
            ).count() > 0:

                return True

        except Exception:
            pass

        try:

            if page.locator(
                '[data-testid="conversation-header"]'
            ).count() > 0:

                return True

        except Exception:
            pass

        return False

    except Exception:

        return False


def extract_chat_name_from_header(header_text):
    """
    Parses WhatsApp conversation header text and returns the actual chat/contact name,
    skipping avatar initials (e.g., 'PK') and UI status strings.
    """
    if not header_text:
        return ""

    lines = [line.strip() for line in header_text.split("\n") if line.strip()]
    
    ignored_keywords = {
        "online", 
        "typing...", 
        "click here for contact info", 
        "add to list",
        "business account"
    }

    for line in lines:
        if line.lower() in ignored_keywords:
            continue
        
        if len(line) <= 2 and line.isupper():
            continue

        return line

    return lines[0] if lines else ""


import re
import logging

logger = logging.getLogger(__name__)

KNOWN_GROUP_TITLES = {"drop-in interpreters", "interpreters", "announcements"}

def is_group_chat_header(chat_name: str, header_lines: list[str]) -> bool:
    if chat_name.strip().lower() in KNOWN_GROUP_TITLES:
        return True

    group_indicators = [
        ",", "you", "click here for group", 
        "group info", "tap here for group", "admin", "participants"
    ]

    for line in header_lines[1:]:
        line_lower = line.lower()
        if any(indicator in line_lower for indicator in group_indicators):
            return True

    return False



def _normalized_name_tokens(name):
    """Return normalized name text and a SET of normalized tokens.

    The lookup code relies on set operations such as intersection/difference.
    Returning a tuple here caused the runtime error:
    ``'tuple' object has no attribute 'intersection'``.
    """
    try:
        normalized = normalize_person_name(name) or ""
    except Exception:
        normalized = ""
    return normalized, set(normalized.split())


def lookup_client_by_verified_name(fullname):
    """Resolve an existing Trello/DB client by a conservatively verified name.

    Used only when WhatsApp does not expose a phone/JID for the open chat.
    Matching is deliberately strict enough to avoid guessing, but tolerant of
    harmless formatting differences such as reordered names, omitted middle
    names, punctuation, or a single token difference. Any ambiguous result is
    rejected.
    """
    if not fullname:
        return None

    normalized_input, input_tokens = _normalized_name_tokens(fullname)
    if not normalized_input or not input_tokens:
        return None

    with DATABASE_LOCK:
        conn = get_db_connection()
        try:
            rows = conn.execute(
                """
                SELECT
                    phone,
                    whatsapp_phone,
                    airtable_id,
                    trello_card_id,
                    fullname,
                    contact_type
                FROM contacts
                WHERE LOWER(TRIM(contact_type)) = 'client'
                """
            ).fetchall()
        finally:
            conn.close()

    def row_payload(row):
        return {
            "phone": row[0],
            "whatsapp_phone": row[1],
            "airtable_id": row[2],
            "trello_card_id": row[3],
            "fullname": row[4],
            "contact_type": row[5],
        }

    # 1. Exact normalized name.
    exact = []
    for row in rows:
        normalized_db, db_tokens = _normalized_name_tokens(row[4])
        if normalized_db and normalized_db == normalized_input:
            exact.append(row)

    if len(exact) == 1:
        return row_payload(exact[0])
    if len(exact) > 1:
        logger.error(
            "AMBIGUOUS exact WhatsApp chat name %r: %d existing client records match; refusing to guess.",
            fullname, len(exact),
        )
        return None

    # 2. Exact token-set match. This handles harmless ordering differences.
    token_matches = []
    for row in rows:
        normalized_db, db_tokens = _normalized_name_tokens(row[4])
        if normalized_db and db_tokens == input_tokens:
            token_matches.append(row)

    if len(token_matches) == 1:
        return row_payload(token_matches[0])
    if len(token_matches) > 1:
        logger.error(
            "AMBIGUOUS token-set WhatsApp chat name %r: %d existing client records match; refusing to guess.",
            fullname, len(token_matches),
        )
        return None

    # 3. Conservative token-overlap match: all WhatsApp tokens must be found
    # in the database name, or vice versa, with at most one omitted token.
    overlap_matches = []
    for row in rows:
        normalized_db, db_tokens = _normalized_name_tokens(row[4])
        if not normalized_db or not db_tokens:
            continue
        common = input_tokens.intersection(db_tokens)
        missing_from_db = input_tokens - db_tokens
        missing_from_input = db_tokens - input_tokens

        if common and len(missing_from_db) <= 1 and len(missing_from_input) <= 1:
            overlap_matches.append(row)

    # De-duplicate by stable client identity.
    unique = {}
    for row in overlap_matches:
        key = (row[0], row[3], row[4])
        unique[key] = row
    overlap_matches = list(unique.values())

    if len(overlap_matches) == 1:
        return row_payload(overlap_matches[0])
    if len(overlap_matches) > 1:
        logger.error(
            "AMBIGUOUS overlap WhatsApp chat name %r: %d existing client records match; refusing to guess. Matches=%r",
            fullname,
            len(overlap_matches),
            [
                {
                    "fullname": row[4],
                    "phone": row[0],
                    "whatsapp_phone": row[1],
                    "trello_card_id": row[3],
                }
                for row in overlap_matches
            ],
        )
        return None

    # 4. High-confidence fuzzy match only when there is a clear winner and
    # enough name material. This catches spacing/diacritic/punctuation quirks
    # without ever choosing between near-equal clients.
    scored = []
    for row in rows:
        normalized_db, db_tokens = _normalized_name_tokens(row[4])
        if not normalized_db:
            continue
        common = input_tokens.intersection(db_tokens)
        token_floor = min(len(input_tokens), len(db_tokens))
        if len(input_tokens) < 2 or len(common) < max(1, token_floor - 1):
            continue
        score = difflib.SequenceMatcher(None, normalized_input, normalized_db).ratio()
        scored.append((score, row))

    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and scored[0][0] >= 0.90:
        best_score, best_row = scored[0]
        if len(scored) == 1 or (best_score - scored[1][0]) >= 0.04:
            logger.info(
                "Verified-name fuzzy match for %r -> %r score=%.3f",
                fullname, best_row[4], best_score,
            )
            return row_payload(best_row)

    return None


def check_open_chat(whatsapp_account, page):
    """Inspect the currently open WhatsApp chat and resolve a known client.

    Resolution order:
      1. Reject group chats immediately.
      2. Use an authoritative WhatsApp phone from the header when available.
      3. If WhatsApp exposes no phone, use the selected chat's verified JID/
         drawer resolution already performed by ``get_open_chat_header``.
      4. As a controlled fallback, match the open chat name against an
         existing client record using exact normalized-name/token matching.

    Crucially, this fallback never consults the old chat-name -> phone cache,
    never invents a phone number, and never uses fuzzy/substring matching.
    """
    header_data = get_open_chat_header(page, whatsapp_account)

    if not header_data:
        logger.debug(
            "[%s] OPEN CHAT DEBUG: No reliably open chat detected; leaving sidebar unchanged.",
            whatsapp_account,
        )
        return None

    candidate_name = (header_data.get("chat_name") or "").strip()
    is_group = bool(header_data.get("is_group"))

    # Group chats are outside the listener's scope. Do not resolve their
    # members, create/modify Trello cards, or process their messages.
    if is_group:
        logger.info(
            "[%s] OPEN CHAT DEBUG: Group chat detected (%r); ignoring chat completely.",
            whatsapp_account,
            candidate_name,
        )
        return None

    if not candidate_name and not header_data.get("phone"):
        logger.debug(
            "[%s] OPEN CHAT DEBUG: Header contained neither a chat name nor a phone.",
            whatsapp_account,
        )
        return None

    phone = header_data.get("phone")
    if phone:
        try:
            phone = normalize_phone(phone)
        except Exception as e:
            logger.warning(
                "[%s] OPEN CHAT DEBUG: Could not normalize authoritative WhatsApp phone %r: %s",
                whatsapp_account,
                phone,
                e,
            )
            phone = None

    # Primary resolution: authoritative WhatsApp phone -> existing client.
    if phone:
        logger.info(
            "[%s] OPEN CHAT DEBUG: Resolving client by authoritative WhatsApp phone=%s",
            whatsapp_account,
            phone,
        )
        client = lookup_client_by_phone_or_whatsapp(phone)
        if client:
            contact_type = (client.get("contact_type") or "").strip().lower()
            if contact_type == "client":
                client["whatsapp_phone"] = phone
                logger.info(
                    "[%s] Identified open WhatsApp client: name=%r phone=%r via WhatsApp phone",
                    whatsapp_account,
                    client.get("fullname") or candidate_name,
                    client.get("phone") or phone,
                )
                return {
                    "chat_name": candidate_name or client.get("fullname"),
                    "phone": phone,
                    "whatsapp_phone": phone,
                    "is_group": False,
                    "fullname": client.get("fullname"),
                    "trello_card_id": client.get("trello_card_id"),
                    "contact_type": "client",
                    "airtable_id": client.get("airtable_id"),
                }

        logger.info(
            "[%s] OPEN CHAT DEBUG: WhatsApp phone %s is not a known Trello client.",
            whatsapp_account,
            phone,
        )

    # Controlled fallback: verified existing client by name.  This is allowed
    # ONLY when WhatsApp did not expose any phone number.  If WhatsApp did
    # expose a phone and that phone is not a known client, do not substitute a
    # same-name Trello record because that could map the wrong person.
    if not phone and candidate_name:
        logger.info(
            "[%s] OPEN CHAT DEBUG: WhatsApp phone unavailable/unmatched; "
            "checking existing Trello client by verified name=%r",
            whatsapp_account,
            candidate_name,
        )
        client = lookup_client_by_verified_name(candidate_name)
        if client:
            client_phone = client.get("phone") or client.get("whatsapp_phone")
            if client_phone:
                try:
                    client_phone = normalize_phone(client_phone)
                except Exception:
                    client_phone = None

            if client_phone:
                logger.info(
                    "[%s] OPEN CHAT DEBUG: Matched existing Trello client by verified name: "
                    "name=%r phone=%r card=%r",
                    whatsapp_account,
                    client.get("fullname") or candidate_name,
                    client_phone,
                    client.get("trello_card_id"),
                )
                return {
                    "chat_name": candidate_name,
                    "phone": client_phone,
                    "whatsapp_phone": client.get("whatsapp_phone") or None,
                    "is_group": False,
                    "fullname": client.get("fullname"),
                    "trello_card_id": client.get("trello_card_id"),
                    "contact_type": client.get("contact_type"),
                    "airtable_id": client.get("airtable_id"),
                    "resolved_by": "verified_name",
                }

        logger.info(
            "[%s] OPEN CHAT DEBUG: No unique existing Trello client matched verified name=%r",
            whatsapp_account,
            candidate_name,
        )

    # Never fall back to a stale chat-name -> phone cache or a guessed phone.
    logger.warning(
        "[%s] OPEN CHAT DEBUG: Chat=%r could not be safely resolved to a client; skipping.",
        whatsapp_account,
        candidate_name,
    )
    return None


def is_valid_chat_title(title: str) -> bool:
    """Filters out auto-responses or system notices parsed as chat names."""
    if not title or len(title) > 60:
        return False

    system_prompts = [
        "thank you for contacting",
        "if you are homeless",
        "call council",
        "automotive reply"
    ]
    
    title_lower = title.lower()
    return not any(prompt in title_lower for prompt in system_prompts)


def monitor_whatsapp_account(whatsapp_account, page):
    """
    Observe synchronized outgoing messages in two ways:

      * passively from chat-list previews, so phone-sent messages can be seen
        even while their conversation is closed;
      * from the full message DOM when an operator has a conversation open.

    No chat is opened or switched by this helper.
    """
    try:
        monitor_sidebar_for_synced_outgoing(
            whatsapp_account=whatsapp_account,
            page=page,
        )
    except Exception as e:
        logger.exception(
            "[%s] Passive phone-sync monitor failed: %s",
            whatsapp_account,
            e,
        )

    client = check_open_chat(whatsapp_account, page)
    if not client:
        return

    phone = client.get("phone")
    if not phone:
        return

    try:
        new_messages = get_new_messages(
            whatsapp_account=whatsapp_account,
            phone=phone,
            page=page,
        )
    except Exception as e:
        logger.exception(
            "[%s] Open-chat message scan failed for %s: %s",
            whatsapp_account,
            phone,
            e,
        )
        return

    for message_info in new_messages:
        try:
            process_message(
                whatsapp_account=whatsapp_account,
                page=page,
                phone=phone,
                message_info=message_info,
                contact=client,
            )
        except Exception as e:
            logger.exception(
                "[%s] Open-chat message processing failed for %s: %s",
                whatsapp_account,
                phone,
                e,
            )


def wait_for_whatsapp_ready(
    page,
    account_id
):

    deadline = (
        time.monotonic()
        +
        WHATSAPP_READY_TIMEOUT_MS / 1000
    )

    while (
        time.monotonic() < deadline
        and not SHUTDOWN_EVENT.is_set()
    ):

        try:

            if is_whatsapp_logged_in(
                page
            ):

                logger.info(
                    "[%s] WhatsApp is ready.",
                    account_id
                )

                update_account_status(
                    account_id,
                    "ONLINE"
                )

                return True

            if page.locator(
                '[data-testid="qrcode"]'
            ).count() > 0:

                update_account_status(
                    account_id,
                    "QR_REQUIRED"
                )

                logger.warning(
                    "[%s] WhatsApp requires QR/login.",
                    account_id
                )

                return False

        except Exception as e:

            logger.warning(
                "[%s] Health check error: %s",
                account_id,
                e
            )

        time.sleep(2)

    update_account_status(
        account_id,
        "DISCONNECTED"
    )

    return False


def attach_playwright_monitoring(
    account_id,
    context
):

    def on_context_close(_context):

        logger.warning(
            "[%s] Playwright context closed.",
            account_id
        )

        update_account_status(
            account_id,
            "DISCONNECTED"
        )

    def on_page_error(error):

        logger.error(
            "[%s] WhatsApp page error: %s",
            account_id,
            error
        )

    context.on(
        "close",
        on_context_close
    )

    for page in context.pages:

        page.on(
            "pageerror",
            on_page_error
        )


def is_playwright_closed_error(error):

    text = str(error).lower()

    return (
        "target page, context or browser has been closed" in text
        or "browser has been closed" in text
        or "context has been closed" in text
        or "page has been closed" in text
    )


# ============================================================
# HEALTH SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def log_message(
        self,
        format,
        *args
    ):

        logger.info(
            "Health HTTP: " + format,
            *args
        )

    def do_GET(self):

        if self.path not in (
            "/",
            "/health"
        ):

            self.send_response(
                404
            )

            self.end_headers()

            return

        with ACCOUNT_STATUS_LOCK:

            accounts = {
                account_id: dict(status)
                for account_id, status
                in ACCOUNT_STATUS.items()
            }

        healthy = (
            len(accounts) == len(WHATSAPP_ACCOUNTS)
            and all(
                status.get("status") == "ONLINE"
                for status in accounts.values()
            )
            and not SHUTDOWN_EVENT.is_set()
        )

        uptime = (
            datetime.now()
            -
            SERVICE_STARTED_AT
        ).total_seconds()

        payload = {
            "service": "whatsapp-trello",
            "healthy": healthy,
            "shutdown_requested":
                SHUTDOWN_EVENT.is_set(),
            "uptime_seconds": uptime,
            "accounts": accounts
        }

        body = json.dumps(
            payload,
            indent=2
        ).encode(
            "utf-8"
        )

        self.send_response(
            200 if healthy else 503
        )

        self.send_header(
            "Content-Type",
            "application/json"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(
            body
        )


def start_health_server():

    server = ThreadingHTTPServer(
        (
            HEALTH_HOST,
            HEALTH_PORT
        ),
        HealthHandler
    )

    thread = threading.Thread(
        target=server.serve_forever,
        name="HealthServer",
        daemon=True
    )

    thread.start()

    logger.info(
        "Health server listening on %s:%s",
        HEALTH_HOST,
        HEALTH_PORT
    )

    return server

# Optional: Allow partial startup state to return 200 OK
@app.route('/health', methods=['GET'])
def health_check():
    """
    Returns 200 during startup if threads are actively initializing,
    preventing external health probes from timing out or failing prematurely.
    """
    total_accounts = len(ACCOUNTS)
    ready_accounts = sum(1 for worker in WORKERS.values() if getattr(worker, 'is_ready', False))
    starting_accounts = sum(1 for worker in WORKERS.values() if getattr(worker, 'is_starting', True))

    # Allow startup grace period
    if ready_accounts + starting_accounts == total_accounts:
        return {
            "status": "healthy" if ready_accounts == total_accounts else "initializing",
            "ready": ready_accounts,
            "total": total_accounts
        }, 200

    return {
        "status": "unhealthy",
        "ready": ready_accounts,
        "total": total_accounts
    }, 503


# ============================================================
# DATABASE HEALTH
# ============================================================

def database_health_check():

    try:

        with DATABASE_LOCK:

            conn = get_db_connection()

            conn.execute(
                "SELECT 1"
            ).fetchone()

            integrity = conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()

            conn.close()

        if not integrity or integrity[0] != "ok":

            raise RuntimeError(
                f"SQLite integrity check failed: {integrity}"
            )

        logger.info(
            "SQLite database health check passed."
        )

        return True

    except Exception as e:

        logger.exception(
            "SQLite database health check failed: %s",
            e
        )

        return False


# ============================================================
# TRELLO SYNC THREAD
# ============================================================

def trello_sync_loop():

    logger.info(
        "[TRELLO] Starting periodic "
        "Trello synchronisation thread."
    )

    while not SHUTDOWN_EVENT.is_set():

        try:

            if SHUTDOWN_EVENT.wait(
                TRELLO_SYNC_INTERVAL_SECONDS
            ):
                break

            logger.info(
                "[TRELLO] Periodic sync..."
            )

            sync_trello_clients()

            logger.info(
                "[TRELLO] Periodic client sync completed."
            )

        except Exception as e:

            logger.exception(
                "[TRELLO] Periodic sync error: %s",
                e
            )

            if SHUTDOWN_EVENT.wait(30):
                break


def trello_health_check():

    try:

        result = trello_request(
            "GET",
            "/members/me",
            {
                "fields": "id,username,fullName"
            }
        )

        logger.info(
            "[TRELLO] Authenticated as %s",
            result.get("fullName")
        )

        return True

    except Exception as e:

        logger.exception(
            "[TRELLO] Health check failed: %s",
            e
        )

        return False


# ============================================================
DIRECTION_FIX_VERSION = "V9_DIRECTION_EVIDENCE_AND_BUBBLE_GEOMETRY"

# MESSAGE EXTRACTION AND DIRECTION DETECTOR
# ============================================================

def get_message_direction(page, message_element, return_source=False):
    """Resolve WhatsApp message direction without guessing from attachments.

    V9 is deliberately evidence-first:
      1) WhatsApp's own message id / outgoing metadata / classes / pre-plain-text;
      2) bubble/layout metadata and horizontal geometry;
      3) otherwise return None and let the scanner skip the message.

    The diagnostic payload is intentionally compact so unresolved DOM changes can
    be identified from logs without dumping an entire message's HTML.
    """
    def _finish(direction, source=None):
        if return_source:
            return (direction, source)
        return direction

    if not page or not message_element:
        return _finish(None, "no-page-or-element")

    # Collect direction evidence and likely bubble rectangles in the browser.
    # Returning the evidence also makes future WhatsApp DOM changes much easier
    # to diagnose from one log line.
    try:
        evidence = message_element.evaluate(r"""el => {
            if (!el) return null;
            const clean = v => (v == null ? '' : String(v)).slice(0, 500);
            const nodeInfo = (n, depth) => {
                if (!n || n.nodeType !== 1) return null;
                const cs = getComputedStyle(n);
                const r = n.getBoundingClientRect();
                return {
                    depth,
                    tag: n.tagName,
                    cls: clean(n.className),
                    testid: clean(n.getAttribute('data-testid')),
                    id: clean(n.getAttribute('data-id')),
                    outgoing: clean(n.getAttribute('data-is-outgoing')),
                    pre: clean(n.getAttribute('data-pre-plain-text')),
                    aria: clean(n.getAttribute('aria-label')),
                    title: clean(n.getAttribute('title')),
                    role: clean(n.getAttribute('role')),
                    text: clean(n.innerText || n.textContent).replace(/\\s+/g, ' ').slice(0, 120),
                    x: Math.round(r.x), y: Math.round(r.y),
                    w: Math.round(r.width), h: Math.round(r.height),
                    display: cs.display,
                    position: cs.position,
                    alignSelf: cs.alignSelf,
                    justifySelf: cs.justifySelf,
                    marginLeft: cs.marginLeft,
                    marginRight: cs.marginRight,
                    left: cs.left,
                    right: cs.right,
                    transform: cs.transform,
                    direction: cs.direction,
                    float: cs.float
                };
            };

            const ancestors = [];
            let n = el;
            for (let depth = 0; n && depth < 55; depth++, n = n.parentElement) {
                ancestors.push(nodeInfo(n, depth));
            }

            const selector = [
                '[data-id]', '[data-is-outgoing]', '[data-pre-plain-text]',
                '[data-testid]', '.message-in', '.message-out',
                '[class*="message-"]', '[class*="copyable-text"]',
                '[aria-label]', '[title]'
            ].join(',');
            const descendants = [];
            for (const child of el.querySelectorAll(selector)) {
                const info = nodeInfo(child, -1);
                if (info) descendants.push(info);
                if (descendants.length >= 180) break;
            }

            // Candidate visual boxes: prefer elements that contain actual text or
            // media, but keep enough structural nodes to catch modern WhatsApp
            // layouts where the bubble itself has no stable class.
            const boxes = [];
            const boxSelector = [
                '[data-testid]', '[data-id]', '[data-pre-plain-text]',
                '.copyable-text', '[role="button"]', 'span', 'div'
            ].join(',');
            for (const child of el.querySelectorAll(boxSelector)) {
                const r = child.getBoundingClientRect();
                const w = r.width, h = r.height;
                if (w < 25 || h < 12) continue;
                const txt = (child.innerText || child.textContent || '').trim();
                const hasMedia = !!child.querySelector('img, video, canvas, audio, [data-testid*="media"], [data-testid*="document"]');
                if (!txt && !hasMedia) continue;
                boxes.push({
                    tag: child.tagName,
                    cls: clean(child.className),
                    testid: clean(child.getAttribute('data-testid')),
                    id: clean(child.getAttribute('data-id')),
                    textLen: Math.min(txt.length, 500),
                    media: hasMedia,
                    x: Math.round(r.x), y: Math.round(r.y),
                    w: Math.round(w), h: Math.round(h),
                    display: getComputedStyle(child).display,
                    alignSelf: getComputedStyle(child).alignSelf,
                    marginLeft: getComputedStyle(child).marginLeft,
                    marginRight: getComputedStyle(child).marginRight
                });
                if (boxes.length >= 120) break;
            }
            return {self: nodeInfo(el, 0), ancestors, descendants, boxes};
        }""")
    except Exception as e:
        logger.debug('[DIRECTION] DOM evidence collection failed: %s', e)
        evidence = None

    def _direction_from_node(n):
        if not isinstance(n, dict):
            return None
        node_id = str(n.get('id') or '')
        out = str(n.get('outgoing') or '').lower()
        cls = str(n.get('cls') or '')
        pre = str(n.get('pre') or '')
        aria = str(n.get('aria') or '')
        title = str(n.get('title') or '')
        testid = str(n.get('testid') or '')

        if node_id.startswith('true_'):
            return 'outgoing', 'data-id=true_'
        if node_id.startswith('false_'):
            return 'incoming', 'data-id=false_'
        if out == 'true':
            return 'outgoing', 'data-is-outgoing=true'
        if out == 'false':
            return 'incoming', 'data-is-outgoing=false'
        if re.search(r'(^|\\s)message-out(?:\\s|$)', cls, re.I):
            return 'outgoing', 'message-out-class'
        if re.search(r'(^|\\s)message-in(?:\\s|$)', cls, re.I):
            return 'incoming', 'message-in-class'
        # Some WhatsApp builds put the direction in a test id rather than a
        # class. Only accept explicit direction words, never generic "msg".
        if re.search(r'(outgoing|sent|sender)', testid, re.I):
            return 'outgoing', 'testid-outgoing'
        if re.search(r'(incoming|received|receiver)', testid, re.I):
            return 'incoming', 'testid-incoming'
        if re.search(r'\\b(you|sent by you|from you)\\b', aria + ' ' + title, re.I):
            return 'outgoing', 'aria/title-you'
        if re.search(r'\\]\s*You\\s*:', pre, re.I):
            return 'outgoing', 'pre-plain-text-you'
        return None

    # 1) Explicit metadata. Search ancestors first because msg-container is
    # commonly a wrapper around the actual WhatsApp message node.
    if evidence:
        ordered = []
        ordered.extend(evidence.get('ancestors') or [])
        ordered.extend(evidence.get('descendants') or [])
        explicit = []
        for node in ordered:
            hit = _direction_from_node(node)
            if hit:
                explicit.append(hit)
        if explicit:
            # If multiple explicit signals disagree, do not guess.
            directions = {d for d, _ in explicit}
            if len(directions) == 1:
                return _finish(next(iter(directions)), explicit[0][1] if explicit else None)
            logger.warning('[DIRECTION] conflicting explicit metadata: %r', explicit[:8])

        # 2) Check CSS/layout evidence on ancestors and descendants. Explicit
        # align-self/justify-self values are useful on flex-based WhatsApp rows.
        layout_votes = []
        for node in (evidence.get('ancestors') or []) + (evidence.get('descendants') or []):
            cls = str(node.get('cls') or '')
            tid = str(node.get('testid') or '')
            align = str(node.get('alignSelf') or '').lower()
            justify = str(node.get('justifySelf') or '').lower()
            ml = str(node.get('marginLeft') or '')
            mr = str(node.get('marginRight') or '')
            if re.search(r'(message|bubble|msg)', cls + ' ' + tid, re.I):
                if align in ('flex-end', 'end') or justify in ('flex-end', 'end'):
                    layout_votes.append('outgoing')
                if align in ('flex-start', 'start') or justify in ('flex-start', 'start'):
                    layout_votes.append('incoming')
            # Auto margin is a strong flex-row alignment signal.
            if ml == 'auto' and mr != 'auto':
                layout_votes.append('outgoing')
            elif mr == 'auto' and ml != 'auto':
                layout_votes.append('incoming')
        if layout_votes and len(set(layout_votes)) == 1:
            return _finish(layout_votes[0], 'layout-css')

    # 3) Geometry. Find the conversation pane, then score the most plausible
    # message bubble rather than blindly choosing the narrowest descendant.
    try:
        pane = None
        for sel in (
            '[data-testid*="conversation-panel-messages"]',
            '[data-testid*="conversation-panel-body"]',
            '[data-testid*="conversation-panel"]',
        ):
            try:
                loc = page.locator(sel).last
                if loc.count() and loc.is_visible():
                    pb = loc.bounding_box()
                    if pb and pb.get('width', 0) > 200:
                        pane = pb
                        break
            except Exception:
                pass
        wrapper_box = message_element.bounding_box()
        if pane and wrapper_box:
            px, py = float(pane['x']), float(pane['y'])
            pw, ph = float(pane['width']), float(pane['height'])
            candidates = []
            raw_boxes = (evidence or {}).get('boxes') or []
            for b in raw_boxes:
                x, y, w, h = map(float, (b.get('x', 0), b.get('y', 0), b.get('w', 0), b.get('h', 0)))
                if w < 25 or h < 12 or w > pw * 0.88 or h > ph * 0.70:
                    continue
                if x < px - 8 or x + w > px + pw + 8:
                    continue
                # Must overlap the wrapper vertically; this filters unrelated
                # controls nested elsewhere in a large msg-container.
                wy, wh = float(wrapper_box['y']), float(wrapper_box['height'])
                overlap = max(0.0, min(y + h, wy + wh) - max(y, wy))
                if overlap < min(h, wh) * 0.35:
                    continue
                left_gap = max(0.0, (x - px) / pw)
                right_gap = max(0.0, ((px + pw) - (x + w)) / pw)
                edge_gap = min(left_gap, right_gap)
                text_bonus = min(0.25, float(b.get('textLen', 0)) / 400.0)
                media_bonus = 0.12 if b.get('media') else 0.0
                # Penalise tiny controls and extremely wide layout nodes.
                size_bonus = min(0.20, (w / pw) * 0.20)
                score = (1.0 - min(edge_gap, 1.0)) + text_bonus + media_bonus + size_bonus
                candidates.append((score, left_gap, right_gap, b))

            # Include the wrapper itself as a fallback geometry candidate.
            x, y, w, h = map(float, (wrapper_box['x'], wrapper_box['y'], wrapper_box['width'], wrapper_box['height']))
            if w <= pw * 0.88:
                candidates.append((0.25 + (1.0 - min(min((x-px)/pw, ((px+pw)-(x+w))/pw), 1.0)),
                                   max(0.0, (x-px)/pw), max(0.0, ((px+pw)-(x+w))/pw),
                                   {'tag':'wrapper','w':w,'h':h}))

            if candidates:
                candidates.sort(key=lambda item: item[0], reverse=True)
                top = candidates[:8]
                # Vote among the best visual candidates. Require a clear edge
                # relationship; centered bubbles/system rows are not directional.
                votes = []
                for _, left_gap, right_gap, _ in top:
                    if left_gap <= 0.20 and right_gap >= 0.12:
                        votes.append('incoming')
                    elif right_gap <= 0.20 and left_gap >= 0.12:
                        votes.append('outgoing')
                if votes and len(set(votes)) == 1:
                    return _finish(votes[0], 'geometry-edge-gap-majority')
                if votes:
                    # Only accept a side if the strongest candidate agrees with
                    # the majority and has a materially better edge score.
                    incoming = sum(v == 'incoming' for v in votes)
                    outgoing = sum(v == 'outgoing' for v in votes)
                    if incoming >= 2 and incoming > outgoing:
                        return _finish('incoming', 'geometry-edge-gap')
                    if outgoing >= 2 and outgoing > incoming:
                        return _finish('outgoing', 'geometry-edge-gap')
    except Exception as e:
        logger.debug('[DIRECTION] geometry detection failed: %s', e)

    # 4) Diagnostics only after all safe evidence failed.
    try:
        if evidence:
            logger.warning(
                '[DIRECTION] unresolved DOM evidence: self=%r ancestors=%r descendants=%r boxes=%r',
                evidence.get('self'),
                (evidence.get('ancestors') or [])[:10],
                (evidence.get('descendants') or [])[:25],
                (evidence.get('boxes') or [])[:20],
            )
    except Exception:
        pass
    return _finish(None, "unresolved")


def extract_message_text(message_element):
    try:
        selectable = message_element.locator(
            '[data-testid="selectable-text"], span.selectable-text, span._ao3e, span[dir="ltr"]'
        )

        parts = []
        if selectable.count() > 0:
            for i in range(selectable.count()):
                try:
                    text = selectable.nth(i).inner_text(
                        timeout=1000
                    ).strip()
                    if text and text not in parts:
                        parts.append(text)
                except Exception:
                    pass

            if parts:
                return "\n".join(parts).strip()

        raw_text = message_element.inner_text(
            timeout=1000
        )
        return raw_text.strip() if raw_text else None

    except Exception as e:
        logger.debug(
            "Could not extract message text: %s",
            e
        )
        return None

def extract_message_direction(page, element_locator) -> str | None:
    """
    Determines message direction ('in' or 'out') by examining the locator's
    CSS classes, data-id attribute, or DOM context (both parent and child nodes).
    """
    try:
        if not element_locator:
            return None

        return page.evaluate("""(el) => {
            if (!el) return null;

            // Helper function to safely parse direction from a node
            const parseNode = (node) => {
                if (!node) return null;
                
                // Safely evaluate class names (handles SVGAnimatedString and plain strings)
                const cls = typeof node.className === 'string' ? node.className : (node.getAttribute('class') || '');
                if (cls.includes('message-in')) return 'in';
                if (cls.includes('message-out')) return 'out';

                // Evaluate data-id attribute ('false_' = incoming, 'true_' = outgoing)
                const dataId = node.getAttribute('data-id') || '';
                if (dataId.startsWith('false_')) return 'in';
                if (dataId.startsWith('true_')) return 'out';

                return null;
            };

            // 1. Check self or closest ancestor container
            const ancestor = el.closest('div.message-in, div.message-out, div[data-id]');
            let dir = parseNode(ancestor);
            if (dir) return dir;

            // 2. Check descendants if el is a wrapper/row element
            const descendant = el.querySelector('div.message-in, div.message-out, div[data-id]');
            dir = parseNode(descendant);
            if (dir) return dir;

            // 3. Last fallback: Check row container or parent wrapper
            const parentRow = el.closest('div[role="row"], div._ak8l') || el;
            const innerMsg = parentRow.querySelector('.message-in, .message-out');
            return parseNode(innerMsg);
        }""", element_locator.element_handle())

    except Exception as e:
        logger.warning("Error resolving message direction: %s", e)
        return None


# Cache failed phone resolutions with a timestamp: { "chat_name": timestamp }
FAILED_PHONE_RESOLUTIONS = {}
CACHE_TTL_SECONDS = 300  # Retry failed lookups every 5 minutes

def close_whatsapp_contact_drawer(page, reason="") -> bool:
    """Close the contact/profile drawer and wait until it is no longer visible."""
    try:
        drawer = page.locator('[data-testid="drawer-right"]').first
        visible = False
        try:
            visible = bool(drawer.count() and drawer.is_visible())
        except Exception:
            visible = False

        if not visible:
            return True

        logger.info(
            "[DRAWER EXTRACTION] Closing drawer-right%s",
            f" ({reason})" if reason else "",
        )

        # Escape is the least invasive close operation and does not change chats.
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

        deadline = time.time() + 2.5
        while time.time() < deadline:
            try:
                if not drawer.is_visible():
                    return True
            except Exception:
                return True
            try:
                page.wait_for_timeout(100)
            except Exception:
                time.sleep(0.1)

        # Last-resort DOM click on a visible close control INSIDE the drawer.
        try:
            close_btn = drawer.locator(
                '[aria-label="Close"], [title="Close"], [data-testid*="close"]'
            ).first
            if close_btn.count() and close_btn.is_visible():
                close_btn.click(timeout=1000, force=True)
                page.wait_for_timeout(150)
        except Exception:
            pass

        try:
            return not drawer.is_visible()
        except Exception:
            return True
    except Exception as exc:
        logger.debug(
            "[DRAWER EXTRACTION] Could not close drawer%s: %s",
            f" ({reason})" if reason else "",
            exc,
        )
        return False


def extract_phone_from_drawer(page) -> str | None:
    """Open (only when necessary) and scan WhatsApp's contact/profile drawer.

    This routine must never spend a long time clicking a header control that is
    already covered by ``drawer-right``.  Prefer direct DOM extraction from an
    existing drawer, then use the explicit Profile details control only when no
    drawer is visible.
    """
    def _scan_visible_drawer() -> str | None:
        try:
            raw_text_data = page.evaluate("""() => {
                const selectors = [
                    '[data-testid="contact-info-drawer"]',
                    '[data-testid="drawer-right"]',
                    'div[tabindex="-1"]._ak9y',
                    'section._aawg'
                ];
                const visible = el => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 120 && r.height > 120 &&
                           s.display !== 'none' && s.visibility !== 'hidden' &&
                           s.opacity !== '0';
                };
                for (const sel of selectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (!visible(el)) continue;
                        const ltr = Array.from(el.querySelectorAll('span[dir="ltr"], span[title], div._ak8q'))
                            .map(x => x.getAttribute('title') || (x.textContent || '').trim())
                            .filter(Boolean);
                        return (el.innerText || '') + ' ' + ltr.join(' ');
                    }
                }
                return '';
            }""")
            if not raw_text_data:
                return None

            matches = re.findall(r'\+?\d[\d\s\-\(\)]{8,18}\d', raw_text_data)
            for raw_match in matches:
                clean_digits = re.sub(r'\D', '', raw_match)
                if 10 <= len(clean_digits) <= 15:
                    return clean_digits
            return extract_phone_from_text(raw_text_data)
        except Exception as e:
            logger.debug("[DRAWER EXTRACTION] Direct drawer scan failed: %s", e)
            return None

    try:
        # 1. If WhatsApp already has a right-side drawer, NEVER click through it.
        try:
            existing_drawer = page.locator('[data-testid="drawer-right"]').first
            if existing_drawer.count() and existing_drawer.is_visible():
                logger.info("[DRAWER EXTRACTION] Existing drawer-right is visible; scanning directly without clicking header")
                phone = _scan_visible_drawer()
                if phone:
                    logger.info("[DRAWER EXTRACTION] phone resolved from existing drawer-right=%s", phone)
                close_whatsapp_contact_drawer(page, reason="after direct phone extraction")
                return phone
        except Exception as e:
            logger.debug("[DRAWER EXTRACTION] existing drawer-right inspection failed: %s", e)

        # 2. Check the dedicated contact-info drawer first.
        try:
            drawer_locator = page.locator('[data-testid="contact-info-drawer"], div[tabindex="-1"]._ak9y').first
            if drawer_locator.count() and drawer_locator.is_visible():
                phone = _scan_visible_drawer()
                if phone:
                    logger.info("[DRAWER EXTRACTION] phone resolved from visible contact-info drawer=%s", phone)
                close_whatsapp_contact_drawer(page, reason="after visible contact-info scan")
                return phone
        except Exception as e:
            logger.debug("[DRAWER EXTRACTION] contact-info drawer inspection failed: %s", e)

        # 3. No drawer: click only an explicit profile-details control.
        profile_btn = page.locator(
            '[data-testid*="conversation-header"] [title="Profile details"], '
            '[data-testid*="conversation-header"] [aria-label="Profile details"], '
            '[title="Profile details"][role="button"], '
            '[aria-label="Profile details"][role="button"]'
        ).first
        if profile_btn.count() and profile_btn.is_visible():
            logger.info("[DRAWER EXTRACTION] Opening contact info via explicit Profile details control")
            try:
                profile_btn.click(timeout=2500)
            except Exception as click_err:
                logger.debug("[DRAWER EXTRACTION] Profile details click failed: %s", click_err)
                try:
                    profile_btn.evaluate('(el) => el.click()')
                except Exception:
                    pass
            try:
                page.wait_for_timeout(500)
            except Exception:
                pass
        else:
            # Some WhatsApp builds expose only the header name as the clickable
            # contact target.  Use it only as a secondary fallback.
            contact_btn = page.locator(
                '[data-testid="conversation-header"] span[dir="auto"]'
            ).first
            if contact_btn.count() and contact_btn.is_visible():
                logger.info("[DRAWER EXTRACTION] Opening contact info via header contact-name fallback")
                try:
                    contact_btn.click(timeout=2500)
                    page.wait_for_timeout(500)
                except Exception as click_err:
                    logger.debug("[DRAWER EXTRACTION] Header contact-name click failed: %s", click_err)

        # 4. Final direct scan.
        phone = _scan_visible_drawer()
        if phone:
            logger.info("[DRAWER EXTRACTION] phone resolved after drawer open=%s", phone)
        close_whatsapp_contact_drawer(page, reason="after drawer-open extraction")
        return phone

    except Exception as e:
        logger.warning("[DRAWER EXTRACTION] Error scanning drawer contents: %s", e)
        return None

def resolve_chat_phone_with_cache(page, chat_name: str) -> str | None:
    """
    Wraps drawer extraction with the 5-minute TTL failure cache.
    """
    now = time.time()

    # Skip lookup if failed recently within TTL window
    if chat_name in FAILED_PHONE_RESOLUTIONS:
        if now - FAILED_PHONE_RESOLUTIONS[chat_name] < CACHE_TTL_SECONDS:
            return None

    phone = extract_phone_from_drawer(page)

    if phone:
        FAILED_PHONE_RESOLUTIONS.pop(chat_name, None)
        return phone
    else:
        FAILED_PHONE_RESOLUTIONS[chat_name] = now
        logger.warning("[DRAWER EXTRACTION] Could not resolve phone for '%s'. Cached for 5m.", chat_name)
        return None


def identify_open_client(account_id, page) -> dict | None:
    """Identify ONLY the conversation currently opened by the operator."""
    try:
        logger.info("[%s] OPEN CHAT DEBUG: identify_open_client entered", account_id)

        # 1. Inspect main pane count
        try:
            main_count = page.locator('#main').count()
            role_main_count = page.locator('div[role="main"]').count()
            logger.info("[%s] OPEN CHAT DEBUG: #main count=%d role-main count=%d", account_id, main_count, role_main_count)
        except Exception as e:
            logger.info("[%s] OPEN CHAT DEBUG: main-pane inspection failed: %s", account_id, e)

        # 2. Retrieve open chat header
        header_data = get_open_chat_header(page, account_id)
        logger.info("[%s] OPEN CHAT DEBUG: get_open_chat_header returned=%r", account_id, header_data)

        non_chat_state = _non_chat_drawer_visible(page)
        if non_chat_state:
            _LAST_OPEN_CHAT_BY_ACCOUNT.pop(account_id, None)
            try:
                page.evaluate("() => { window.__wa_last_clicked_chat = null; }")
            except Exception:
                pass
            logger.info(
                "[%s] OPEN CHAT DEBUG: non-chat drawer/state %r visible; "
                "refusing stale operator chat",
                account_id, non_chat_state
            )
            return None

        # 3. Never switch tabs merely because the chat DOM is temporarily
        # absent.  A missing conversation selector is not proof that the user
        # closed the chat.  Prefer the last confirmed chat for this account
        # for a short grace period; only run the unread workflow when it is
        # explicitly needed elsewhere.
        if not header_data or not isinstance(header_data, dict):
            empty_visible = False
            try:
                empty_state = page.locator('[data-testid="intro-panel"], [data-testid*="intro-panel"]')
                for i in range(min(empty_state.count(), 3)):
                    try:
                        if empty_state.nth(i).is_visible():
                            empty_visible = True
                            break
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("[%s] OPEN CHAT DEBUG: intro-panel check failed: %s", account_id, e)

            cached = None if empty_visible else _LAST_OPEN_CHAT_BY_ACCOUNT.get(account_id)
            if empty_visible:
                logger.info("[%s] OPEN CHAT DEBUG: intro/empty-state visible; refusing stale last-known chat", account_id)
            if cached:
                age = time.time() - float(cached.get('timestamp', 0))
                cached_header = cached.get('header')
                if age <= 15 and isinstance(cached_header, dict) and cached_header.get('chat_name'):
                    header_data = dict(cached_header)
                    logger.info(
                        "[%s] OPEN CHAT DEBUG: DOM has no header; retaining last confirmed chat %r (age=%.1fs)",
                        account_id, header_data.get('chat_name'), age
                    )
            if not header_data:
                logger.info(
                    "[%s] OPEN CHAT DEBUG: No chat header in current DOM; NOT switching to unread tab",
                    account_id
                )

        # Direct exit if still no valid header pane
        if not header_data or not isinstance(header_data, dict):
            return None

        chat_name = (header_data.get('chat_name') or '').strip()
        phone = header_data.get('phone')

        # 4. EARLY EXIT: Check ignored titles BEFORE expensive DOM lookups
        if not chat_name or is_ignored_chat_title(chat_name):
            logger.info("[%s] OPEN CHAT DEBUG: Ignored or empty chat title %r", account_id, chat_name)
            return None

        if header_data.get('is_group'):
            logger.info("[%s] OPEN CHAT DEBUG: Group chat detected; skipping", account_id)
            return None

        logger.info("[%s] OPEN CHAT DEBUG: Processing active client chat_name=%r initial_phone=%r", account_id, chat_name, phone)

        # 5. Phone Normalization & Fallbacks
        if phone:
            phone = normalize_phone(phone)

        # IMPORTANT: the verified conversation-header name may be used only
        # to match an EXISTING Trello client record. It is not a free-form
        # phone lookup and it never consults the historical chat-name cache.
        # This must run BEFORE JID/drawer probing so named Trello clients such
        # as "Zewdu Kebede Araya" are resolved from their stored CRM phone.
        if not phone and chat_name:
            logger.info(
                "[%s] OPEN CHAT DEBUG: no phone in header; trying verified Trello client name=%r before JID/drawer fallbacks",
                account_id,
                chat_name,
            )
            try:
                named_client = lookup_client_by_verified_name(chat_name)
            except Exception as e:
                named_client = None
                logger.warning(
                    "[%s] OPEN CHAT DEBUG: verified-name client lookup failed for %r: %s",
                    account_id, chat_name, e,
                )

            if named_client:
                resolved_phone = named_client.get("whatsapp_phone")
                resolved_phone = normalize_phone(resolved_phone) if resolved_phone else None
                if resolved_phone and resolved_phone not in WORK_PHONE_NUMBERS:
                    logger.info(
                        "[%s] OPEN CHAT DEBUG: Matched existing Trello client by verified name: name=%r phone=%r card=%r",
                        account_id,
                        named_client.get("fullname") or chat_name,
                        resolved_phone,
                        named_client.get("trello_card_id"),
                    )
                    try:
                        save_chat_phone(account_id, chat_name, resolved_phone)
                    except Exception as e:
                        logger.debug(
                            "[%s] OPEN CHAT DEBUG: Could not save verified-name phone cache: %s",
                            account_id, e,
                        )
                    return {
                        "chat_name": chat_name,
                        "phone": resolved_phone,
                        "whatsapp_phone": named_client.get("whatsapp_phone") or resolved_phone,
                        "is_group": False,
                        "fullname": named_client.get("fullname") or chat_name,
                        "trello_card_id": named_client.get("trello_card_id"),
                        "contact_type": named_client.get("contact_type"),
                        "airtable_id": named_client.get("airtable_id"),
                        "resolved_by": "verified_name",
                    }

            logger.info(
                "[%s] OPEN CHAT DEBUG: no unique existing Trello client matched verified name=%r; continuing to WhatsApp JID/drawer resolution",
                account_id,
                chat_name,
            )

        # Fallback A: Chat row JID extraction
        if not phone:
            logger.info("[%s] OPEN CHAT DEBUG: Trying chat-row/JID lookup for %r", account_id, chat_name)
            try:
                row = get_chat_row_by_name(page, chat_name)
                if row:
                    candidates = []
                    try: candidates.append(row.evaluate('(el) => el.outerHTML') or '')
                    except Exception: pass
                    for attr in ('data-id', 'aria-label', 'title'):
                        try:
                            v = row.get_attribute(attr)
                            if v: candidates.append(v)
                        except Exception: pass
                    try:
                        v = row.inner_text(timeout=1000)
                        if v: candidates.append(v)
                    except Exception: pass
                    for candidate in candidates:
                        # First look for canonical WhatsApp JIDs.
                        jid_matches = re.findall(
                            r'(?<!\d)(\d{8,15})(?::\d+)?@(?:c\.us|s\.whatsapp\.net)',
                            candidate,
                            flags=re.I,
                        )
                        for jid in jid_matches:
                            p = normalize_phone(jid)
                            if p and p not in WORK_PHONE_NUMBERS:
                                phone = p
                                logger.info(
                                    "[%s] OPEN CHAT DEBUG: extracted phone %s from WhatsApp JID in chat row",
                                    account_id, p,
                                )
                                break
                        if phone:
                            break

                        # Some WhatsApp builds expose the JID as a bare numeric
                        # data-id (for example true_447... / false_447...) or
                        # inside an href/aria/title rather than an @c.us JID.
                        # Inspect attribute-shaped fragments before considering
                        # looser text matches.
                        for bare in re.findall(
                            r'(?<!\d)(?:true_|false_|status_)?(\d{10,15})(?!\d)',
                            candidate,
                            flags=re.I,
                        ):
                            p = normalize_phone(bare)
                            if p and p not in WORK_PHONE_NUMBERS and len(p) >= 11:
                                phone = p
                                logger.info(
                                    "[%s] OPEN CHAT DEBUG: extracted phone %s from numeric WhatsApp row identifier",
                                    account_id, p,
                                )
                                break
                        if phone:
                            break
            except Exception as e:
                logger.info("[%s] OPEN CHAT DEBUG: Chat-row lookup failed: %s", account_id, e)

        # Fallback B: Direct title phone string. Never interpret a date-like
        # display name such as 07/07/2026 as a phone number.
        if not phone:
            date_like_title = bool(re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", chat_name.strip()))
            direct = None if date_like_title else normalize_phone(chat_name)
            if direct and direct not in WORK_PHONE_NUMBERS:
                phone = direct
                logger.info("[%s] OPEN CHAT DEBUG: Title resolved directly as phone=%s", account_id, phone)

        # Fallback C: Profile Drawer
        if not phone:
            logger.info("[%s] OPEN CHAT DEBUG: Trying contact-info drawer lookup", account_id)
            try:
                # On some builds the clickable title is the only reliable way
                # to open contact info.  Do not use a generic header button:
                # that can be the menu/actions button.
                title_button = page.locator(
                    '[data-testid="conversation-header"] span[dir="auto"]'
                ).first
                if title_button.count() and title_button.is_visible():
                    try:
                        title_button.click(timeout=1500)
                        page.wait_for_timeout(700)
                    except Exception:
                        pass
                phone = extract_phone_from_drawer(page)
            except Exception as e:
                logger.info("[%s] OPEN CHAT DEBUG: Drawer lookup failed: %s", account_id, e)

        if not phone:
            logger.warning("[%s] OPEN CHAT DEBUG: Chat=%r opened but NO PHONE could be resolved", account_id, chat_name)
            return None

        try:
            save_chat_phone(account_id, chat_name, phone)
        except Exception as e:
            logger.debug("[%s] OPEN CHAT DEBUG: Could not save phone cache: %s", account_id, e)

        logger.info("[%s] Successfully identified WhatsApp client: name=%r phone=%r", account_id, chat_name, phone)
        return {'chat_name': chat_name, 'phone': phone}

    except Exception as e:
        logger.exception("[%s] Error identifying open client: %s", account_id, e)
        return None


def get_new_messages(
    whatsapp_account,
    phone,
    page,
    force_process_newest=False,
    recovery_unread=False,
    unread_count=1,
):
    """Scan the open chat for genuinely new messages.

    The first operator-opened scan establishes a baseline instead of treating
    the existing conversation history as new.  Phone-sync scans may set
    ``force_process_newest=True`` because the sidebar change is already the
    trigger proving that a fresh message arrived.

    ``recovery_unread=True`` is an explicit operator recovery signal: messages
    in the unread portion of the chat are eligible even when their persistent
    processed marker already exists.  Trello/Drive idempotency remains in force.
    """
    logger.info("[%s] MESSAGE SCAN: scanning open WhatsApp chat", whatsapp_account)

    non_chat_state = _non_chat_drawer_visible(page)
    if non_chat_state:
        logger.info(
            "[%s] MESSAGE SCAN: non-chat drawer/state %r is visible; skipping",
            whatsapp_account, non_chat_state
        )
        return []

    # Strongest selectors first.  data-id=true_/false_ is much safer than
    # generic div[role=row], which can include non-message rows.  When WhatsApp
    # only exposes generic msg-container wrappers, direction detection below
    # uses the actual conversation pane geometry as a fallback.
    selectors = [
        'div.message-in, div.message-out',
        '[data-testid="msg-container"]',
        '[data-id^="true_"], [data-id^="false_"]',
        '[data-pre-plain-text]'
    ]

    messages = None
    count = 0
    counts = {}
    for selector in selectors:
        try:
            c = page.locator(selector).count()
            counts[selector] = c
            if c:
                messages = page.locator(selector)
                count = c
                break
        except Exception:
            counts[selector] = -1

    logger.info("[%s] MESSAGE SCAN: selector counts=%r", whatsapp_account, counts)

    if count == 0:
        # Diagnostic only: expose likely message-bearing DOM nodes so the next
        # iteration can be based on the actual WhatsApp DOM rather than guesses.
        try:
            diag = page.evaluate("""() => {
                const vw = window.innerWidth || document.documentElement.clientWidth || 0;
                const vh = window.innerHeight || document.documentElement.clientHeight || 0;
                const visible = el => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 10 && r.height > 8 && r.right > vw * 0.35 &&
                           r.left < vw && r.bottom > 0 && r.top < vh &&
                           st.display !== 'none' && st.visibility !== 'hidden';
                };
                const out = [];
                const nodes = Array.from(document.querySelectorAll(
                    '[data-id], [data-pre-plain-text], [data-testid], [role="row"], [class*="message"], [class*="copyable-text"]'
                ));
                for (const el of nodes) {
                    if (!visible(el)) continue;
                    const id = el.getAttribute('data-id') || '';
                    const pre = el.getAttribute('data-pre-plain-text') || '';
                    const testid = el.getAttribute('data-testid') || '';
                    const cls = (typeof el.className === 'string' ? el.className : '').slice(0, 220);
                    const txt = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 180);
                    if (!id && !pre && !testid && !/message|copyable-text/i.test(cls)) continue;
                    const r = el.getBoundingClientRect();
                    out.push({tag: el.tagName, id, pre, testid, cls, text: txt,
                              x: Math.round(r.x), y: Math.round(r.y),
                              w: Math.round(r.width), h: Math.round(r.height)});
                    if (out.length >= 60) break;
                }
                return {vw, vh, nodes: out};
            }""")
            logger.info("[%s] MESSAGE DOM DIAGNOSTIC: %r", whatsapp_account, diag)
        except Exception as e:
            logger.warning("[%s] MESSAGE DOM DIAGNOSTIC failed: %s", whatsapp_account, e)
        logger.info("[%s] MESSAGE SCAN: message wrapper count=0", whatsapp_account)
        return []

    logger.info("[%s] MESSAGE SCAN: using selector=%r count=%d", whatsapp_account,
                next((k for k,v in counts.items() if v == count), 'unknown'), count)

    results = []
    start = max(0, count - 30)

    baseline_key = (whatsapp_account, phone)
    with OPEN_CHAT_BASELINE_LOCK:
        baseline_exists = baseline_key in OPEN_CHAT_BASELINE_STATE
        baseline_hashes = set(OPEN_CHAT_BASELINE_STATE.get(baseline_key, set()))

    # When the operator deliberately marks a chat/message unread, WhatsApp may
    # render an "Unread messages" divider in the conversation.  Use its vertical
    # position to recover the correct portion of history rather than blindly
    # selecting the newest message (which would be wrong if newer messages exist).
    unread_marker_y = None
    if recovery_unread:
        try:
            unread_marker_y = page.evaluate(r"""() => {
                const nodes = Array.from(document.querySelectorAll(
                    '#main [data-testid], #main [aria-label], #main span, #main div'
                ));
                const terms = [
                    'unread messages', 'unread message', 'messages unread',
                    'unread'
                ];
                for (const el of nodes) {
                    const txt = ((el.getAttribute('aria-label') || '') + ' ' +
                                 (el.textContent || '')).replace(/\s+/g, ' ').trim().toLowerCase();
                    if (!txt || !terms.some(t => txt === t || txt.includes(t))) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width > 20 && r.height > 5 && r.bottom > 0) return r.bottom;
                }
                return null;
            }""")
            logger.info(
                "[%s] UNREAD RECOVERY: unread divider y=%r unread_count=%s",
                whatsapp_account, unread_marker_y, unread_count,
            )
        except Exception as e:
            logger.debug(
                "[%s] UNREAD RECOVERY: could not locate unread divider: %s",
                whatsapp_account, e,
            )

    candidates = []
    for i in range(start, count):
        try:
            message_element = messages.nth(i)

            if recovery_unread and unread_marker_y is not None:
                try:
                    box = message_element.bounding_box()
                    if not box or (box.get("y", 0) + box.get("height", 0) / 2) < unread_marker_y:
                        logger.debug(
                            "[%s] UNREAD RECOVERY: idx=%d is before unread divider; skipping",
                            whatsapp_account, i,
                        )
                        continue
                except Exception:
                    pass

            direction_info = get_message_direction(page, message_element, return_source=True)
            if isinstance(direction_info, tuple):
                direction, direction_source = direction_info
            else:
                direction, direction_source = direction_info, None
            if direction not in ("incoming", "outgoing"):
                logger.warning(
                    "[%s] MESSAGE SCAN: message %d direction unresolved (source=%r); skipping rather than guessing",
                    whatsapp_account, i, direction_source,
                )
                continue

            text = extract_message_text(message_element)
            has_attachment = message_has_attachment(message_element)
            timestamp = get_message_timestamp(message_element)
            # A media-only message can legitimately be represented by a second
            # wrapper whose visible text is only the timestamp (e.g. `13:34`).
            # IMPORTANT: decide this BEFORE rejecting timestamp-only wrappers.
            # Earlier versions dropped the wrapper before reaching the attachment
            # recovery logic, making some media messages permanently invisible.
            media_only_wrapper = bool(
                has_attachment and text and
                re.fullmatch(r"\d{1,2}:\d{2}", text.strip()) and not timestamp
            )

            if text and re.fullmatch(r"\d{1,2}:\d{2}", text.strip()) and not timestamp and not media_only_wrapper:
                logger.info("[%s] MESSAGE FILTER: idx=%d reason=timestamp-only-wrapper text=%r attachment=%s", whatsapp_account, i, text, has_attachment)
                continue
            if not text and not has_attachment:
                logger.info(
                    "[%s] MESSAGE FILTER: idx=%d reason=no-text-no-attachment direction=%s source=%r",
                    whatsapp_account, i, direction, direction_source,
                )
                continue

            message_id = extract_message_id(message_element)
            attachment_identity = extract_attachment_identity(message_element) if has_attachment else ""

            if media_only_wrapper:
                logger.info(
                    "[%s] MESSAGE FILTER: idx=%d media-only timestamp wrapper retained for attachment recovery text=%r",
                    whatsapp_account, i, text,
                )
                text = ""

            message_key = build_message_identity(
                whatsapp_account=whatsapp_account,
                phone=phone,
                message=text,
                direction=direction,
                message_id=message_id,
                timestamp=timestamp,
                attachment_identity=attachment_identity,
            )
            message_hash = build_hash(
                whatsapp_account, phone, text, timestamp, extra=direction,
                message_id=message_id,
                attachment_identity=attachment_identity,
                message_key=message_key,
            )

            candidates.append({
                "text": text or "[WhatsApp attachment]",
                "direction": direction,
                "timestamp": timestamp,
                "element": message_element,
                "message_hash": message_hash,
                "has_attachment": has_attachment,
                "message_id": message_id,
                "message_key": message_key,
                "attachment_identity": attachment_identity,
                "direction_source": direction_source,
                "index": i,
                "recovery_unread": bool(recovery_unread),
            })
            logger.info(
                "[%s] MESSAGE DETAIL: idx=%d direction=%s source=%r id=%r timestamp=%r text=%r attachment=%s hash=%s baseline=%s",
                whatsapp_account, i, direction, direction_source, message_id, timestamp,
                text, has_attachment, message_hash, message_hash in baseline_hashes,
            )
            logger.debug("[%s] MESSAGE SCAN: candidate %s message index=%d text=%r",
                         whatsapp_account, direction, i, text)
        except Exception as e:
            logger.warning("[%s] MESSAGE SCAN: could not inspect message %d: %s",
                           whatsapp_account, i, e)

    # Collapse duplicate DOM wrappers for the same logical media message.
    # WhatsApp can expose both the real message wrapper and a separate
    # timestamp-only wrapper containing the same attachment. Prefer the
    # richer candidate (real text/message id) and keep the media-only wrapper
    # only when it is the sole representation.
    deduped_candidates = []
    seen_ids = set()
    seen_attachment_keys = set()
    for item in candidates:
        mid = str(item.get("message_id") or "").strip()
        att = str(item.get("attachment_identity") or "").strip()
        if mid:
            if mid in seen_ids:
                logger.info(
                    "[%s] MESSAGE DEDUPE: dropping duplicate message-id wrapper idx=%s id=%r",
                    whatsapp_account, item.get("index"), mid,
                )
                continue
            seen_ids.add(mid)
        # Only use attachment identity as a dedupe key when it contains a
        # concrete DOM identity, not merely the generic msg-container testid.
        att_key = att if (att and ("data-id=" in att or "aria-label=" in att or "title=" in att or "alt=" in att)) else ""
        if att_key and att_key in seen_attachment_keys:
            replaced = False
            for j, prev in enumerate(deduped_candidates):
                if prev.get("attachment_identity") == att_key:
                    prev_rich = bool(prev.get("text") and prev.get("text") != "[WhatsApp attachment]")
                    curr_rich = bool(item.get("text") and item.get("text") != "[WhatsApp attachment]")
                    if curr_rich and not prev_rich:
                        deduped_candidates[j] = item
                    replaced = True
                    break
            if replaced:
                logger.info(
                    "[%s] MESSAGE DEDUPE: collapsing duplicate attachment wrapper idx=%s attachment_identity=%r",
                    whatsapp_account, item.get("index"), att_key[:240],
                )
                continue
        elif att_key:
            seen_attachment_keys.add(att_key)
        deduped_candidates.append(item)
    candidates = deduped_candidates
    logger.info(
        "[%s] MESSAGE DEDUPE SUMMARY: candidates_after_dedupe=%d",
        whatsapp_account, len(candidates),
    )

    # Remove messages already persisted.  The database remains the final
    # duplicate-safety layer, while the in-memory baseline protects against
    # treating the existing conversation history as new on first open.
    #
    # IMPORTANT: an attachment may be detected and baselined before the actual
    # download/upload processing succeeds.  Therefore a baselined attachment
    # MUST remain eligible until the database records the message as processed.
    # This is especially important during ordinary open-chat polling, where
    # force_process_newest is not set.
    unseen = []
    for item in candidates:
        msg_hash = item.get("message_hash")
        if not msg_hash:
            continue
        is_attachment = bool(
            item.get("has_attachment")
            or item.get("text") == "[WhatsApp attachment]"
        )

        processed = already_processed(msg_hash)
        if processed and item.get("recovery_unread"):
            logger.info(
                "[%s] UNREAD RECOVERY: allowing previously processed candidate through duplicate gate: %r",
                whatsapp_account, item.get("text"),
            )
        elif processed:
            if is_attachment and not attachment_upload_completed_for_message(
                whatsapp_account, msg_hash
            ):
                logger.info(
                    "[%s] MESSAGE SCAN: processed attachment has no completed Drive upload; re-queueing for attachment retry: %r",
                    whatsapp_account, item.get("text"),
                )
            else:
                logger.info(
                    "[%s] MESSAGE FILTER: idx=%d reason=already-processed hash=%s",
                    whatsapp_account, item.get("index"), msg_hash,
                )
                continue

        if msg_hash in baseline_hashes and not is_attachment:
            logger.info(
                "[%s] MESSAGE FILTER: idx=%d reason=baseline-non-attachment hash=%s",
                whatsapp_account, item.get("index"), msg_hash,
            )
            continue

        if msg_hash in baseline_hashes and is_attachment:
            logger.info(
                "[%s] MESSAGE SCAN: baselined attachment remains eligible because it is not yet processed: %r",
                whatsapp_account, item.get("text"),
            )

        logger.info(
            "[%s] MESSAGE FILTER: idx=%d ACCEPTED unseen processed=%s baseline=%s attachment=%s",
            whatsapp_account, item.get("index"), processed, msg_hash in baseline_hashes, is_attachment,
        )
        unseen.append(item)

    logger.info(
        "[%s] MESSAGE SCAN SUMMARY: wrappers=%d candidates=%d baseline_exists=%s baseline_hashes=%d unseen=%d force_process_newest=%s",
        whatsapp_account, count, len(candidates), baseline_exists, len(baseline_hashes), len(unseen),
        force_process_newest,
    )

    if recovery_unread:
        # Explicit unread recovery is independent of the normal first-open
        # baseline.  Return every candidate in the unread portion so each one
        # can reconcile missing Trello/Drive work.
        results = list(unseen)
        logger.info(
            "[%s] UNREAD RECOVERY: returning %d candidate(s) for reconciliation",
            whatsapp_account, len(results),
        )
    elif not baseline_exists:
        # Establish the entire visible history as the initial baseline.
        # During normal operator-open monitoring nothing is emitted on this
        # first observation, preventing historical messages from being sent
        # to Trello.
        with OPEN_CHAT_BASELINE_LOCK:
            OPEN_CHAT_BASELINE_STATE[baseline_key] = {
                item["message_hash"] for item in candidates if item.get("message_hash")
            }

        if force_process_newest and candidates:
            # The sidebar change is the trigger.  Select the newest current
            # message candidate even when it was included in the newly-created
            # baseline.  This prevents attachment-only messages from becoming
            # permanently invisible merely because they were first observed
            # before the processing step ran.
            newest = candidates[-1]
            if newest.get("message_hash") and already_processed(newest.get("message_hash")):
                results = []
                logger.info(
                    "[%s] MESSAGE SCAN: first sync newest candidate is already processed; skipping duplicate: %r",
                    whatsapp_account, newest.get("text"),
                )
            else:
                results = [newest]
                logger.info(
                    "[%s] MESSAGE BASELINE: force_process_newest selected newest idx=%s id=%r hash=%s direction=%s text=%r",
                    whatsapp_account, newest.get("index"), newest.get("message_id"), newest.get("message_hash"),
                    newest.get("direction"), newest.get("text"),
                )
                logger.info(
                    "[%s] MESSAGE SCAN: first sync scan; forcing newest triggered message candidate: %r",
                    whatsapp_account, newest.get("text"),
                )
        else:
            results = []
            logger.info(
                "[%s] MESSAGE BASELINE: establishing first observation for chat=%s candidate_count=%d force_process_newest=%s hashes=%s",
                whatsapp_account, phone, len(candidates), force_process_newest,
                [item.get("message_hash") for item in candidates],
            )
    else:
        if force_process_newest:
            # A sidebar fingerprint change is an independent signal that the
            # currently opened chat changed.  Do NOT require the newest message
            # to be absent from the in-memory baseline: attachment-only messages
            # can legitimately have been observed during an earlier DOM pass
            # without ever being processed/uploaded.
            #
            # The database remains the authoritative duplicate gate.  Therefore
            # we select the newest real message candidate from the current DOM
            # and let process_message()/already_processed() decide whether it
            # still needs processing.  This specifically repairs the failure
            # mode where an attachment was detected, baselined, and then lost.
            if candidates:
                newest = candidates[-1]
                if newest.get("message_hash") and already_processed(newest.get("message_hash")):
                    results = []
                    logger.info(
                        "[%s] MESSAGE SCAN: triggered sync newest candidate is already processed; skipping duplicate: %r",
                        whatsapp_account, newest.get("text"),
                    )
                else:
                    results = [newest]
                    logger.info(
                        "[%s] MESSAGE BASELINE: force_process_newest selected newest idx=%s id=%r hash=%s direction=%s text=%r",
                        whatsapp_account, newest.get("index"), newest.get("message_id"), newest.get("message_hash"),
                        newest.get("direction"), newest.get("text"),
                    )
                    logger.info(
                        "[%s] MESSAGE SCAN: triggered sync; forcing newest DOM candidate even if present in baseline: %r",
                        whatsapp_account, newest.get("text"),
                    )
            else:
                results = []
                logger.info(
                    "[%s] MESSAGE SCAN: triggered sync but no message candidate exists in current DOM",
                    whatsapp_account,
                )
        else:
            results = unseen

        with OPEN_CHAT_BASELINE_LOCK:
            state = OPEN_CHAT_BASELINE_STATE.setdefault(baseline_key, set())
            state.update(item["message_hash"] for item in candidates if item.get("message_hash"))

    logger.info("[%s] get_new_messages(): detected %d NEW candidate message(s).",
                whatsapp_account, len(results))
    return results


def main():

    logger.info(
        "=" * 80
    )

    logger.info(
        "STARTING MULTI-WHATSAPP / TRELLO SERVICE"
    )
    logger.info("WHATSAPP LISTENER VERSION: V22 stable-message-identity + unread-recovery-guards")

    logger.info(
        "=" * 80
    )

    # --------------------------------------------------------
    # CONFIG
    # --------------------------------------------------------

    validate_configuration()

    logger.info(
        "Board: %s",
        TRELLO_BOARD_NAME
    )

    logger.info(
        "Configured Trello lists: %s",
        ", ".join(
            get_trello_list_ids()
        )
    )

    logger.info(
        "Configured WhatsApp accounts:"
    )

    for account_id, account in WHATSAPP_ACCOUNTS.items():

        logger.info(
            "  %s -> %s -> %s",
            account_id,
            account["name"],
            account["profile"]
        )

    # --------------------------------------------------------
    # DATABASE
    # --------------------------------------------------------

    init_db()

    migrate_db()

    if not database_health_check():

        raise RuntimeError(
            "Database health check failed. "
            "Refusing to start."
        )

    # --------------------------------------------------------
    # TRELLO HEALTH
    # --------------------------------------------------------

    if not trello_health_check():

        raise RuntimeError(
            "Trello API health check failed."
        )

    # --------------------------------------------------------
    # INITIAL TRELLO SYNC
    # --------------------------------------------------------

    sync_trello_clients()

    # --------------------------------------------------------
    # HEALTH SERVER
    # --------------------------------------------------------

    health_server = start_health_server()

    # --------------------------------------------------------
    # PERIODIC TRELLO SYNC
    # --------------------------------------------------------

    trello_thread = threading.Thread(
        target=trello_sync_loop,
        name="TrelloSync",
        daemon=True
    )

    trello_thread.start()

    # --------------------------------------------------------
    # START ALL WHATSAPP ACCOUNTS
    # --------------------------------------------------------

    threads = []

    for account_id, account_config in WHATSAPP_ACCOUNTS.items():

        logger.info(
            "[%s] Starting account worker thread...",
            account_id
        )

        thread = threading.Thread(
            target=run_whatsapp_account,
            args=(
                account_id,
                account_config
            ),
            name=f"WhatsApp-{account_id}",
            daemon=False
        )

        thread.start()

        threads.append(
            thread
        )

        # Pause 5 seconds between browser spawns to prevent CPU/memory spikes
        time.sleep(5)

    logger.info(
        "ALL WHATSAPP ACCOUNTS STARTED"
    )

    # --------------------------------------------------------
    # WAIT FOR ACCOUNT THREADS
    # --------------------------------------------------------

    try:

        while not SHUTDOWN_EVENT.is_set():

            alive = any(
                thread.is_alive()
                for thread in threads
            )

            if not alive:
                break

            time.sleep(1)

    except KeyboardInterrupt:

        request_shutdown()

    finally:

        logger.info(
            "Waiting for account workers to stop..."
        )

        SHUTDOWN_EVENT.set()

        for thread in threads:

            thread.join(
                timeout=30
            )

        try:

            health_server.shutdown()

            health_server.server_close()

        except Exception as e:

            logger.warning(
                "Could not stop health server: %s",
                e
            )

        logger.info(
            "Multi-WhatsApp service stopped."
        )

# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    install_signal_handlers()

    main()