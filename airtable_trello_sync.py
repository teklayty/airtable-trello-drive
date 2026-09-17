import os
import re
import requests
from datetime import datetime, timedelta, timezone
from dateutil.parser import parse as parse_iso
import logging
import urllib.parse
import json
import base64


# =========================================================
# CONFIG
# =========================================================

TRELLO_API_KEY = os.getenv("TRELLO_API_KEY")
TRELLO_API_TOKEN = os.getenv("TRELLO_API_TOKEN")

CREATE_LIST_ID = os.getenv("TRELLO_CREATE_LIST_ID")
EXISTING_LIST_IDS = os.getenv("TRELLO_EXISTING_LIST_IDS")
RAW_ALLOWED_STAFF = os.getenv("TRELLO_ALLOWED_STAFF", "")

AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN")

AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_ID = os.getenv("AIRTABLE_TABLE_ID")
AIRTABLE_VIEW_ID = os.getenv("AIRTABLE_VIEW_ID")

URGENT_LIST_NAME = "URGENT – Eviction within 14 days"

BASE_URL = "https://api.trello.com/1"

AUTH = {"key": TRELLO_API_KEY, "token": TRELLO_API_TOKEN}

AIRTABLE_HEADERS = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}

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

# Marker used so the Airtable sync can update only its own Trello comment.
# Comments created by WhatsApp, operators, or other systems are preserved.
AIRTABLE_COMMENT_MARKER = f"📌**SU AIRTABLE Record and Drive Docs**"


TODAY = datetime.now()
RECENT_ARCHIVE_DAYS = 10
TWO_MONTHS_AGO = TODAY - timedelta(days=60)

ALLOWED_STAFF = {s.strip().lower() for s in RAW_ALLOWED_STAFF.split(",") if s.strip()}

EXISTING_LIST_IDS = [lid.strip() for lid in EXISTING_LIST_IDS.split(",") if lid.strip()]

CLEANUP_LIST_ID = os.getenv("TRELLO_CLEANUP_LIST_ID")


# =========================================================
# LOGGING SETUP
# =========================================================

logging.basicConfig(
    filename="/home/springvolunteer/Airtable2Trello/sync.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def log(msg, level="info"):
    print(msg)
    if level == "info":
        logging.info(msg)
    elif level == "error":
        logging.error(msg)
    elif level == "warning":
        logging.warning(msg)

# =========================================================
# HELPERS
# =========================================================

def encode_airtable_id(record_id):
    return base64.urlsafe_b64encode(record_id.encode()).decode()

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

def extract_airtable_id_from_card(value):
    print("=" * 60)
    print(f"DEBUG: Raw input: {value}")
    
    if not value:
        print("DEBUG: Value is empty or None")
        return ""
    
    value = str(value)
    print(f"DEBUG: Cast to string: {value}")
    
    match = re.search(r"(rec[a-zA-Z0-9]{14})", value)
    
    if match:
        airtable_id = match.group(1)
        print(f"DEBUG: Match found: {airtable_id}")
        return airtable_id
    else:
        print("DEBUG: No match found")
        return ""

def get_field(fields, key):
    value = fields.get(key, "")
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value).strip()

def build_airtable_link(record_id):
    return build_notes_link(record_id)


def get_case_status(eviction_date):
    if not eviction_date:
        return "🟡 Pending documents"
    days = (eviction_date - TODAY).days
    if days < 0 or days < 14:
        return "🔴 Urgent eviction"
    if days < 30:
        return "🟡 Pending documents"
    return "🟢 Active"

def calculate_card_position(created_at):
    """
    Return a stable Trello position based on the Airtable record creation time.

    The client cards are ordered chronologically Old -> New.  The first card
    in the list is reserved for the instruction card, so all client-card
    positions are deliberately greater than zero.
    """
    if not created_at:
        return 10**12

    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)

    # Trello sorts numeric positions from low to high.  Using the timestamp
    # gives a deterministic Old -> New order without disturbing the instruction
    # card at the top of the list.
    return created_at.timestamp() + 1


def generate_notes_anchor_link(record_id):
    return build_notes_link(record_id)



# notes_link = build_notes_link(record_id)


def generate_interface_link(record_id):
    return build_notes_link(record_id)


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
# Airtable Comments API Function
# =========================================================

def get_airtable_comments(record_id):
    """
    Fetch comments from Airtable (includes timestamps)
    """
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}/{record_id}/comments"

    r = requests.get(url, headers=AIRTABLE_HEADERS)
    r.raise_for_status()

    data = r.json()
    return data.get("comments", [])


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

# =========================================================
# GET RETAINED REFERRAL LINK
# ========================================================= 
def get_retained_referral_link(card_id):

    try:
        card = trello_get(
            f"/cards/{card_id}",
            {"fields": "desc"}
        )

        desc = card.get("desc", "")

        lines = desc.splitlines()

        for i, line in enumerate(lines):

            if "REFERRAL_DOCUMENTS:" in line:

                if i + 1 < len(lines):

                    url = lines[i + 1].strip()

                    if "drive.google.com" in url:
                        return url

    except Exception as e:
        log(f"Could not read retained referral link: {e}", "error")

    return ""



# =========================================================
# Format Comments Nicely
# =========================================================

def format_airtable_comments(comments):
    """
    Format Airtable comments into clean Markdown for Trello
    """
    if not comments:
        return ""

    lines = []

    for c in comments:
        text = c.get("text", "").strip()
        created = c.get("createdTime", "")

        # Format timestamp nicely
        try:
            dt = parse_iso(created)
            created_str = dt.strftime("%d %b %Y, %H:%M")
        except:
            created_str = created

        lines.append(f"- 🕒 {created_str}\n  {text}")

    return "\n\n".join(lines)


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
# COMMENTS
# =========================================================

def get_airtable_comment_actions(card_id):
    """Return all Airtable-sync-owned Trello comment actions for a card."""
    actions = trello_get(
        f"/cards/{card_id}/actions",
        {
            "filter": "commentCard",
            "limit": 1000,
        }
    )

    owned = []
    for action in actions:
        text = action.get("data", {}).get("text", "")
        if AIRTABLE_COMMENT_MARKER in text:
            owned.append(action)

    return owned


def get_card_comment_actions(card_id):
    """Return recent comment actions for a card, newest first."""
    return trello_get(
        f"/cards/{card_id}/actions",
        {
            "filter": "commentCard",
            "limit": 1000,
        }
    )


def get_latest_non_airtable_comment(card_id):
    """Return the newest non-Airtable comment action, if one exists."""
    actions = get_card_comment_actions(card_id)

    for action in actions:
        text = action.get("data", {}).get("text", "") or ""
        if AIRTABLE_COMMENT_MARKER not in text:
            return action

    return None


def delete_airtable_comments(card_id, keep_action_id=None):
    """Delete duplicate Airtable-sync comments only.

    All other Trello comments (WhatsApp/operator/manual comments, etc.) are
    left untouched. When keep_action_id is supplied, that single comment is
    retained and every other Airtable-owned comment is deleted.
    """
    actions = get_airtable_comment_actions(card_id)

    for action in actions:
        action_id = action.get("id")
        if keep_action_id and action_id == keep_action_id:
            continue

        try:
            trello_delete(f"/actions/{action_id}")
            log(
                f"Deleted duplicate Airtable sync comment "
                f"{action_id} from card {card_id}"
            )
        except Exception as e:
            log(
                f"Could not delete Airtable sync comment {action_id}: {e}",
                "error"
            )


def delete_airtable_comment(card_id, action_id):
    """Delete one specific Airtable-owned comment."""
    if not action_id:
        return

    try:
        trello_delete(f"/actions/{action_id}")
        log(
            f"Deleted Airtable sync comment {action_id} from card {card_id} "
            "to promote it back to the top of the comment feed"
        )
    except Exception as e:
        log(
            f"Could not delete Airtable sync comment {action_id}: {e}",
            "error"
        )
        raise


def update_airtable_comment(action_id, comment_text):
    """Update an existing Airtable-owned Trello comment in place."""
    r = trello_put(
        f"/actions/{action_id}/comments",
        {"text": comment_text}
    )
    return r


def create_airtable_comment(card_id, comment_text):
    """Create a new Airtable-owned comment and return the Trello action."""
    result = trello_post(
        f"/cards/{card_id}/actions/comments",
        {"text": comment_text}
    )
    return result


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

    # Only manage the Airtable-owned comment.
    # Preserve WhatsApp/operator/manual comments on the same Trello card.
    existing_airtable_comments = get_airtable_comment_actions(card_id)

    # Keep one Airtable comment if one already exists; remove any duplicates.
    keep_action_id = None
    if existing_airtable_comments:
        # Keep the newest Airtable-owned comment.
        existing_airtable_comments.sort(
            key=lambda a: a.get("date", "") or a.get("id", ""),
            reverse=True
        )
        keep_action_id = existing_airtable_comments[0].get("id")
        delete_airtable_comments(card_id, keep_action_id=keep_action_id)

    notes_link = build_notes_link(record_id)
    form_link = build_prefill_form_link(fullname, record_id)

    lines = [AIRTABLE_COMMENT_MARKER]

    lines.append(f"🟦 CoSS Notes:\n{notes_link}\n")

    if scc_notes and str(scc_notes).strip():
        lines.append(f"🟨 SCC Notes:\n{notes_link}\n")

    if cas_notes and str(cas_notes).strip():
        lines.append(f"🟩 CAS Notes:\n{notes_link}\n")

    drive_links = []

    if gdrive and str(gdrive).strip():
        drive_links.append(
            f"📁 Client Documents:\n{gdrive}"
        )

    if drive_links:
        lines.append("")
        lines.extend(drive_links)

    lines.extend([
        "",
        "📝 CoSS Support Form:",
        form_link
    ])

    comment_text = "\n".join(lines)

    log("ABOUT TO POST/UPDATE COMMENT")
    log(comment_text)

    # Trello's comment actions are ordered newest first.  If a WhatsApp or
    # other non-Airtable comment was added after our Airtable comment, recreate
    # the Airtable comment so it becomes the newest/top comment again.
    promote_to_top = False
    if keep_action_id:
        try:
            latest_non_airtable = get_latest_non_airtable_comment(card_id)
            airtable_date = existing_airtable_comments[0].get("date", "")
            latest_other_date = (latest_non_airtable or {}).get("date", "")

            if latest_other_date and (not airtable_date or latest_other_date > airtable_date):
                promote_to_top = True
                log(
                    f"PROMOTING AIRTABLE COMMENT TO TOP: card={card_id} "
                    f"airtable_date={airtable_date} "
                    f"latest_other_date={latest_other_date}"
                )
        except Exception as e:
            log(
                f"Could not determine whether Airtable comment needs promotion "
                f"on card {card_id}: {e}",
                "warning"
            )

    if promote_to_top:
        delete_airtable_comment(card_id, keep_action_id)
        create_airtable_comment(card_id, comment_text)
        log("AIRTABLE COMMENT RECREATED AT TOP")
    elif keep_action_id:
        update_airtable_comment(keep_action_id, comment_text)
        log(
            f"AIRTABLE COMMENT UPDATED IN PLACE "
            f"action={keep_action_id}"
        )
    else:
        create_airtable_comment(card_id, comment_text)
        log("AIRTABLE COMMENT CREATED")

    log("COMMENT SYNC SUCCESSFUL")


def generate_note_links(record_id, coss_notes, scc_notes, cas_notes):
    notes_link = build_notes_link(record_id)

    links = []

    if coss_notes and str(coss_notes).strip():
        links.append(("🟦 CoSS Notes", notes_link))

    if scc_notes and str(scc_notes).strip():
        links.append(("🟨 SCC Notes", notes_link))

    if cas_notes and str(cas_notes).strip():
        links.append(("🟩 CAS Notes", notes_link))

    return links


def generate_section_links(record_id):
    notes_link = build_notes_link(record_id)

    return {
        "coss": notes_link,
        "scc": notes_link,
        "cas": notes_link
    }



# =========================================================
# ADD ATTACHMENTS
# =========================================================    
def add_attachments(card_id, record_id, fullname, gdrive):
    delete_system_attachments(card_id)
    return

# =========================================================
# GET DRIVE LINK
# =========================================================  
def get_drive_link_from_card(card_id):

    try:
        card = trello_get(
            f"/cards/{card_id}",
            {
                "fields": "name,desc"
            }
        )

        desc = card.get("desc", "")

        matches = re.findall(
            r'https?://[^\s\]]+',
            desc
        )

        log(f"CARD: {card['name']}")
        log(f"URLS FOUND: {matches}")

        for link in matches:

            link = link.strip()
            link = link.lstrip("[")
            link = link.rstrip("]>.,)")

            if (
                "drive.google.com" in link
                or "docs.google.com" in link
            ):
                log(f"✅ DRIVE LINK FOUND: {link}")
                return link

    except Exception as e:
        log(f"Failed to extract Drive link: {e}", "error")

    return ""







# =========================================================
# GET DRIVE ATTACHMENT
# ========================================================= 
def get_drive_attachment(card_id):
    attachments = trello_get(
        f"/cards/{card_id}/attachments"
    )

    for att in attachments:
        url = att.get("url", "")

        if "drive.google.com" in url:
            return url

    return None


# =========================================================
# AIRTABLE API
# =========================================================

def airtable_get_all_records(limit=60):
    """
    Fetch records exactly in the order they appear in the Airtable view.
    No additional sorting is applied.
    """
    records = []
    offset = None

    while True:
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}"

        params = {
            "view": AIRTABLE_VIEW_ID,
            "pageSize": 100
        }

        if offset:
            params["offset"] = offset

        r = requests.get(
            url,
            headers=AIRTABLE_HEADERS,
            params=params
        )
        r.raise_for_status()

        data = r.json()

        records.extend(data.get("records", []))

        offset = data.get("offset")

        if not offset:
            break

    log(f"Fetched {len(records)} Airtable records")

    return records[:limit]

# =========================================================
# MOVE TO URGENT LIST
# =========================================================

def extract_phone_from_card(card):
    text = card.get("name", "") + " " + card.get("desc", "")

    matches = re.findall(
        r"(?:\+44|44|0)?[\d\s()\-]{9,}",
        text
    )

    phones = []

    for match in matches:
        cleaned = clean_phone(match)

        if len(cleaned) >= 11:
            phones.append(cleaned)

    if not phones:
        return ""

    return max(phones, key=len)



# =========================================================
# LOAD EXISTING CARDS & CLEAN DUPLICATES
# =========================================================

def load_existing_cards():
    """
    Load existing cards from Trello and index them by:
    1. Airtable record ID
    2. Phone number (for duplicate detection)

    Also archives duplicate Airtable cards automatically.
    """
    cards = {}
    phone_index = {}   # ✅ NEW

    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_ARCHIVE_DAYS)

    for lid in EXISTING_LIST_IDS:
        fetched_cards = trello_get(f"/lists/{lid}/cards", {
            "cards": "all",
            "fields": "id,name,desc,closed,dateLastActivity,shortUrl,idList"
        })

        for card in fetched_cards:
            is_archived = card.get("closed", False)

            # Skip old archived cards
            if is_archived:
                try:
                    last_activity = parse_iso(card["dateLastActivity"])
                    if last_activity < cutoff:
                        continue
                except:
                    continue

            # =====================================================
            # ✅ Build PHONE INDEX (for duplicate detection)
            # =====================================================
            phone = extract_phone_from_card(card)
            log(f"Extracted phone from card '{card.get('name')}': {phone}")


            if phone:
                if phone not in phone_index:
                    phone_index[phone] = []
                phone_index[phone].append(card)

            log(f"📞 Phone index built: { {k: len(v) for k,v in phone_index.items()} }")


            # =====================================================
            # ✅ Existing Airtable ID logic (unchanged)
            # =====================================================
            airtable_id = extract_airtable_id_from_card(card)
            if not airtable_id:
                continue

            if airtable_id in cards:
                # Duplicate found — keep the most recent active card
                existing_card = cards[airtable_id]["card"]

                existing_last_activity = parse_iso(existing_card["dateLastActivity"])
                current_last_activity = parse_iso(card["dateLastActivity"])

                if current_last_activity > existing_last_activity:
                    trello_put(f"/cards/{existing_card['id']}", {"closed": "true"})
                    log(
                        f"⚠️ Archived older duplicate card: {existing_card['id']} "
                        f"for Airtable ID {airtable_id}",
                        "warning"
                    )
                    cards[airtable_id] = {"card": card, "closed": is_archived}
                else:
                    trello_put(f"/cards/{card['id']}", {"closed": "true"})
                    log(
                        f"⚠️ Archived duplicate card: {card['id']} "
                        f"for Airtable ID {airtable_id}",
                        "warning"
                    )
                continue

            cards[airtable_id] = {"card": card, "closed": is_archived}
            log(f"FOUND EXISTING: {card.get('name')} | {airtable_id}")

    # ✅ IMPORTANT: return BOTH
    return cards, phone_index

# =========================================================
# CLEAN CARD
# =========================================================

def clean_card(card_id):
    for cl in trello_get(f"/cards/{card_id}/checklists"):
        trello_delete(f"/checklists/{cl['id']}")
    trello_put(f"/cards/{card_id}", {"desc": ""})

def phone_matches(p1, p2):
    if not p1 or not p2:
        return False
    return p1[-9:] == p2[-9:]


def delete_system_attachments(card_id):
    attachments = trello_get(f"/cards/{card_id}/attachments")

    for att in attachments:
        name = att.get("name", "")

        # ✅ Only delete attachments created by your system
        if any(keyword in name for keyword in [
            "Airtable Record",
            "Google Drive",
            "CoSS Notes",
            "SCC Notes",
            "CAS Notes",
            "CoSS Support Form"
        ]):
            try:
                trello_delete(f"/cards/{card_id}/attachments/{att['id']}")
            except Exception as e:
                log(f"Could not delete attachment: {e}", "error")


# =========================================================
# MARK AND MOVE TO CLEANUP
# =========================================================
def mark_and_move_to_cleanup(card):
    card_id = card["id"]

    log(f"🧹 Marking as 'Delete Me': {card.get('name')}")

    labels = trello_get(f"/cards/{card_id}/labels")

    # ✅ Add "Delete Me!" label if missing
    if not any(lbl.get("name") == "Delete Me!" for lbl in labels):
        trello_post(
            f"/cards/{card_id}/labels",
            {"name": "Duplicate,Delete Me!", "color": "black"}
        )

    # ✅ OPTIONAL: move to cleanup list (keep this if you want movement)
    if CLEANUP_LIST_ID and card.get("idList") != CLEANUP_LIST_ID:
        trello_put(
            f"/cards/{card_id}",
            {"idList": CLEANUP_LIST_ID}
        )


# =========================================================
# NORMALIZE UK PHONE
# =========================================================
def clean_phone(phone):
    if not phone:
        return ""

    digits = re.sub(r"\D", "", str(phone))

    if digits.startswith("00"):
        digits = digits[2:]

    if digits.startswith("44"):
        return digits

    if digits.startswith("0"):
        return "44" + digits[1:]

    if len(digits) >= 10:
        return "44" + digits[-10:]

    return digits

# =========================================================
# MAIN
# =========================================================

log("🚀 Starting Optimized Airtable → Trello Sync")
existing_cards, phone_index = load_existing_cards()


# Find urgent list ID
board_id = trello_get(f"/lists/{CREATE_LIST_ID}")["idBoard"]
urgent_list_id = None
for lst in trello_get(f"/boards/{board_id}/lists"):
    if lst["name"] == URGENT_LIST_NAME:
        urgent_list_id = lst["id"]

records = airtable_get_all_records(limit=60)

log("========== FIRST RECORDS FROM AIRTABLE ==========")

for i, r in enumerate(records, start=1):
    fields = r.get("fields", {})

    log(
        f"{i:02d}. "
        f"{r['id']} | "
        f"createdTime={r.get('createdTime')} | "
        f"Created={fields.get('Created')} | "
        f"{fields.get('Forename/First name(s)', '')} "
        f"{fields.get('Surname/Last Name(s)', '')}"
    )

log("===============================================")


created = updated = 0

for record in records:
    try:
        airtable_id = record.get("id")
        fields = record.get("fields", {})

        # created_at = parse_date(get_field(fields, "Created"))
        created_at = parse_iso(record["createdTime"]).replace(tzinfo=None)
        if created_at and created_at < TWO_MONTHS_AGO:
            log(f"Skipping {airtable_id}: older than two months")
            continue  # skip old records

        first = get_field(fields, "Forename/First name(s)")
        last = get_field(fields, "Surname/Last Name(s)")
        fullname = f"{first} {last}".strip()
        if not fullname:
            log(f"Skipping {airtable_id}: no full name")
            continue

        contact_phone = get_field(fields, "Contact number")
        whatsapp_phone = get_field(fields, "WhatsApp number") or contact_phone
        if not contact_phone and not whatsapp_phone:
            log(f"Skipping {airtable_id}: no phone number")
            continue

        staff = get_field(fields, "(IA) Name of staff/volunteer").strip().lower()

        
        # Skip records where staff is not allowed
        if staff not in ALLOWED_STAFF:
            log(
                f"⏭ Skipped record {airtable_id}: "
                f"Staff '{staff}' not in allowed list",
                "⚠️ warning"
            )
            continue
        log(log("(IA) Name of staff/volunteer"))
        
        eviction_raw = get_field(fields, "MEARS Eviction Date")
        eviction_date = parse_date(eviction_raw)
        gdrive = get_field(fields, "Link")

        airtable_link = build_airtable_link(airtable_id)
        whatsapp_number = clean_phone(whatsapp_phone)

        card_name = f"{fullname} – Phone: {contact_phone} – Eviction date: {eviction_raw}"

        spring_issue = get_field(fields, "SPRING - Issue^")
        coss_support = get_field(fields, "CoSS Support Provided")
        supporting_docs = get_field(fields, "Supporting Documents attached")
        coss_support_1y = get_field(fields, "CoSS Support Provided (1y)")
        coss_notes = get_field(fields, "CoSS Notes")
        scc_notes = get_field(fields, "SCC Notes")
        cas_notes = get_field(fields, "CAS Notes")
        last_modified = get_field(fields, "Last modified")
        referral_drive_link = ""
        referral_requested_date = get_field(
            fields,
            "SCC - Referral requested (date)."
        )

        # Using PHONE to detect duplications
        ia_phone = whatsapp_phone

        ia_clean = clean_phone(ia_phone)
        wa_clean = clean_phone(whatsapp_phone)

        print(f"IA: {ia_phone} -> {ia_clean}")
        print(f"WA: {whatsapp_phone} -> {wa_clean}")
        print(f"Match: {ia_clean == wa_clean}")


        log(f"BEFORE BUILD DESC referral_drive_link={referral_drive_link}")


        desc = (
            f"WhatsApp Web:\nhttps://web.whatsapp.com/send?phone={whatsapp_number}\n\n"
            f"WhatsApp Phone:\nhttps://wa.me/{whatsapp_number}\n\n"
            f"SPRING - Issue:\n{spring_issue}\n\n"
            f"CoSS Support Provided:\n{coss_support}\n\n"
            f"Supporting Documents attached:\n{supporting_docs}\n\n"
            f"'':{airtable_id}\n\n"
            )

        
        retained_referral_link = ""

        if airtable_id in existing_cards:

            log(f"AIRTABLE ID = {airtable_id}")
            log(f"FOUND IN EXISTING = {airtable_id in existing_cards}")

            existing = existing_cards[airtable_id]
            card = existing["card"]
            card_id = card["id"]

            log(f"🔄 Updating existing card: {card_name}")

            if existing["closed"]:
                trello_put(
                    f"/cards/{card_id}",
                    {"closed": "false"}
                )

            log(f"EXISTING CARD COUNT = {len(existing_cards)}")

            # Save referral link BEFORE wiping description
            retained_referral_link = get_retained_referral_link(card_id)
            updated += 1


        else:

            log(f"🆕 Creating new card: {card_name}")

            card = trello_post(
                "/cards",
                {
                    "idList": CREATE_LIST_ID,
                    "name": card_name,
                    "desc": "",
                    "due": eviction_date.isoformat() if eviction_date else None,
                    "pos": calculate_card_position(created_at)
                }
            )

            card_id = card["id"]

            existing_cards[airtable_id] = {
                "card": card,
                "closed": False
            }

            created += 1

            # =========================================================
            # ✅ DUPLICATE CHECK (MOVE THIS INTO YOUR MAIN LOOP)
            # =========================================================

            phone = clean_phone(
                whatsapp_phone or contact_phone
            )


            if phone:

                for stored_phone, cards_list in phone_index.items():

                    log(
                        f"COMPARE CREATE | incoming={phone} "
                        f"| stored={stored_phone} "
                        f"| match={phone_matches(phone, stored_phone)}"
                    )


                    if not phone_matches(phone, stored_phone):
                        continue

                    for other_card in cards_list:

                        other_id = other_card["id"]

                        if other_id == card_id:
                            continue

                        card_details = trello_get(
                            f"/cards/{other_id}",
                            {"fields": "name,desc"}
                        )

                        other_airtable_id = extract_airtable_id_from_card(
                            card_details.get("desc", "")
                        )

                        if other_airtable_id:
                            continue

                        log(
                            f"CARD={other_card['name']} "
                            f"AIRTABLE_ID={other_airtable_id}"
                        )


                        existing_drive = get_drive_link_from_card(other_id)

                        log("================================")
                        log(f"MATCH FOUND")
                        log(f"PHONE={phone}")
                        log(f"STORED={stored_phone}")
                        log(f"CARD={other_card['name']}")
                        log("================================")


                        if existing_drive:

                            referral_drive_link = existing_drive

                            log(
                                f"📁 Found duplicate drive link: "
                                f"{referral_drive_link}"
                            )

                            log(f"AFTER DETECTION referral_drive_link={referral_drive_link}")


                        mark_and_move_to_cleanup(other_card)
                        break
        # If updating, use retained copy
        if not referral_drive_link:
            referral_drive_link = retained_referral_link

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

            f"AIRTABLE_RECORD_ID:\n"
            f"{airtable_id}\n"
        )

        if referral_drive_link:

            desc += (
                "\n\n"
                "REFERRAL_DOCUMENTS:\n"
                f"{referral_drive_link}\n"
            )

        log("FINAL DESC:")
        log(desc)

        clean_card(card_id)

        trello_put(
            f"/cards/{card_id}",
            {
                "name": card_name,
                "desc": desc,
                "due": eviction_date.isoformat() if eviction_date else None,
                "pos": calculate_card_position(created_at)
            }
        )



        log(
                f"ADDING REFERRAL LINK BACK TO DESC: "
                f"{referral_drive_link}"
            )

    
        # =========================================================
        # Remove attachments
        # =========================================================

        attachments = trello_get(f"/cards/{card_id}/attachments")

        for att in attachments:
            trello_delete(f"/cards/{card_id}/attachments/{att['id']}")

        
        log(f"Client docs: {gdrive}")
        log(f"Referral docs: {referral_drive_link}")
        log(f"CLIENT DOCS = {gdrive}")
        log(f"REFERRAL DOCS = {referral_drive_link}")


        log(f"INSIDE add_comment() referral={referral_drive_link}")
        log("====================================")
        log(f"FINAL referral_drive_link = {referral_drive_link}")
        log("====================================")

        log("========= COMMENT INPUT =========")
        log(f"gdrive={gdrive}")
        log(f"referral_drive_link={referral_drive_link}")
        log("================================")


        

        log(f"FINAL referral_drive_link = {referral_drive_link}")


        
        # ✅ Add secure Airtable link instead of raw notes
        add_comment(
            card_id,
            airtable_id,
            last_modified,
            coss_notes,
            scc_notes,
            cas_notes,
            gdrive,
            fullname
        )


        log(
            f"DEBUG: {fullname} | SCC referral date = {repr(referral_requested_date)}"
        )

        ensure_referral_label(
            card_id,
            referral_requested_date
        )


        for key in fields.keys():
            if "SCC" in key:
                log(f"SCC FIELD: {key} = {fields[key]}")



        
        log(f"PHONE INDEX: {phone_index.keys()}")



    except Exception as e:
        log(f"❌ Error processing {airtable_id}: {e}", "error")

log(f"\n✅ Complete. Cards created: {created}, updated: {updated}")