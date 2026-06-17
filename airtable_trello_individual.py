import os
import re
import requests
from datetime import datetime, timedelta
from dateutil.parser import parse as parse_iso
import logging

# =========================================================
# CONFIG
# =========================================================

TRELLO_API_KEY = os.getenv("TRELLO_API_KEY")
TRELLO_API_TOKEN = os.getenv("TRELLO_API_TOKEN")
EXISTING_LIST_IDS = os.getenv("TRELLO_EXISTING_LIST_IDS")
AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN")

AIRTABLE_BASE_ID = "appt4PI9krGalheLk"
AIRTABLE_TABLE_ID = "tblPBsPUOoPDzknkj"
AIRTABLE_VIEW_ID = "viw24xRpomUhGJEIG"

BASE_URL = "https://api.trello.com/1"
AUTH = {"key": TRELLO_API_KEY, "token": TRELLO_API_TOKEN}
AIRTABLE_HEADERS = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}

EXISTING_LIST_IDS = [lid.strip() for lid in EXISTING_LIST_IDS.split(",") if lid.strip()]
TODAY = datetime.now()

# =========================================================
# LOGGING SETUP
# =========================================================

logging.basicConfig(
    filename="../Airtable2Trello/sync.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def log(msg):
    print(msg)
    logging.info(msg)

# =========================================================
# HELPERS
# =========================================================

def parse_date(value):
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except:
            continue
    return None

def get_field(fields, key):
    value = fields.get(key, "")
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value).strip()

def clean_phone(phone):
    digits = "".join(filter(str.isdigit, str(phone)))
    if digits.startswith("44"):
        return digits
    if digits.startswith("0"):
        return f"44{digits[1:]}"
    return f"44{digits}"

def build_airtable_link(record_id):
    return f"https://airtable.com/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}/{AIRTABLE_VIEW_ID}/{record_id}?blocks=hide"

def calculate_card_position(eviction_date):
    if not eviction_date:
        return 999999999
    days_until = (eviction_date - TODAY).days
    return max(days_until, 0)

def get_case_status(eviction_date):
    if not eviction_date:
        return "🟡 Pending documents"
    days = (eviction_date - TODAY).days
    if days < 0 or days < 14:
        return "🔴 Urgent eviction"
    if days < 30:
        return "🟡 Pending documents"
    return "🟢 Active"

# =========================================================
# TRELLO API
# =========================================================

def trello_post(path, data):
    r = requests.post(f"{BASE_URL}{path}", params=AUTH, data=data)
    r.raise_for_status()
    return r.json()

# =========================================================
# AIRTABLE API
# =========================================================

def airtable_get_record(record_id):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}/{record_id}"
    r = requests.get(url, headers=AIRTABLE_HEADERS)
    r.raise_for_status()
    return r.json()

# =========================================================
# CREATE TRELLO CARD FROM AIRTABLE ID
# =========================================================

def create_card_from_airtable(record_id, target_list_id):
    record = airtable_get_record(record_id)
    fields = record.get("fields", {})

    # Full name
    first = get_field(fields, "Forename/First name(s)")
    last = get_field(fields, "Surname/Last Name(s)")
    fullname = f"{first} {last}".strip()
    if not fullname:
        log("❌ Missing full name, cannot create card")
        return

    # Phone numbers
    contact_number = get_field(fields, "Contact number")
    whatsapp_number = get_field(fields, "WhatsApp number") or contact_number
    if not contact_number:
        log("❌ Missing contact number, cannot create card")
        return
    whatsapp_number_clean = clean_phone(whatsapp_number)

    # Eviction date & Google Drive link
    eviction_raw = get_field(fields, "MEARS Eviction Date")
    eviction_date = parse_date(eviction_raw)
    gdrive = get_field(fields, "Link")

    # Extra fields
    spring_issue = get_field(fields, "SPRING - Issue^")
    coss_support = get_field(fields, "CoSS Support Provided")
    supporting_docs = get_field(fields, "Supporting Documents attached")
    coss_support_1y = get_field(fields, "CoSS Support Provided (1y)")
    coss_notes = get_field(fields, "CoSS Notes")
    scc_notes = get_field(fields, "SCC Notes")

    # Description uses WhatsApp number
    desc = (
        f"AIRTABLE_RECORD_ID:\n{record_id}\n\n"
        f"WhatsApp Web:\nhttps://web.whatsapp.com/send?phone={whatsapp_number_clean}\n\n"
        f"WhatsApp Phone:\nhttps://wa.me/{whatsapp_number_clean}\n\n"
        f"SPRING - Issue:\n{spring_issue}\n\n"
        f"CoSS Support Provided:\n{coss_support}\n\n"
        f"Supporting Documents attached:\n{supporting_docs}\n\n"
        f"CoSS Support Provided (1y):\n{coss_support_1y}\n\n"
        f"Airtable Link:\n{build_airtable_link(record_id)}\n\n"
        f"Google Drive:\n{gdrive}"
    )

    # Card name uses Contact number
    card_name = f"{fullname} – Phone: {contact_number} – Eviction date: {eviction_raw}"

    # Create card
    card = trello_post("/cards", {
        "idList": target_list_id,
        "name": card_name,
        "desc": desc,
        "due": eviction_date.isoformat() if eviction_date else None,
        "pos": calculate_card_position(eviction_date)
    })

    # Add comments
    comment_text = ""
    if coss_notes:
        comment_text += f"CoSS Notes:\n{coss_notes.strip()}\n\n"
    if scc_notes:
        comment_text += f"SCC Notes:\n{scc_notes.strip()}\n\n"
    if comment_text:
        trello_post(f"/cards/{card['id']}/actions/comments", {"text": comment_text.strip()})

    # Labels
    status = get_case_status(eviction_date)
    if "Urgent" in status:
        trello_post(f"/cards/{card['id']}/labels", {"color": "red"})
    elif "Pending" in status:
        trello_post(f"/cards/{card['id']}/labels", {"color": "yellow"})
    else:
        trello_post(f"/cards/{card['id']}/labels", {"color": "green"})

    log(f"✅ Created card: {card_name} in list {target_list_id}")

# =========================================================
# RUN INTERACTIVE
# =========================================================

if __name__ == "__main__":
    record_id = input("Enter Airtable Record ID: ").strip()
    print("Available lists:")
    for i, lid in enumerate(EXISTING_LIST_IDS):
        print(f"{i+1}. {lid}")
    choice = int(input(f"Select target list (1-{len(EXISTING_LIST_IDS)}): ").strip())
    target_list_id = EXISTING_LIST_IDS[choice-1]

    create_card_from_airtable(record_id, target_list_id)