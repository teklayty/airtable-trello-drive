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

# =========================================================
# FIND DUPLICATE CARDS BY PHONE
# =========================================================
def find_duplicate_card(phone):
    phone = clean_phone(phone)

    for list_id in EXISTING_LIST_IDS:
        cards = trello_get(
            f"/lists/{list_id}/cards",
            {
                "fields": "id,name,desc,idList"
            }
        )

        for card in cards:
            text = f"{card.get('name', '')} {card.get('desc', '')}"

            matches = re.findall(
                r"(?:\+44|44|0)?[\d\s()\-]{9,}",
                text
            )

            for match in matches:
                existing_phone = clean_phone(match)

                if existing_phone and existing_phone[-9:] == phone[-9:]:
                    return card

    return None

# =========================================================
# EXTRACT DRIVE LINK
# =========================================================    
def get_drive_links_from_card(card_id):
    links = []

    try:
        card = trello_get(
            f"/cards/{card_id}",
            {
                "fields": "desc"
            }
        )

        desc = card.get("desc", "")

        log(f"DESCRIPTION FOUND:\n{desc}")

        matches = re.findall(
            r'https://drive\.google\.com[^\s\])"]+',
            desc
        )

        for link in matches:

            cleaned = (
                link.strip()
                .lstrip("[")
                .rstrip("]>.,)")
            )

            if cleaned not in links:
                links.append(cleaned)


    except Exception as e:
        log(f"Drive extraction failed: {e}")

    log(f"DRIVE LINKS FOUND: {links}")

    return list(set(links))


# =========================================================
# MOVE DUPLICATE DRIVE
# ========================================================= 

CLEANUP_LIST_ID = os.getenv("TRELLO_CLEANUP_LIST_ID")

def move_duplicate_to_cleanup(card):
    try:
        trello_put(
            f"/cards/{card['id']}",
            {
                "idList": CLEANUP_LIST_ID
            }
        )

        trello_post(
            f"/cards/{card['id']}/labels",
            {
                "name": "Duplicate,Delete Me!",
                "color": "black"
            }
        )

        log(
            f"Moved duplicate card: "
            f"{card['name']}"
        )

    except Exception as e:
        log(f"Failed to move duplicate: {e}")

def add_comment(
    card_id,
    record_id,
    last_modified,
    coss_notes,
    scc_notes,
    cas_notes,
    gdrive,
    referral_drive_links,
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

    # =====================================================
    # CLIENT DOCUMENTS
    # =====================================================

    gdrive_clean = str(gdrive).strip() if gdrive else ""

    if gdrive_clean:
        lines.extend([
            "",
            "📁 Client Documents:",
            gdrive_clean
        ])

    # =====================================================
    # REFERRAL DOCUMENTS
    # =====================================================

    if referral_drive_links:

        valid_links = []
        seen = set()

        for link in referral_drive_links:

            cleaned = (
                link.strip()
                .rstrip("]>.,)")
                .lstrip("[")
            )

            if not cleaned:
                continue

            if cleaned == gdrive_clean:
                continue

            if cleaned in seen:
                continue

            seen.add(cleaned)
            valid_links.append(cleaned)


        if valid_links:

            log(f"REFERRAL LINKS RAW: {referral_drive_links}")
            log(f"REFERRAL LINKS CLEANED: {valid_links}")


            lines.append("")
            lines.append("📁 Referral Documents:")

            for link in valid_links:
                lines.append(link)

    # =====================================================
    # COSS SUPPORT FORM
    # =====================================================

    lines.extend([
        "",
        "📝 CoSS Support Form:",
        form_link
    ])

    comment_text = "\n".join(lines)

    log("====================================")
    log("COMMENT TO BE POSTED:")
    log(comment_text)
    log("====================================")

    trello_post(
        f"/cards/{card_id}/actions/comments",
        {
            "text": comment_text
        }
    )

    log(f"✅ Comment added to card {card_id}")


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

    duplicate_card = find_duplicate_card(contact_phone)

    referral_drive_links = []

    if duplicate_card:

        referral_drive_links = get_drive_links_from_card(
            duplicate_card["id"]
        )

        log(
            f"Duplicate card ID: {duplicate_card['id']}"
            f"Referral drive links found: {referral_drive_links}"
        )



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
        referral_drive_links,
        fullname
    )

    if duplicate_card:
        move_duplicate_to_cleanup(duplicate_card)


    # =========================================================
    # REFERRAL LABEL
    # =========================================================

    ensure_referral_label(
        card_id,
        referral_requested_date
    )

    

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
