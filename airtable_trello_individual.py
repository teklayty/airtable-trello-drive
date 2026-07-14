import os
import re
import requests
from datetime import datetime, timedelta
from dateutil.parser import parse as parse_iso
import logging
import urllib.parse

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
# NEW SETUP AFTER AIRTABLE CHANGES for CoSS/SCC/CAS Notes
# =========================================================
AIRTABLE_NOTES_PAGE_ID = os.getenv(
    "AIRTABLE_NOTES_PAGE_ID",
    "pagLsUMtGqYgvu2a5"
)

AIRTABLE_NOTES_PARAM = os.getenv(
    "AIRTABLE_NOTES_PARAM",
    "Pwx3z"
)

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

# =========================================================
# Airtable NOTES LINKS ON Comments
# =========================================================
def build_notes_link(record_id):
    return (
        f"https://airtable.com/"
        f"{AIRTABLE_BASE_ID}/"
        f"{AIRTABLE_NOTES_PAGE_ID}"
        f"?{AIRTABLE_NOTES_PARAM}={record_id}"
    )

# =========================================================
# Open CoSS Support for client
# ========================================================= 
def build_prefill_form_link(fullname, airtable_id):
    base_url = (
        "https://airtable.com/"
        "appt4PI9krGalheLk/"
        "pag30p3pX8fc0lFkn/form"
    )

    value = f"{fullname} ({airtable_id})"
    encoded_value = urllib.parse.quote(value)

    return (
        f"{base_url}"
        f"?prefill_Client%20ID%20and%20Full%20Name={encoded_value}"
    )

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
# COMMENTS
# =========================================================

def delete_all_comments(card_id):
    actions = trello_get(f"/cards/{card_id}/actions", {"filter": "commentCard"})
    for action in actions:
        try:
            trello_delete(f"/actions/{action.get('id')}")
        except Exception as e:
            log(f"Could not delete comment: {e}", "error")


def add_comment(
    card_id,
    record_id,
    last_modified,
    coss_notes,
    scc_notes,
    cas_notes,
    gdrive,
    fullname
):
    delete_all_comments(card_id)

    notes_link = build_notes_link(record_id)
    form_link = build_prefill_form_link(fullname, record_id)

    lines = [
        f"📌 Last updated: {last_modified}",
        "",
        "🔒 Notes stored in Airtable:",
        ""
    ]

    if coss_notes and str(coss_notes).strip():
        lines.append(f"🟦 CoSS Notes:\n{notes_link}\n")

    if scc_notes and str(scc_notes).strip():
        lines.append(f"🟨 SCC Notes:\n{notes_link}\n")

    if cas_notes and str(cas_notes).strip():
        lines.append(f"🟩 CAS Notes:\n{notes_link}\n")

    if gdrive and str(gdrive).strip():
        lines.extend([
            "",
            "📁 Google Drive:",
            gdrive
        ])

    lines.extend([
        "",
        "📝 CoSS Support Form:",
        form_link
    ])

    comment_text = "\n".join(lines)

    trello_post(
        f"/cards/{card_id}/actions/comments",
        {"text": comment_text}
    )

# =========================================================
# TRELLO API
# =========================================================

def trello_get(path, extra_params=None):
    params = dict(AUTH)
    if extra_params:
        params.update(extra_params)
    r = requests.get(f"{BASE_URL}{path}", params=params)
    r.raise_for_status()
    return r.json()

def trello_post(path, data):
    r = requests.post(f"{BASE_URL}{path}", params=AUTH, data=data)
    r.raise_for_status()
    return r.json()

def trello_put(path, data):
    r = requests.put(f"{BASE_URL}{path}", params=AUTH, data=data)
    r.raise_for_status()
    return r.json()

def trello_delete(path):
    r = requests.delete(f"{BASE_URL}{path}", params=AUTH)
    r.raise_for_status()

# =========================================================
# LABELING CARDS IF NO REFERRAL TO COUNCIL
# =========================================================
def ensure_referral_label(card_id, referral_requested_date):
    labels = trello_get(f"/cards/{card_id}/labels")

    referral_label = None

    for lbl in labels:
        if lbl.get("name") == "No Referral made to SCC":
            referral_label = lbl
            break

    value = str(referral_requested_date).strip().lower()

    has_referral = value not in {
        "",
        "none",
        "null",
        "[]"
    }

    log(
        f"DEBUG referral value='{value}' "
        f"has_referral={has_referral}"
    )

    if not has_referral:
        if not referral_label:
            trello_post(
                f"/cards/{card_id}/labels",
                {
                    "name": "No Referral made to SCC",
                    "color": "orange"
                }
            )

    else:
        if referral_label:
            trello_delete(
                f"/cards/{card_id}/idLabels/{referral_label['id']}"
            )


# =========================================================
# CREATE TRELLO CARD FROM AIRTABLE ID
# =========================================================

def create_card_from_airtable(record_id, target_list_id):
    record = airtable_get_record(record_id)
    fields = record.get("fields", {})

    # =========================================================
    # BASIC DETAILS
    # =========================================================

    first = get_field(fields, "Forename/First name(s)")
    last = get_field(fields, "Surname/Last Name(s)")
    fullname = f"{first} {last}".strip()

    if not fullname:
        log("❌ Missing full name")
        return

    contact_phone = get_field(fields, "Contact number")
    whatsapp_phone = get_field(fields, "WhatsApp number") or contact_phone

    if not contact_phone and not whatsapp_phone:
        log("❌ Missing phone number")
        return

    whatsapp_number = clean_phone(whatsapp_phone)

    eviction_raw = get_field(fields, "MEARS Eviction Date")
    eviction_date = parse_date(eviction_raw)

    gdrive = get_field(fields, "Link")

    spring_issue = get_field(fields, "SPRING - Issue^")
    coss_support = get_field(fields, "CoSS Support Provided")
    supporting_docs = get_field(fields, "Supporting Documents attached")
    coss_support_1y = get_field(fields, "CoSS Support Provided (1y)")

    coss_notes = get_field(fields, "CoSS Notes")
    scc_notes = get_field(fields, "SCC Notes")
    cas_notes = get_field(fields, "CAS Notes")

    last_modified = get_field(fields, "Last modified")

    referral_requested_date = get_field(
        fields,
        "SCC - Referral requested (date)."
    )

    # =========================================================
    # CARD NAME
    # =========================================================

    card_name = (
        f"{fullname} – Phone: {contact_phone} "
        f"– Eviction date: {eviction_raw}"
    )

    # =========================================================
    # DESCRIPTION
    # SAME AS MAIN SYNC
    # =========================================================

    desc = (
        f"WhatsApp Web:\n"
        f"https://web.whatsapp.com/send?phone={whatsapp_number}\n\n"

        f"WhatsApp Phone:\n"
        f"https://wa.me/{whatsapp_number}\n\n"

        f"SPRING - Issue:\n"
        f"{spring_issue}\n\n"

        f"CoSS Support Provided:\n"
        f"{coss_support}\n\n"

        f"Supporting Documents attached:\n"
        f"{supporting_docs}\n\n"

        f"'':{record_id}\n\n"
    )

    # =========================================================
    # CREATE CARD
    # =========================================================

    card = trello_post(
        "/cards",
        {
            "idList": target_list_id,
            "name": card_name,
            "desc": desc,
            "due": (
                eviction_date.isoformat()
                if eviction_date else None
            ),
            "pos": calculate_card_position(eviction_date)
        }
    )

    card_id = card["id"]

    # =========================================================
    # COMMENTS
    # SAME AS MAIN SYNC
    # =========================================================

    add_comment(
        card_id,
        record_id,
        last_modified,
        coss_notes,
        scc_notes,
        cas_notes,
        gdrive,
        fullname
    )

    # =========================================================
    # REFERRAL LABEL
    # =========================================================

    ensure_referral_label(
        card_id,
        referral_requested_date
    )

    # =========================================================
    # STATUS LABELS
    # SAME AS MAIN SYNC
    # =========================================================

    status = get_case_status(eviction_date)

    if "Urgent" in status:
        trello_post(
            f"/cards/{card_id}/labels",
            {"color": "red"}
        )

    elif "Pending" in status:
        trello_post(
            f"/cards/{card_id}/labels",
            {"color": "yellow"}
        )

    else:
        trello_post(
            f"/cards/{card_id}/labels",
            {"color": "green"}
        )

    log(
        f"✅ Created card: "
        f"{card_name} "
        f"({record_id})"
    )

    return card

# =========================================================
# RUN INTERACTIVE
# =========================================================

# if __name__ == "__main__":
#     record_id = input("Enter Airtable Record ID: ").strip()
#     print("Available lists:")
#     for i, lid in enumerate(EXISTING_LIST_IDS):
#         print(f"{i+1}. {lid}")
#     choice = int(input(f"Select target list (1-{len(EXISTING_LIST_IDS)}): ").strip())
#     target_list_id = EXISTING_LIST_IDS[choice-1]

#     create_card_from_airtable(record_id, target_list_id)

if __name__ == "__main__":
    record_id = input("Enter Airtable Record ID: ").strip()

    print("\nAvailable lists:")

    for i, lid in enumerate(EXISTING_LIST_IDS, start=1):
        try:
            list_info = trello_get(f"/lists/{lid}")
            print(f"{i}. {list_info['name']}")
        except Exception as e:
            print(f"{i}. {lid} (Error loading name: {e})")

    choice = int(
        input(
            f"\nSelect target list (1-{len(EXISTING_LIST_IDS)}): "
        ).strip()
    )

    target_list_id = EXISTING_LIST_IDS[choice - 1]

    create_card_from_airtable(record_id, target_list_id)
