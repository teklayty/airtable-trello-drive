#!/usr/init/env python3
"""
Airtable MCP to Trello Individual Card Creator (Smart Phone Fallback)
-------------------------------------------------------------------
Uses the Interface MCP session and automatically extracts phone numbers from 
any visible field payload if the dedicated column is hidden.
"""

import asyncio
import json
import logging
import os
import re
import sys
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
EXISTING_LIST_IDS = os.getenv("TRELLO_EXISTING_LIST_IDS", "")

AIRTABLE_MCP_URL = os.getenv("AIRTABLE_MCP_URL", "https://mcp.airtable.com/mcp")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "appt4PI9krGalheLk")
AIRTABLE_TABLE_ID = os.getenv("AIRTABLE_TABLE_ID", "tblPBsPUOoPDzknkj")
AIRTABLE_INTERFACE_ID = os.getenv("AIRTABLE_INTERFACE_ID", "pbdDOjqDonDnlt0nx")
AIRTABLE_PAGE_ID = os.getenv("AIRTABLE_PAGE_ID", "pagLsUMtGqYgvu2a5")

BASE_URL = "https://api.trello.com/1"
AUTH = {"key": TRELLO_API_KEY, "token": TRELLO_API_TOKEN}

EXISTING_LIST_IDS = [lid.strip() for lid in EXISTING_LIST_IDS.split(",") if lid.strip()]
TODAY = datetime.now()

AIRTABLE_NOTES_PAGE_ID = os.getenv("AIRTABLE_NOTES_PAGE_ID", "pagLsUMtGqYgvu2a5")
AIRTABLE_NOTES_PARAM = os.getenv("AIRTABLE_NOTES_PARAM", "Pwx3z")
AIRTABLE_COMMENT_MARKER = "📌**SU AIRTABLE Record and Drive Docs**"

READ_ONLY_SCOPES = (
    "data.records:read "
    "schema.bases:read "
    "data.recordComments:read "
    "workspacesAndBases:read"
)

TOKEN_FILE = Path(__file__).parent / ".airtable_tokens.json"

logging.basicConfig(
    filename="../Airtable2Trello/sync.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def log(msg):
    print(msg)
    logging.info(msg)

# =========================================================
# MCP AUTH & SESSION SETUP
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
        except Exception:
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
        except Exception:
            pass

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
            client_name="Airtable Trello Interface Individual Sync",
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


async def airtable_mcp_get_single_record(session, target_record_id):
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
        log("Failed to fetch structured records from MCP response.")
        return None

    by_table = payload.get("recordsByTableId", {})
    raw_records = by_table.get(AIRTABLE_TABLE_ID, [])
    
    if not isinstance(raw_records, list):
        if isinstance(by_table, dict) and len(by_table) > 0:
            raw_records = next(iter(by_table.values()))
        else:
            raw_records = []

    for rec in raw_records:
        if rec.get("id") == target_record_id:
            cell_map = rec.get("cellValuesByFieldId", {})
            fields = {}
            if isinstance(cell_map, dict):
                for fid, val in cell_map.items():
                    fields[fid] = val
            if "fields" in rec and isinstance(rec["fields"], dict):
                fields.update(rec["fields"])

            return {
                "id": rec.get("id"),
                "createdTime": rec.get("createdTime", datetime.now(timezone.utc).isoformat()),
                "fields": fields,
            }
            
    return None

# =========================================================
# HELPERS & TRELLO
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

def get_field(fields, *keys):
    for key in keys:
        if key in fields:
            value = fields[key]
            if isinstance(value, list):
                return ", ".join(map(str, value))
            val_str = str(value).strip()
            if val_str:
                return val_str
    return ""

def clean_phone(phone):
    digits = "".join(filter(str.isdigit, str(phone)))
    if digits.startswith("44"):
        return digits
    if digits.startswith("0"):
        return f"44{digits[1:]}"
    return f"44{digits}"

def build_notes_link(record_id):
    return (
        f"https://airtable.com/"
        f"{AIRTABLE_BASE_ID}/"
        f"{AIRTABLE_NOTES_PAGE_ID}"
        f"?{AIRTABLE_NOTES_PARAM}={record_id}"
    )

def build_prefill_form_link(fullname, airtable_id):
    base_url = "https://airtable.com/appt4PI9krGalheLk/pag30p3pX8fc0lFkn/form"
    value = f"{fullname} ({airtable_id})"
    encoded_value = urllib.parse.quote(value)
    return f"{base_url}?prefill_Client%20ID%20and%20Full%20Name={encoded_value}"

def calculate_card_position(eviction_date):
    if not eviction_date:
        return 999999999
    days_until = (eviction_date - TODAY).days
    return max(days_until, 0)

def trelox(method, path, data=None, extra_params=None):
    params = dict(AUTH)
    if extra_params:
        params.update(extra_params)
    url = f"{BASE_URL}{path}"
    if method == "GET":
        r = requests.get(url, params=params)
    elif method == "POST":
        r = requests.post(url, params=params, data=data)
    elif method == "PUT":
        r = requests.put(url, params=params, data=data)
    elif method == "DELETE":
        r = requests.delete(url, params=params)
    r.raise_for_status()
    return r.json() if method != "DELETE" else None

def delete_all_comments(card_id):
    actions = trelox("GET", f"/cards/{card_id}/actions", extra_params={"filter": "commentCard"})
    for action in actions:
        try:
            trelox("DELETE", f"/actions/{action.get('id')}")
        except Exception as e:
            log(f"Could not delete comment: {e}")

def find_duplicate_card(phone):
    phone = clean_phone(phone)
    for list_id in EXISTING_LIST_IDS:
        cards = trelox("GET", f"/lists/{list_id}/cards", extra_params={"fields": "id,name,desc,idList"})
        for card in cards:
            text = f"{card.get('name', '')} {card.get('desc', '')}"
            matches = re.findall(r"(?:\+44|44|0)?[\d\s()\-]{9,}", text)
            for match in matches:
                existing_phone = clean_phone(match)
                if existing_phone and existing_phone[-9:] == phone[-9:]:
                    return card
    return None

def get_drive_links_from_card(card_id):
    links = []
    try:
        card = trelox("GET", f"/cards/{card_id}", extra_params={"fields": "desc"})
        desc = card.get("desc", "")
        matches = re.findall(r'https://drive\.google\.com[^\s\])"]+', desc)
        for link in matches:
            cleaned = link.strip().lstrip("[").rstrip("]>.,)")
            if cleaned not in links:
                links.append(cleaned)
    except Exception as e:
        log(f"Drive extraction failed: {e}")
    return list(set(links))

def move_duplicate_to_cleanup(card):
    cleanup_list_id = os.getenv("TRELLO_CLEANUP_LIST_ID")
    try:
        trelox("PUT", f"/cards/{card['id']}", data={"idList": cleanup_list_id})
        trelox("POST", f"/cards/{card['id']}/labels", data={"name": "Duplicate,Delete Me!", "color": "black"})
        log(f"Moved duplicate card: {card['name']}")
    except Exception as e:
        log(f"Failed to move duplicate: {e}")

def add_comment(card_id, record_id, last_modified, coss_notes, scc_notes, cas_notes, gdrive, referral_drive_links, fullname):
    delete_all_comments(card_id)
    notes_link = build_notes_link(record_id)
    form_link = build_prefill_form_link(fullname, record_id)

    lines = [f"📌**SU AIRTABLE Record and Drive Docs**"]
    lines.append(f"🟦 CoSS Notes:\n{notes_link}\n")
    if scc_notes and str(scc_notes).strip():
        lines.append(f"🟨 SCC Notes:\n{notes_link}\n")
    if cas_notes and str(cas_notes).strip():
        lines.append(f"🟩 CAS Notes:\n{notes_link}\n")

    gdrive_clean = str(gdrive).strip() if gdrive else ""
    if gdrive_clean:
        lines.extend(["", "📁 Client Documents:", gdrive_clean])

    if referral_drive_links:
        valid_links = []
        seen = set()
        for link in referral_drive_links:
            cleaned = link.strip().rstrip("]>.,)").lstrip("[")
            if not cleaned or cleaned == gdrive_clean or cleaned in seen:
                continue
            seen.add(cleaned)
            valid_links.append(cleaned)

        if valid_links:
            lines.append("")
            lines.append("📁 Referral Documents:")
            for link in valid_links:
                lines.append(link)

    lines.extend(["", "📝 CoSS Support Form:", form_link])
    comment_text = "\n".join(lines)

    trelox("POST", f"/cards/{card_id}/actions/comments", data={"text": comment_text})
    log(f"✅ Comment added to card {card_id}")

def ensure_referral_label(card_id, referral_requested_date):
    labels = trelox("GET", f"/cards/{card_id}/labels")
    referral_label = next((lbl for lbl in labels if lbl.get("name") == "No Referral made to SCC"), None)

    value = str(referral_requested_date).strip().lower()
    has_referral = value not in {"", "none", "null", "[]"}

    if not has_referral:
        if not referral_label:
            trelox("POST", f"/cards/{card_id}/labels", data={"name": "No Referral made to SCC", "color": "orange"})
    else:
        if referral_label:
            trelox("DELETE", f"/cards/{card_id}/idLabels/{referral_label['id']}")

# =========================================================
# MAIN CARD CREATION LOGIC
# =========================================================

def process_record_creation(record, target_list_id):
    record_id = record["id"]
    fields = record.get("fields", {})

    first = get_field(fields, "Forename/First name(s)", "Forename", "First name")
    last = get_field(fields, "Surname/Last Name(s)", "Surname", "Last name")
    fullname = f"{first} {last}".strip()

    if not fullname and "fld7l03Yg7qOpECmp" in fields:
        fullname = str(fields["fld7l03Yg7qOpECmp"]).strip()

    if not fullname:
        log("❌ Missing full name")
        return

    contact_phone = get_field(fields, "Contact number")
    whatsapp_phone = get_field(fields, "WhatsApp number") or contact_phone

    # Fallback: scan all available payload fields if phone wasn't found in dedicated keys
    if not contact_phone and not whatsapp_phone:
        for k, val in fields.items():
            if val:
                val_str = str(val)
                matches = re.findall(r"(?:07|\+447|447)\d{9}", val_str)
                if matches:
                    contact_phone = matches[0]
                    break

    if not contact_phone and not whatsapp_phone:
        log("❌ Missing phone number")
        return

    whatsapp_number = clean_phone(whatsapp_phone or contact_phone)
    duplicate_card = find_duplicate_card(contact_phone)
    referral_drive_links = []

    if duplicate_card:
        referral_drive_links = get_drive_links_from_card(duplicate_card["id"])
        log(f"Duplicate card ID: {duplicate_card['id']} Referral drive links found: {referral_drive_links}")

    eviction_raw = get_field(fields, "MEARS Eviction Date")
    eviction_date = parse_date(eviction_raw)
    gdrive = get_field(fields, "Link")

    spring_issue = get_field(fields, "SPRING - Issue^")
    coss_support = get_field(fields, "CoSS Support Provided")
    supporting_docs = get_field(fields, "Supporting Documents attached")
    coss_notes = get_field(fields, "CoSS Notes")
    scc_notes = get_field(fields, "SCC Notes")
    cas_notes = get_field(fields, "CAS Notes")
    last_modified = get_field(fields, "Last modified")
    referral_requested_date = get_field(fields, "SCC - Referral requested (date).")

    card_name = f"{fullname} – Phone: {contact_phone}"

    desc = (
        f"WhatsApp Web:\nhttps://web.whatsapp.com/send?phone={whatsapp_number}\n\n"
        f"WhatsApp Phone:\nhttps://wa.me/{whatsapp_number}\n\n"
        f"SPRING - Issue:\n{spring_issue}\n\n"
        f"CoSS Support Provided:\n{coss_support}\n\n"
        f"Supporting Documents attached:\n{supporting_docs}\n\n"
        f"AIRTABLE_RECORD_ID:\n{record_id}\n"
    )

    card = trelox(
        "POST",
        "/cards",
        data={
            "idList": target_list_id,
            "name": card_name,
            "desc": desc,
            "due": eviction_date.isoformat() if eviction_date else None,
            "pos": calculate_card_position(eviction_date)
        }
    )

    card_id = card["id"]

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

    ensure_referral_label(card_id, referral_requested_date)
    log(f"✅ Successfully created card for {fullname}!")

# =========================================================
# ENTRY POINT VIA MCP SESSION
# =========================================================

async def main():
    target_record_id = input("Enter Airtable Record ID: ").strip()
    if not target_record_id:
        print("No record ID provided.")
        return

    print("\nAvailable lists:")
    for i, lid in enumerate(EXISTING_LIST_IDS, start=1):
        try:
            list_info = trelox("GET", f"/lists/{lid}")
            print(f"{i}. {list_info['name']}")
        except Exception as e:
            print(f"{i}. {lid} (Error loading name: {e})")

    choice = int(input(f"\nSelect target list (1-{len(EXISTING_LIST_IDS)}): ").strip())
    target_list_id = EXISTING_LIST_IDS[choice - 1]

    async def run_work(session):
        log(f"Searching for record {target_record_id} via Interface MCP...")
        record = await airtable_mcp_get_single_record(session, target_record_id)
        if not record:
            log(f"❌ Record {target_record_id} could not be found via the Interface page payload.")
            return
        
        process_record_creation(record, target_list_id)

    await run_oauth_mcp_session(run_work)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        sys.exit(1)