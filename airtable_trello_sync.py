#!/usr/init/env python3
"""
Airtable MCP to Trello Sync Pipeline (Smart Fallback Version)
-------------------------------------------------------------
Configured with smart phone regex fallback and flexible field parsing 
to match the successful individual script behavior.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
import requests
from dateutil.parser import parse as parse_iso
from pydantic import AnyUrl

from mcp import ClientSession
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientMetadata, OAuthToken

# =========================================================
# CONFIG
# =========================================================

TRELLO_API_KEY = os.getenv("TRELLO_API_KEY")
TRELLO_API_TOKEN = os.getenv("TRELLO_API_TOKEN")

CREATE_LIST_ID = os.getenv("TRELLO_CREATE_LIST_ID")
EXISTING_LIST_IDS = os.getenv("TRELLO_EXISTING_LIST_IDS", "")
RAW_ALLOWED_STAFF = os.getenv("TRELLO_ALLOWED_STAFF", "")

AIRTABLE_MCP_URL = os.getenv("AIRTABLE_MCP_URL", "https://mcp.airtable.com/mcp")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "appt4PI9krGalheLk")
AIRTABLE_TABLE_ID = os.getenv("AIRTABLE_TABLE_ID", "tblPBsPUOoPDzknkj")
AIRTABLE_INTERFACE_ID = os.getenv("AIRTABLE_INTERFACE_ID", "pbdDOjqDonDnlt0nx")
AIRTABLE_PAGE_ID = os.getenv("AIRTABLE_PAGE_ID", "pagLtfDQqmKmrEIBa")

URGENT_LIST_NAME = "URGENT – Eviction within 14 days"
BASE_URL = "https://api.trello.com/1"
AUTH = {"key": TRELLO_API_KEY, "token": TRELLO_API_TOKEN}

# =========================================================
# NOTES & DRIVE CONFIG
# =========================================================
AIRTABLE_NOTES_PAGE_ID = os.getenv(
    "AIRTABLE_NOTES_PAGE_ID",
    "pagLtfDQqmKmrEIBa"
)

AIRTABLE_NOTES_PARAM = os.getenv(
    "AIRTABLE_NOTES_PARAM",
    "Pwx3z"
)

AIRTABLE_COMMENT_MARKER = "📌**SU AIRTABLE Record and Drive Docs**"

GOOGLE_DRIVE_ROOT_FOLDER_ID = os.getenv(
    "GOOGLE_DRIVE_ROOT_FOLDER_ID",
    "17QIHfTwZtPAv3o3Nhur4AOYJzq8e13So"
)
GOOGLE_DRIVE_ROOT_FOLDER_URL = (
    "https://drive.google.com/drive/folders/"
    f"{GOOGLE_DRIVE_ROOT_FOLDER_ID}?usp=drive_link"
)

GOOGLE_DRIVE_CREDENTIALS_FILE = os.getenv(
    "GOOGLE_DRIVE_CREDENTIALS_FILE",
    os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
)
GOOGLE_DRIVE_API_KEY = os.getenv("GOOGLE_DRIVE_API_KEY", "")
GOOGLE_DRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

DRIVE_NO_LINK_LABEL = "No Drive Link"
DRIVE_FOLDER_EMPTY_LABEL = "Drive Folder Empty"

RECENT_ARCHIVE_DAYS = 10

ALLOWED_STAFF = {s.strip().lower() for s in RAW_ALLOWED_STAFF.split(",") if s.strip()}
EXISTING_LIST_IDS = [lid.strip() for lid in EXISTING_LIST_IDS.split(",") if lid.strip()]
CLEANUP_LIST_ID = os.getenv("TRELLO_CLEANUP_LIST_ID")

READ_ONLY_SCOPES = (
    "data.records:read "
    "schema.bases:read "
    "data.recordComments:read "
    "workspacesAndBases:read"
)

TOKEN_FILE = Path(__file__).parent / ".airtable_tokens.json"

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
# PERSISTENT OAUTH TOKEN STORAGE FOR MCP
# =========================================================

class PersistentTokenStorage:
    def __init__(self, token_path: Path):
        self.token_path = token_path

    async def get_tokens(self) -> OAuthToken | None:
        if not self.token_path.exists():
            return None
        try:
            data = json.loads(self.token_path.read_text(encoding="utf-8"))
            return OAuthToken(
                access_token=data.get("access_token"),
                refresh_token=data.get("refresh_token"),
                token_type=data.get("token_type", "Bearer"),
                expires_in=data.get("expires_in"),
            )
        except Exception as e:
            log(f"Could not load saved tokens: {e}", "warning")
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        try:
            data = {
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "token_type": tokens.token_type,
                "expires_in": tokens.expires_in,
            }
            self.token_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            log(f"Could not save tokens to disk: {e}", "error")

    async def get_client_info(self):
        return None

    async def set_client_info(self, client_info):
        pass


class CallbackCapture:
    def __init__(self):
        self.callback_url = None

    async def __call__(self, url):
        print("\n[First-time setup] Open this Airtable OAuth URL in your browser:")
        print("=" * 78)
        print(url)
        print("=" * 78)
        self.callback_url = input("\nPaste the FULL localhost:3030/callback URL here: ").strip()


async def run_oauth_mcp_session(work_callback):
    callback = CallbackCapture()

    async def redirect_handler(url):
        await callback(url)

    async def callback_handler():
        if not callback.callback_url:
            raise RuntimeError("OAuth callback URL was not supplied.")
        parsed = urlparse(callback.callback_url)
        params = parse_qs(parsed.query)
        if params.get("error"):
            raise RuntimeError(params.get("error_description", params["error"])[0])
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        iss = params.get("iss", [None])[0]
        if not code:
            raise RuntimeError("OAuth callback URL did not contain a code.")
        return AuthorizationCodeResult(code=code, state=state, iss=iss)

    provider = OAuthClientProvider(
        server_url=AIRTABLE_MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Airtable Trello Interface Sync",
            redirect_uris=[AnyUrl("http://localhost:3030/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=READ_ONLY_SCOPES,
        ),
        storage=PersistentTokenStorage(TOKEN_FILE),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )

    timeout = httpx2.Timeout(30.0, read=300.0)

    async with httpx2.AsyncClient(auth=provider, timeout=timeout) as http_client:
        async with streamable_http_client(AIRTABLE_MCP_URL, http_client=http_client) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                await work_callback(session)


async def call_mcp_structured(session, tool_name, arguments):
    result = await session.call_tool(tool_name, arguments)
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if structured is not None:
        return structured

    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except Exception:
            pass
    return None

# =========================================================
# MCP RECORD FETCHING
# =========================================================

async def airtable_mcp_get_all_records(session, limit=2000):
    log("Fetching records via Airtable Interface MCP...")
    
    payload = await call_mcp_structured(
        session,
        "list_records_for_page",
        {
            "baseId": AIRTABLE_BASE_ID,
            "interfaceId": AIRTABLE_INTERFACE_ID,
            "pageId": AIRTABLE_PAGE_ID,
        }
    )

    if not isinstance(payload, dict):
        log("Failed to fetch structured records from MCP response.", "error")
        return []

    by_table = payload.get("recordsByTableId", {})
    raw_records = by_table.get(AIRTABLE_TABLE_ID, [])
    
    if not isinstance(raw_records, list):
        if isinstance(by_table, dict) and len(by_table) > 0:
            raw_records = next(iter(by_table.values()))
        else:
            raw_records = []

    formatted_records = []
    for rec in raw_records:
        rec_id = rec.get("id")
        cell_map = rec.get("cellValuesByFieldId", {})
        
        fields = {}
        if isinstance(cell_map, dict):
            for fid, val in cell_map.items():
                fields[fid] = val

        if "fields" in rec and isinstance(rec["fields"], dict):
            fields.update(rec["fields"])

        formatted_records.append({
            "id": rec_id,
            "createdTime": rec.get("createdTime", datetime.now(timezone.utc).isoformat()),
            "fields": fields,
        })

    log(f"Fetched {len(formatted_records)} Airtable records via Interface MCP")
    return formatted_records[:limit]

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

def extract_airtable_id_from_card(value):
    if isinstance(value, dict):
        value = value.get("desc", "") or value.get("name", "")
    if not value:
        return ""
    value = str(value)
    match = re.search(r"(rec[a-zA-Z0-9]{14})", value)
    return match.group(1) if match else ""

def get_field(fields, *keys):
    priority_keys = ["fld7l03Yg7qOpECmp", "fldW1oINrdIcidIWx", "fldmnUq5R51iXqDuf"] + list(keys)
    for key in priority_keys:
        if key in fields:
            value = fields[key]
            if isinstance(value, list):
                return ", ".join(map(str, value))
            val_str = str(value).strip()
            if val_str:
                return val_str
    return ""

def verify_staff_allowed(fields, allowed_staff_set):
    """Verifies staff using 'Staff/Volunteer Name' or linked record IDs."""
    if not allowed_staff_set:
        return True

    staff_val = get_field(fields, "Staff/Volunteer Name", "(IA) Name of staff/volunteer", "Staff", "fldStaff").lower()
    if staff_val:
        if staff_val in allowed_staff_set:
            return True
        for allowed in allowed_staff_set:
            if allowed in staff_val:
                return True

    for val in fields.values():
        if not val:
            continue
        val_str = str(val)
        found_rec_ids = re.findall(r"(rec[a-zA-Z0-9]{14})", val_str, re.IGNORECASE)
        for rec_id in found_rec_ids:
            if rec_id.lower() in allowed_staff_set:
                return True
                
    return False

def build_notes_link(record_id):
    return (
        f"https://airtable.com/"
        f"{AIRTABLE_BASE_ID}/"
        f"{AIRTABLE_NOTES_PAGE_ID}"
        f"?{AIRTABLE_NOTES_PARAM}={record_id}"
    )

def calculate_card_position(created_at):
    if not created_at:
        return 10**12
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
    return created_at.timestamp() + 1

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

def get_retained_referral_link(card_id):
    try:
        card = trello_get(f"/cards/{card_id}", {"fields": "desc"})
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

def extract_drive_folder_id(value):
    if not value:
        return ""
    value = str(value).strip()
    if not value:
        return ""
    patterns = (
        r"drive\.google\.com/drive/folders/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return ""

_drive_access_token = None
_drive_access_token_expires_at = 0

def get_google_drive_access_token():
    global _drive_access_token, _drive_access_token_expires_at
    now = int(time.time())
    if _drive_access_token and now < (_drive_access_token_expires_at - 60):
        return _drive_access_token

    credentials_file = GOOGLE_DRIVE_CREDENTIALS_FILE.strip()
    if not credentials_file:
        return ""

    try:
        with open(credentials_file, "r", encoding="utf-8") as fh:
            credentials = json.load(fh)

        client_email = credentials.get("client_email")
        private_key = credentials.get("private_key")
        if not client_email or not private_key:
            return ""

        try:
            import jwt
        except ImportError:
            return ""

        issued_at = now
        payload = {
            "iss": client_email,
            "scope": GOOGLE_DRIVE_SCOPE,
            "aud": GOOGLE_DRIVE_TOKEN_URL,
            "iat": issued_at,
            "exp": issued_at + 3600,
        }

        assertion = jwt.encode(payload, private_key, algorithm="RS256")
        response = requests.post(
            GOOGLE_DRIVE_TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=30,
        )
        response.raise_for_status()
        token_data = response.json()

        _drive_access_token = token_data.get("access_token", "")
        _drive_access_token_expires_at = issued_at + int(
            token_data.get("expires_in", 3600)
        )
        return _drive_access_token
    except Exception as e:
        log(f"Could not obtain Google Drive access token: {e}", "warning")
        return ""

def check_google_drive_folder_status(gdrive_link):
    value = str(gdrive_link or "").strip()
    if value.lower() in {"", "none", "null", "n/a", "na", "-"}:
        return "missing"

    folder_id = extract_drive_folder_id(value)
    if not folder_id:
        return "missing"

    params = {
        "q": f"'{folder_id}' in parents and trashed = false",
        "pageSize": 1,
        "fields": "files(id,name,mimeType)",
        "includeItemsFromAllDrives": "true",
        "supportsAllDrives": "true",
    }

    try:
        access_token = get_google_drive_access_token()
        headers = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        elif GOOGLE_DRIVE_API_KEY:
            params["key"] = GOOGLE_DRIVE_API_KEY
        else:
            return "unknown"

        response = requests.get(
            GOOGLE_DRIVE_FILES_URL,
            params=params,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        files = response.json().get("files", [])
        return "nonempty" if files else "empty"
    except Exception as e:
        log(f"Could not check Google Drive folder {folder_id}: {e}", "warning")
        return "unknown"

def ensure_drive_status_label(card_id, gdrive_link):
    labels = trello_get(f"/cards/{card_id}/labels")
    existing = {lbl.get("name"): lbl for lbl in labels}
    no_link_label = existing.get(DRIVE_NO_LINK_LABEL)
    empty_label = existing.get(DRIVE_FOLDER_EMPTY_LABEL)

    status = check_google_drive_folder_status(gdrive_link)
    log(f"Drive status for card {card_id}: {status}")

    if status == "missing":
        if empty_label:
            trello_delete(f"/cards/{card_id}/idLabels/{empty_label['id']}")
        if not no_link_label:
            trello_post(f"/cards/{card_id}/labels", {"name": DRIVE_NO_LINK_LABEL, "color": "red"})
        return

    if status == "empty":
        if no_link_label:
            trello_delete(f"/cards/{card_id}/idLabels/{no_link_label['id']}")
        if not empty_label:
            trello_post(f"/cards/{card_id}/labels", {"name": DRIVE_FOLDER_EMPTY_LABEL, "color": "yellow"})
        return

    if status == "nonempty":
        if no_link_label:
            trello_delete(f"/cards/{card_id}/idLabels/{no_link_label['id']}")
        if empty_label:
            trello_delete(f"/cards/{card_id}/idLabels/{empty_label['id']}")
        return

def ensure_referral_label(card_id, referral_requested_date):
    labels = trello_get(f"/cards/{card_id}/labels")
    referral_label = next((lbl for lbl in labels if lbl.get("name") == "No Referral made to SCC"), None)

    value = str(referral_requested_date).strip().lower()
    has_referral = value not in {"", "none", "null", "[]"}

    if not has_referral:
        if not referral_label:
            trello_post(
                f"/cards/{card_id}/labels",
                {"name": "No Referral made to SCC", "color": "orange"}
            )
    else:
        if referral_label:
            trello_delete(f"/cards/{card_id}/idLabels/{referral_label['id']}")

# =========================================================
# TRELLO API
# =========================================================

def trello_get(path, extra_params=None):
    params = dict(AUTH)
    if extra_params:
        params.update(extra_params)
    r = requests.get(f"{BASE_URL}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def trello_post(path, data):
    r = requests.post(f"{BASE_URL}{path}", params=AUTH, data=data, timeout=30)
    r.raise_for_status()
    return r.json()

def trello_put(path, data):
    r = requests.put(f"{BASE_URL}{path}", params=AUTH, data=data, timeout=30)
    r.raise_for_status()
    return r.json()

def trello_delete(path):
    r = requests.delete(f"{BASE_URL}{path}", params=AUTH, timeout=30)
    r.raise_for_status()

# =========================================================
# COMMENTS
# =========================================================

def get_airtable_comment_actions(card_id):
    actions = trello_get(f"/cards/{card_id}/actions", {"filter": "commentCard", "limit": 1000})
    return [a for a in actions if AIRTABLE_COMMENT_MARKER in a.get("data", {}).get("text", "")]

def get_card_comment_actions(card_id):
    return trello_get(f"/cards/{card_id}/actions", {"filter": "commentCard", "limit": 1000})

def get_latest_non_airtable_comment(card_id):
    actions = get_card_comment_actions(card_id)
    for action in actions:
        text = action.get("data", {}).get("text", "") or ""
        if AIRTABLE_COMMENT_MARKER not in text:
            return action
    return None

def delete_airtable_comments(card_id, keep_action_id=None):
    actions = get_airtable_comment_actions(card_id)
    for action in actions:
        action_id = action.get("id")
        if keep_action_id and action_id == keep_action_id:
            continue
        try:
            trello_delete(f"/actions/{action_id}")
        except Exception:
            pass

def delete_airtable_comment(card_id, action_id):
    if not action_id:
        return
    trello_delete(f"/actions/{action_id}")

def update_airtable_comment(action_id, comment_text):
    return trello_put(f"/actions/{action_id}/comments", {"text": comment_text})

def create_airtable_comment(card_id, comment_text):
    return trello_post(f"/cards/{card_id}/actions/comments", {"text": comment_text})

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
    existing_airtable_comments = get_airtable_comment_actions(card_id)
    keep_action_id = None
    if existing_airtable_comments:
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
        drive_links.append(f"📁 Client Documents:\n{gdrive}")

    if drive_links:
        lines.append("")
        lines.extend(drive_links)

    lines.extend([
        "",
        "📝 CoSS Support Form:",
        form_link
    ])

    comment_text = "\n".join(lines)

    promote_to_top = False
    if keep_action_id:
        try:
            latest_non_airtable = get_latest_non_airtable_comment(card_id)
            airtable_date = existing_airtable_comments[0].get("date", "")
            latest_other_date = (latest_non_airtable or {}).get("date", "")

            if latest_other_date and (not airtable_date or latest_other_date > airtable_date):
                promote_to_top = True
        except Exception as e:
            log(f"Could not determine comment promotion order on card {card_id}: {e}", "warning")

    if promote_to_top:
        delete_airtable_comment(card_id, keep_action_id)
        create_airtable_comment(card_id, comment_text)
    elif keep_action_id:
        update_airtable_comment(keep_action_id, comment_text)
    else:
        create_airtable_comment(card_id, comment_text)

def clean_card(card_id):
    for cl in trello_get(f"/cards/{card_id}/checklists"):
        trello_delete(f"/checklists/{cl['id']}")
    trello_put(f"/cards/{card_id}", {"desc": ""})

def mark_and_move_to_cleanup(card):
    card_id = card["id"]
    labels = trello_get(f"/cards/{card_id}/labels")
    if not any(lbl.get("name") == "Duplicate,Delete Me!" for lbl in labels):
        trello_post(
            f"/cards/{card_id}/labels",
            {"name": "Duplicate,Delete Me!", "color": "black"}
        )
    if CLEANUP_LIST_ID and card.get("idList") != CLEANUP_LIST_ID:
        trello_put(
            f"/cards/{card_id}",
            {"idList": CLEANUP_LIST_ID}
        )

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

def phone_matches(p1, p2):
    if not p1 or not p2:
        return False
    return p1[-9:] == p2[-9:]

def extract_phone_from_card(card):
    text = card.get("name", "") + " " + card.get("desc", "")
    matches = re.findall(r"(?:\+44|44|0)?[\d\s()\-]{9,}", text)
    phones = []
    for match in matches:
        cleaned = clean_phone(match)
        if len(cleaned) >= 11:
            phones.append(cleaned)
    return max(phones, key=len) if phones else ""

def get_drive_link_from_card(card_id):
    try:
        card = trello_get(f"/cards/{card_id}", {"fields": "name,desc"})
        desc = card.get("desc", "")
        matches = re.findall(r'https?://[^\s\]]+', desc)
        for link in matches:
            link = link.strip().lstrip("[").rstrip("]>.,)")
            if "drive.google.com" in link or "docs.google.com" in link:
                return link
    except Exception as e:
        log(f"Failed to extract Drive link: {e}", "error")
    return ""

def load_existing_cards():
    cards = {}
    phone_index = {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_ARCHIVE_DAYS)

    for lid in EXISTING_LIST_IDS:
        fetched_cards = trello_get(f"/lists/{lid}/cards", {
            "cards": "all",
            "fields": "id,name,desc,closed,dateLastActivity,shortUrl,idList"
        })

        for card in fetched_cards:
            is_archived = card.get("closed", False)
            if is_archived:
                try:
                    last_activity = parse_iso(card["dateLastActivity"])
                    if last_activity < cutoff:
                        continue
                except:
                    continue

            phone = extract_phone_from_card(card)
            if phone:
                if phone not in phone_index:
                    phone_index[phone] = []
                phone_index[phone].append(card)

            airtable_id = extract_airtable_id_from_card(card)
            if not airtable_id:
                continue

            if airtable_id in cards:
                existing_card = cards[airtable_id]["card"]
                existing_last_activity = parse_iso(existing_card["dateLastActivity"])
                current_last_activity = parse_iso(card["dateLastActivity"])

                if current_last_activity > existing_last_activity:
                    trello_put(f"/cards/{existing_card['id']}", {"closed": "true"})
                    cards[airtable_id] = {"card": card, "closed": is_archived}
                else:
                    trello_put(f"/cards/{card['id']}", {"closed": "true"})
                continue

            cards[airtable_id] = {"card": card, "closed": is_archived}

    return cards, phone_index

# =========================================================
# MAIN EXECUTION VIA INTERFACE PAGE 2
# =========================================================

async def execute_sync_pipeline(session):
    log("🚀 Starting Optimized Airtable (Page 2 MCP) → Trello Sync")
    existing_cards, phone_index = load_existing_cards()

    records = await airtable_mcp_get_all_records(session, limit=2000)

    created = updated = 0

    for record in records:
        try:
            airtable_id = record.get("id")
            fields = record.get("fields", {})

            created_at = parse_iso(record["createdTime"])
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            first = get_field(fields, "Forename/First name(s)")
            last = get_field(fields, "Surname/Last Name(s)")
            fullname = f"{first} {last}".strip()
            
            if not fullname and "fld7l03Yg7qOpECmp" in fields:
                fullname = str(fields["fld7l03Yg7qOpECmp"]).strip()

            if not fullname:
                log(f"Skipping {airtable_id}: no full name")
                continue

            contact_phone = get_field(fields, "Contact number")
            whatsapp_phone = get_field(fields, "WhatsApp number") or contact_phone
            
            # Smart fallback for bulk sync script
            if not contact_phone and not whatsapp_phone:
                for k, val in fields.items():
                    if val:
                        val_str = str(val)
                        matches = re.findall(r"(?:07|\+447|447)\d{9}", val_str)
                        if matches:
                            contact_phone = matches[0]
                            break

            if not contact_phone and not whatsapp_phone:
                log(f"Skipping {airtable_id}: no phone number", "warning")
                continue

            # Verify staff
            if ALLOWED_STAFF and not verify_staff_allowed(fields, ALLOWED_STAFF):
                log(f"⏭ Skipped record {airtable_id}: Staff not allowed", "warning")
                continue

            eviction_raw = get_field(fields, "MEARS Eviction Date")
            eviction_date = parse_date(eviction_raw)
            gdrive = get_field(fields, "Link")

            whatsapp_number = clean_phone(whatsapp_phone or contact_phone)
            card_name = f"{fullname} – Phone: {contact_phone} – Eviction date: {eviction_raw}"

            spring_issue = get_field(fields, "SPRING - Issue^")
            coss_support = get_field(fields, "CoSS Support Provided")
            supporting_docs = get_field(fields, "Supporting Documents attached")
            coss_notes = get_field(fields, "CoSS Notes")
            scc_notes = get_field(fields, "SCC Notes")
            cas_notes = get_field(fields, "CAS Notes")
            last_modified = get_field(fields, "Last modified")
            referral_drive_link = ""
            referral_requested_date = get_field(fields, "SCC - Referral requested (date).")

            retained_referral_link = ""

            if airtable_id in existing_cards:
                existing = existing_cards[airtable_id]
                card = existing["card"]
                card_id = card["id"]

                log(f"🔄 Updating existing card: {card_name}")
                if existing["closed"]:
                    trello_put(f"/cards/{card_id}", {"closed": "false"})

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
                existing_cards[airtable_id] = {"card": card, "closed": False}
                created += 1

                ensure_drive_status_label(card_id, gdrive)

                phone = clean_phone(whatsapp_phone or contact_phone)
                if phone:
                    for stored_phone, cards_list in phone_index.items():
                        if not phone_matches(phone, stored_phone):
                            continue
                        for other_card in cards_list:
                            other_id = other_card["id"]
                            if other_id == card_id:
                                continue
                            card_details = trello_get(f"/cards/{other_id}", {"fields": "name,desc"})
                            other_airtable_id = extract_airtable_id_from_card(card_details.get("desc", ""))
                            if other_airtable_id:
                                continue

                            existing_drive = get_drive_link_from_card(other_id)
                            if existing_drive:
                                referral_drive_link = existing_drive
                            mark_and_move_to_cleanup(other_card)
                            break

            if not referral_drive_link:
                referral_drive_link = retained_referral_link

            desc = (
                f"WhatsApp Web:\nhttps://web.whatsapp.com/send?phone={whatsapp_number}\n\n"
                f"WhatsApp Phone:\nhttps://wa.me/{whatsapp_number}\n\n"
                f"SPRING - Issue:\n{spring_issue}\n\n"
                f"CoSS Support Provided:\n{coss_support}\n\n"
                f"Supporting Documents attached:\n{supporting_docs}\n\n"
                f"AIRTABLE_RECORD_ID:\n{airtable_id}\n"
            )

            if referral_drive_link:
                desc += f"\n\nREFERRAL_DOCUMENTS:\n{referral_drive_link}\n"

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

            attachments = trello_get(f"/cards/{card_id}/attachments")
            for att in attachments:
                trello_delete(f"/cards/{card_id}/attachments/{att['id']}")

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

            ensure_referral_label(card_id, referral_requested_date)

        except Exception as e:
            log(f"❌ Error processing {airtable_id}: {e}", "error")

    log(f"\n✅ Complete. Cards created: {created}, updated: {updated}")


async def main():
    await run_oauth_mcp_session(execute_sync_pipeline)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except Exception as exc:
        import traceback
        print("\n❌ DETAILED EXCEPTION TRACEBACK:")
        traceback.print_exc()
        log(f"Pipeline failed: {exc}", "error")
        sys.exit(1)