#!/usr/bin/env python3
"""Airtable Interface MCP → Trello sync pipeline.

Preserves the uploaded working version's Airtable MCP/OAuth, staff allowlist,
flexible field parsing, phone-number fallback, card description/comment sync,
Google Drive status labels, referral labels, and legacy phone-based cleanup.

Adds board-wide Airtable-ID duplicate detection and sanitised Trello errors.
Fetch-only is the default. Set AIRTABLE_MCP_FETCH_ONLY=false explicitly to
allow Trello writes. Destructive checklist/attachment cleanup is opt-in.
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
RAW_ALLOWED_STAFF_IDS = os.getenv("TRELLO_ALLOWED_STAFF_IDS", "")

AIRTABLE_MCP_URL = os.getenv("AIRTABLE_MCP_URL", "https://mcp.airtable.com/mcp")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "appt4PI9krGalheLk")
AIRTABLE_TABLE_ID = os.getenv("AIRTABLE_TABLE_ID", "tblPBsPUOoPDzknkj")
AIRTABLE_INTERFACE_ID = os.getenv("AIRTABLE_INTERFACE_ID", "pbdDOjqDonDnlt0nx")
AIRTABLE_PAGE_ID = os.getenv("AIRTABLE_PAGE_ID", "pagLsUMtGqYgvu2a5")

URGENT_LIST_NAME = "URGENT – Eviction within 14 days"
BASE_URL = "https://api.trello.com/1"
AUTH = {"key": TRELLO_API_KEY, "token": TRELLO_API_TOKEN}

# =========================================================
# NOTES & DRIVE CONFIG
# =========================================================
AIRTABLE_NOTES_PAGE_ID = "pagLsUMtGqYgvu2a5"  # Explicitly requested client record view

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

AIRTABLE_RECORD_ID_RE = re.compile(r"rec[a-zA-Z0-9]{14}", re.IGNORECASE)
# Staff filtering is record-ID-only; names never permit a client through.
ALLOWED_STAFF = set()  # retained only for compatibility with unused diagnostics
# Accept Airtable record IDs regardless of whether the environment variable
# uses commas, spaces, newlines, or quotes as separators. Names are never
# accepted because only the exact Airtable `rec...` ID pattern is extracted.
ALLOWED_STAFF_IDS = {
    match.casefold()
    for match in AIRTABLE_RECORD_ID_RE.findall(RAW_ALLOWED_STAFF_IDS)
}
COSS_SUPPORT_PROVIDED_FIELD_ID = "fldIae0zVo5CMLfOj"
COSS_NOTES_FIELD_ID = "fld9T6W0FyxyMw4E8"
STAFF_ID_FILTER_FIELD_ID = "fldIOi1f33xWKzIJC"
EXISTING_LIST_IDS = [lid.strip() for lid in EXISTING_LIST_IDS.split(",") if lid.strip()]
CLEANUP_LIST_ID = os.getenv("TRELLO_CLEANUP_LIST_ID")
TRELLO_SCAN_ALL_BOARD_LISTS = os.getenv("TRELLO_SCAN_ALL_BOARD_LISTS", "true").strip().lower() in {"1", "true", "yes", "on"}
# Existing duplicate cards are reported and one canonical card is selected by
# default. Set true only if you explicitly want the sync to close older duplicates.
TRELLO_AUTO_CLOSE_DUPLICATE_CARDS = os.getenv("TRELLO_AUTO_CLOSE_DUPLICATE_CARDS", "false").strip().lower() in {"1", "true", "yes", "on"}
# The source version cleared checklists/descriptions and deleted all card
# attachments on each sync. Preserve these capabilities, but make them explicit
# opt-ins because attachments/checklists may contain user-managed information.
TRELLO_CLEAR_CHECKLISTS_ON_SYNC = os.getenv("TRELLO_CLEAR_CHECKLISTS_ON_SYNC", "false").strip().lower() in {"1", "true", "yes", "on"}
TRELLO_DELETE_ATTACHMENTS_ON_SYNC = os.getenv("TRELLO_DELETE_ATTACHMENTS_ON_SYNC", "false").strip().lower() in {"1", "true", "yes", "on"}

# Set to true for a safe MCP connectivity/data-shape test. It fetches records
# but does not create/update/delete any Trello cards.
MCP_FETCH_ONLY = os.getenv("AIRTABLE_MCP_FETCH_ONLY", "true").strip().lower() in {"1", "true", "yes", "on"}

# Hard safety lock: creator identity has not yet been verified.
# User-confirmed eligibility rule: a valid SCC referral date is present.
ELIGIBILITY_RULE_CONFIRMED = True

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


def _to_plain_json(value):
    """Convert MCP/Pydantic SDK objects to ordinary JSON-compatible values."""
    if value is None or isinstance(value, (dict, list, str, int, float, bool)):
        return value

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        for kwargs in ({"mode": "json"}, {}):
            try:
                return model_dump(**kwargs)
            except TypeError:
                continue
            except Exception:
                break

    dict_method = getattr(value, "dict", None)
    if callable(dict_method):
        try:
            return dict_method()
        except Exception:
            pass

    try:
        return json.loads(
            json.dumps(
                value,
                default=lambda obj: getattr(obj, "__dict__", str(obj)),
            )
        )
    except Exception:
        return value


def _extract_json_from_text(text):
    """Parse JSON returned as MCP text content, including fenced/embedded JSON."""
    if not text:
        return None

    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()
            try:
                return json.loads(candidate)
            except Exception:
                pass

    decoder = json.JSONDecoder()
    for marker in ("{", "["):
        pos = text.find(marker)
        if pos >= 0:
            try:
                value, _ = decoder.raw_decode(text[pos:])
                return value
            except Exception:
                pass

    return None


async def call_mcp_structured(session, tool_name, arguments):
    """
    Read an MCP tool result safely across SDK response shapes.

    MCP's current Python SDK exposes structured results as
    `result.structured_content`; older/alternate spellings and text content
    are handled as fallbacks. Tool errors are explicitly checked because
    call_tool() returns an error result rather than necessarily raising.
    """
    result = await session.call_tool(tool_name, arguments)

    is_error = getattr(result, "is_error", None)
    if is_error:
        error_messages = []

        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if text:
                error_messages.append(str(text))

        if error_messages:
            log(
                f"MCP tool {tool_name} returned an error: "
                + " | ".join(error_messages),
                "error",
            )
        else:
            log(
                f"MCP tool {tool_name} returned is_error=True "
                f"but no error text.",
                "error",
            )

        return None
    if is_error is None:
        is_error = getattr(result, "isError", False)

    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)

    structured = _to_plain_json(structured)

    if structured is not None:
        return structured

    for item in getattr(result, "content", []) or []:
        # Content is a union; only text blocks have .text.
        block_type = getattr(item, "type", None)
        if block_type not in (None, "text"):
            continue

        text = getattr(item, "text", None)
        parsed = _extract_json_from_text(text)
        if parsed is not None:
            return _to_plain_json(parsed)

    # Safe diagnostics only: do not echo returned text because it could
    # contain client data.
    result_keys = []
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(by_alias=True)
            if isinstance(dumped, dict):
                result_keys = list(dumped.keys())
        except Exception:
            pass

    if not result_keys:
        result_keys = [
            name
            for name in (
                "content",
                "structured_content",
                "structuredContent",
                "is_error",
                "isError",
            )
            if hasattr(result, name)
        ]

    log(
        f"MCP tool {tool_name} returned no usable structured content. "
        f"is_error={bool(is_error)}; result_fields={result_keys}",
        "error",
    )
    return None

def find_largest_cell_map(data):
    """Find the largest field-ID/value map in an MCP response."""
    best = {}

    def walk(obj):
        nonlocal best

        if isinstance(obj, dict):
            cells = obj.get("cellValuesByFieldId")

            if isinstance(cells, dict) and len(cells) > len(best):
                best = cells

            for value in obj.values():
                walk(value)

        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(data)
    return best


def find_cell_map_for_field(data, field_id, expected_record_id=None):
    """Find a nested cell map containing one exact Airtable field ID.

    Interface MCP detail responses can contain more than one
    ``cellValuesByFieldId`` map. The largest map is not guaranteed to be the
    client record's complete map. For staff filtering we must locate the map
    that actually contains the confirmed CoSS Support Provided field.

    Selection order:
      1. map containing ``field_id`` and explicitly owned by expected_record_id
      2. map containing ``field_id`` when it is the only such map

    Otherwise return an empty map rather than guessing.
    """
    data = _to_plain_json(data)
    if not isinstance(data, (dict, list)) or not isinstance(field_id, str):
        return {}

    exact_record_maps = []
    field_maps = []

    def walk(obj):
        if isinstance(obj, dict):
            cells = obj.get("cellValuesByFieldId")
            if isinstance(cells, dict) and field_id in cells:
                obj_id = obj.get("id") or obj.get("recordId")
                field_maps.append((cells, obj_id))
                if expected_record_id and obj_id == expected_record_id:
                    exact_record_maps.append(cells)

            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(data)

    if exact_record_maps:
        return exact_record_maps[0]
    if len(field_maps) == 1:
        cells, obj_id = field_maps[0]
        if expected_record_id and obj_id and obj_id != expected_record_id:
            return {}
        return cells

    return {}


def extract_record_cell_map(data, expected_record_id=None, required_field_ids=None):
    """Return the requested record's own cellValuesByFieldId map.

    Prefer the MCP `record` root. If the SDK wraps it, follow known wrapper
    paths. If multiple unrelated record maps exist and no matching root can be
    identified, return an empty map rather than guessing from a nested record.
    """
    data = _to_plain_json(data)
    if not isinstance(data, dict):
        return {}

    if required_field_ids:
        for field_id in required_field_ids:
            matched = find_cell_map_for_field(
                data,
                field_id,
                expected_record_id=expected_record_id,
            )
            if matched:
                return matched

    def direct_cells(obj):
        if isinstance(obj, dict):
            cells = obj.get("cellValuesByFieldId")
            if isinstance(cells, dict):
                return cells
        return None

    # Common shape: {"record": {"id": "rec...", "cellValuesByFieldId": {...}}}
    record = data.get("record")
    if isinstance(record, dict):
        record_id = record.get("id") or record.get("recordId")
        cells = direct_cells(record)
        if cells is not None and (not expected_record_id or not record_id or record_id == expected_record_id):
            return cells

    # Some SDK responses expose the record itself at the top level.
    cells = direct_cells(data)
    if cells is not None:
        top_id = data.get("id") or data.get("recordId")
        if not expected_record_id or not top_id or top_id == expected_record_id:
            return cells

    # Search wrapper objects for a field map belonging to the expected record.
    matches = []
    all_maps = []

    def walk(obj):
        if isinstance(obj, dict):
            cells_map = direct_cells(obj)
            if cells_map is not None:
                obj_id = obj.get("id") or obj.get("recordId")
                all_maps.append((cells_map, obj_id))
                if expected_record_id and obj_id == expected_record_id:
                    matches.append(cells_map)
            for child in obj.values():
                walk(child)
        elif isinstance(obj, list):
            for child in obj:
                walk(child)

    walk(data)
    if matches:
        return matches[0]
    if len(all_maps) == 1:
        only_map, only_id = all_maps[0]
        if expected_record_id and only_id and only_id != expected_record_id:
            return {}
        return only_map
    return {}

async def diagnose_client_field_metadata(session, field_ids):
    """Inspect metadata for fields returned by the client record.
    Never prints field values.
    """
    try:
        metadata = await call_mcp_structured(
            session,
            "list_pages_for_base",
            {"baseId": AIRTABLE_BASE_ID},
        )

        if not isinstance(metadata, dict):
            log(
                "Client field metadata response unavailable.",
                "warning",
            )
            return

        wanted = {
            field_id
            for field_id in field_ids
            if isinstance(field_id, str)
            and field_id.startswith("fld")
        }

        found = {}

        def walk(obj):
            if isinstance(obj, dict):
                fields = obj.get("fields")

                if isinstance(fields, list):
                    for field in fields:
                        if not isinstance(field, dict):
                            continue

                        field_id = (
                            field.get("id")
                            or field.get("fieldId")
                        )

                        if field_id in wanted:
                            found[field_id] = field

                for value in obj.values():
                    walk(value)

            elif isinstance(obj, list):
                for value in obj:
                    walk(value)

        walk(metadata)

        log(
            f"Client field metadata found for "
            f"{len(found)}/{len(wanted)} returned fields."
        )

        for field_id in sorted(found):
            field = found[field_id]

            name = (
                field.get("name")
                or field.get("label")
                or field.get("title")
                or ""
            )

            field_type = field.get("type", "")

            options = field.get("options")
            option_summary = ""

            if isinstance(options, dict):
                useful = {}

                for key in (
                    "linkedTableId",
                    "recordLinkTableId",
                    "targetTableId",
                    "tableId",
                    "relationshipType",
                ):
                    if key in options:
                        useful[key] = options[key]

                if useful:
                    option_summary = (
                        " | options="
                        + json.dumps(
                            useful,
                            ensure_ascii=False,
                            default=str,
                        )
                    )

            log(
                f"FIELD: {field_id} | "
                f"label={name!r} | "
                f"type={field_type!r}"
                f"{option_summary}"
            )

    except Exception as exc:
        log(
            "Client field metadata diagnostic failed: "
            f"{type(exc).__name__}",
            "warning",
        )
async def diagnose_relationship_field_definitions(session):
    """Inspect structural metadata of relationship/rollup fields only."""
    try:
        metadata = await call_mcp_structured(
            session,
            "list_pages_for_base",
            {"baseId": AIRTABLE_BASE_ID},
        )

        if not isinstance(metadata, dict):
            log("Relationship metadata response unavailable.", "warning")
            return

        interesting_types = {
            "multipleRecordLinks",
            "rollup",
            "lookup",
            "formula",
        }

        interesting_labels = {
            "Field",
            "Family in SPRING (Dependents)",
            "Details of support provided (from Interventions)",
            "Third Party - Referral number (from Interventions)",
        }

        def safe_related_metadata(field):
            result = {}

            for key in (
                "type",
                "name",
                "label",
                "id",
                "fieldId",
                "options",
                "config",
                "dependencies",
                "source",
                "relationship",
            ):
                if key in field:
                    value = field[key]

                    # Only expose structural metadata.
                    if key in {
                        "options",
                        "config",
                        "dependencies",
                        "source",
                        "relationship",
                    }:
                        if isinstance(value, dict):
                            structural = {}

                            for subkey, subvalue in value.items():
                                lower = str(subkey).lower()

                                if (
                                    "id" in lower
                                    or "table" in lower
                                    or "field" in lower
                                    or "link" in lower
                                    or "rollup" in lower
                                    or "source" in lower
                                ):
                                    structural[subkey] = subvalue

                            result[key] = structural
                        else:
                            result[key] = type(value).__name__
                    else:
                        result[key] = value

            return result

        found = []

        for interface in metadata.get("interfaces", []):
            for page in interface.get("pages", []):
                tables = page.get("tablesByTableId", {})

                if not isinstance(tables, dict):
                    continue

                table = tables.get(AIRTABLE_TABLE_ID)

                if not isinstance(table, dict):
                    continue

                for field in table.get("fields", []):
                    if not isinstance(field, dict):
                        continue

                    field_name = (
                        field.get("name")
                        or field.get("label")
                        or ""
                    )

                    field_type = field.get("type", "")

                    if (
                        field_type in interesting_types
                        or field_name in interesting_labels
                    ):
                        found.append(field)

        unique = {}

        for field in found:
            field_id = field.get("id") or field.get("fieldId")

            if field_id:
                unique[field_id] = field

        log(
            f"Relationship/derived field definitions found: "
            f"{len(unique)}"
        )

        for field_id, field in sorted(unique.items()):
            name = (
                field.get("name")
                or field.get("label")
                or ""
            )

            field_type = field.get("type", "")

            log(
                f"FIELD DEFINITION: {field_id} | "
                f"label={name!r} | type={field_type!r}"
            )

            structural = safe_related_metadata(field)

            for key, value in structural.items():
                if key not in {"id", "name", "label", "type"}:
                    log(
                        f"  {key}: "
                        + json.dumps(
                            value,
                            ensure_ascii=False,
                            default=str,
                        )
                    )

    except Exception as exc:
        log(
            "Relationship field diagnostic failed: "
            f"{type(exc).__name__}",
            "warning",
        )

def diagnostic_text(value):
    """Extract display text without exposing it in diagnostic logs."""
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, list):
        return ", ".join(
            text for item in value
            if (text := diagnostic_text(item))
        )

    if isinstance(value, dict):
        for key in ("name", "label", "text", "value"):
            text = diagnostic_text(value.get(key))
            if text:
                return text

    return ""


async def diagnose_case_study_relationship(session, client_record_id):
    """Check staff/client links using counts only; make no Trello changes."""
    interface_id = "pbdFEsqUlnGbhsBHr"
    page_id = "pagf4ZlN6hFw14w6Y"
    table_id = "tblqCmf1qgXKkBVBa"

    client_field_id = "fldEM4k91gIj1mV4u"
    staff_field_id = "fldzOoncKK5HjmBVH"

    try:
        payload = await call_mcp_structured(
            session,
            "list_records_for_page",
            {
                "baseId": AIRTABLE_BASE_ID,
                "interfaceId": interface_id,
                "pageId": page_id,
            },
        )

        if not isinstance(payload, dict):
            log("Case studies diagnostic: list response unavailable.")
            return

        by_table = payload.get("recordsByTableId", {})
        records = by_table.get(table_id, []) if isinstance(
            by_table, dict
        ) else []

        if not isinstance(records, list):
            log("Case studies diagnostic: unexpected records format.")
            return

        log(f"Case studies records returned: {len(records)}")

        allowed = {
            re.sub(r"\s+", " ", value.strip().lower())
            for value in ALLOWED_STAFF
            if value.strip()
        }

        scanned = 0
        link_field_present = 0
        staff_field_present = 0
        linked_ids_found = 0
        target_link_count = 0
        target_with_staff = 0
        target_allowed_staff = 0
        missing_details = 0
        link_shapes = {}

        for record in records[:500]:
            if not isinstance(record, dict):
                continue

            record_id = record.get("id")
            if not record_id:
                continue

            cells = record.get("cellValuesByFieldId", {})

            if not isinstance(cells, dict):
                cells = {}

            if (
                client_field_id not in cells
                or staff_field_id not in cells
            ):
                detail = await call_mcp_structured(
                    session,
                    "get_record_for_page",
                    {
                        "baseId": AIRTABLE_BASE_ID,
                        "interfaceId": interface_id,
                        "path": {
                            "root": {
                                "pageId": page_id,
                                "recordId": record_id,
                            },
                            "edges": [],
                        },
                    },
                )

                detail_cells = extract_record_cell_map(detail, expected_record_id=record_id)

                if detail_cells:
                    cells = {**cells, **detail_cells}
                else:
                    missing_details += 1
                    continue

                await asyncio.sleep(0.1)

            scanned += 1

            if client_field_id in cells:
                link_field_present += 1

                link_value = cells[client_field_id]
                shape = type(link_value).__name__
                link_shapes[shape] = link_shapes.get(shape, 0) + 1

                raw_link = json.dumps(
                    link_value, ensure_ascii=False, default=str
                )
                linked_ids = set(
                    re.findall(
                        r"rec[a-zA-Z0-9]{14}",
                        raw_link,
                    )
                )
                linked_ids_found += len(linked_ids)

                if client_record_id in linked_ids:
                    target_link_count += 1

                    if cells.get(staff_field_id) not in (None, "", []):
                        target_with_staff += 1

                    staff_value = re.sub(
                        r"\s+",
                        " ",
                        diagnostic_text(cells.get(staff_field_id)).lower(),
                    )

                    if staff_value and any(
                        name in staff_value for name in allowed
                    ):
                        target_allowed_staff += 1

            if staff_field_id in cells:
                staff_field_present += 1

        log(
            "Case studies diagnostic counts: "
            f"scanned={scanned}; "
            f"client-link field present={link_field_present}; "
            f"staff field present={staff_field_present}; "
            f"linked record IDs found={linked_ids_found}; "
            f"selected client linked={target_link_count}; "
            f"selected client links with staff={target_with_staff}; "
            f"selected client links matching allowlist={target_allowed_staff}; "
            f"detail failures={missing_details}; "
            f"allowlist entries={len(allowed)}."
        )
        log(f"Client-link value types: {link_shapes}")

        if len(records) > 500:
            log("Diagnostic limited to the first 500 Case studies records.")

    except Exception as exc:
        log(
            "Case studies diagnostic failed: "
            f"{type(exc).__name__}",
            "warning",
        )

def list_cell_maps(data):
    """List field maps and their paths without displaying field values."""
    found = []

    def walk(obj, path="$"):
        if isinstance(obj, dict):
            cells = obj.get("cellValuesByFieldId")

            if isinstance(cells, dict):
                found.append((path, cells))

            for key, value in obj.items():
                walk(value, f"{path}.{key}")

        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                walk(value, f"{path}[{index}]")

    walk(data)
    return found

async def find_interface_pages_for_field(session, field_id, table_id):
    """Find Interface pages whose metadata exposes an exact field ID.

    The current client-list page may omit a lookup field even though that
    field is available on another Interface page using the same underlying
    table. We keep the current page for record ordering, then use these pages
    only to enrich the same record IDs with the staff lookup.

    Returns a list of dictionaries containing only structural metadata.
    """
    try:
        metadata = await call_mcp_structured(
            session,
            "list_pages_for_base",
            {"baseId": AIRTABLE_BASE_ID},
        )

        if not isinstance(metadata, dict):
            log("Could not inspect Interface metadata for the staff field.", "warning")
            return []

        matches = []

        def display_name(obj):
            if not isinstance(obj, dict):
                return "(unnamed)"
            for key in ("name", "label", "title", "displayName"):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return "(unnamed)"

        for interface in metadata.get("interfaces", []):
            if not isinstance(interface, dict):
                continue

            interface_name = display_name(interface)

            for page in interface.get("pages", []):
                if not isinstance(page, dict):
                    continue

                page_id = page.get("id") or page.get("pageId")
                if not page_id:
                    continue

                page_name = display_name(page)
                tables = page.get("tablesByTableId", {})
                if not isinstance(tables, dict):
                    continue

                table = tables.get(table_id)
                if not isinstance(table, dict):
                    continue

                fields = table.get("fields", [])
                if not isinstance(fields, list):
                    continue

                found = False
                for field in fields:
                    if not isinstance(field, dict):
                        continue
                    candidate_id = field.get("id") or field.get("fieldId")
                    if candidate_id == field_id:
                        found = True
                        break

                if found:
                    matches.append({
                        "interface_id": interface.get("id") or interface.get("interfaceId"),
                        "interface_name": interface_name,
                        "page_id": page_id,
                        "page_name": page_name,
                    })

        log(
            f"Interface metadata: field {field_id} is exposed on "
            f"{len(matches)} page(s) for table {table_id}."
        )
        for item in matches:
            log(
                "STAFF FIELD PAGE: "
                f"interface={item['interface_name']} "
                f"({item.get('interface_id') or 'unknown'}) | "
                f"page={item['page_name']} ({item['page_id']})"
            )

        return matches

    except Exception as exc:
        log(
            "Could not discover Interface pages exposing the staff field: "
            f"{type(exc).__name__}",
            "warning",
        )
        return []


async def log_interface_field_labels(session):
    """Inventory Interface tables and relevant field metadata only."""
    try:
        metadata = await call_mcp_structured(
            session,
            "list_pages_for_base",
            {"baseId": AIRTABLE_BASE_ID},
        )

        if not isinstance(metadata, dict):
            log("Interface metadata response unavailable.", "warning")
            return

        def display_name(obj):
            if not isinstance(obj, dict):
                return "(unnamed)"

            for key in ("name", "label", "title", "displayName"):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

            return "(unnamed)"

        tables_by_id = {}

        for interface in metadata.get("interfaces", []):
            interface_name = display_name(interface)

            for page in interface.get("pages", []):
                page_name = display_name(page)
                page_id = page.get("id") or page.get(
                    "pageId", "(unknown)"
                )

                tables = page.get("tablesByTableId", {})
                if not isinstance(tables, dict):
                    continue

                for table_id, table in tables.items():
                    if not isinstance(table, dict):
                        continue

                    entry = tables_by_id.setdefault(
                        table_id,
                        {
                            "name": display_name(table),
                            "fields": {},
                            "pages": set(),
                        },
                    )

                    entry["pages"].add(
                        f"{interface_name} / {page_name} ({page_id})"
                    )

                    for field in table.get("fields", []):
                        if not isinstance(field, dict):
                            continue

                        field_id = (
                            field.get("id")
                            or field.get("fieldId")
                        )
                        field_name = display_name(field)

                        if isinstance(field_id, str):
                            entry["fields"][field_id] = field_name

                            if (
                                "intervention" in entry["name"].casefold()
                                or "intervention" in field_name.casefold()
                            ):
                                options = field.get("options", {})
                                structural = {}

                                if isinstance(options, dict):
                                    for key, value in options.items():
                                        if any(
                                            term in str(key).casefold()
                                            for term in (
                                                "table", "link", "record", "field"
                                            )
                                        ):
                                            structural[key] = value

                                log(
                                    "INTERVENTION FIELD DETAIL: "
                                    f"table={entry['name']} ({table_id}) | "
                                    f"field={field_name!r} ({field_id}) | "
                                    f"type={field.get('type', 'not exposed')} | "
                                    f"options={structural}"
                                )

                            if "intervention" in field_name.casefold():
                                log(
                                    f"INTERVENTIONS FIELD: {field_id} | "
                                    f"name={field_name!r} | "
                                    f"type={field.get('type', 'not exposed')!r}"
                                )

        log(f"Unique Interface tables found: {len(tables_by_id)}")

        for table_id, entry in sorted(
            tables_by_id.items(),
            key=lambda pair: pair[1]["name"].lower(),
        ):
            log(
                f"TABLE: {entry['name']} | ID: {table_id} | "
                f"Fields: {len(entry['fields'])} | "
                f"Pages: {len(entry['pages'])}"
            )

            table_name = entry["name"].lower()
            fields = entry["fields"]

            relevant = (
                "intervention" in table_name
                or "case study" in table_name
                or any(
                    any(term in name.lower() for term in (
                        "staff",
                        "volunteer",
                        "assigned",
                        "completed by",
                        "support provided",
                        "client",
                    ))
                    for name in fields.values()
                )
            )

            if relevant:
                for field_id, field_name in sorted(
                    fields.items(),
                    key=lambda pair: pair[1].lower(),
                ):
                    log(f"  {field_id} | {field_name}")

    except Exception as exc:
        log(
            "Could not inventory Interface tables: "
            f"{type(exc).__name__}",
            "warning",
        )
async def diagnose_service_activity_source(session):
    """Find the Interface metadata corresponding to the service/activity view."""
    try:
        metadata = await call_mcp_structured(
            session,
            "list_pages_for_base",
            {"baseId": AIRTABLE_BASE_ID},
        )

        if not isinstance(metadata, dict):
            log(
                "Service/activity metadata response unavailable.",
                "warning",
            )
            return

        targets = (
            "Client",
            "Contact/Appointment Date",
            "Staff/Volunteer Name",
            "Organisation",
            "Notes",
            "Support & Activity Summary",
            "Referred to",
            "Organisation(s) referred to",
            "Interventions",
        "City of Sanctuary Sheffield (COSS)",
        "City of Sanctuary Sheffield",
        )

        matches = []

        def walk(obj, path="$", context=None):
            if isinstance(obj, dict):
                local_context = context

                # Capture useful parent context.
                for key in (
                    "name",
                    "label",
                    "title",
                    "displayName",
                ):
                    value = obj.get(key)
                    if isinstance(value, str) and value.strip():
                        local_context = value.strip()
                        break

                for key, value in obj.items():
                    if isinstance(value, str):
                        text = value.strip()

                        for target in targets:
                            if text.lower() == target.lower():
                                matches.append(
                                    (
                                        target,
                                        path + "." + key,
                                        local_context,
                                    )
                                )

                    walk(
                        value,
                        f"{path}.{key}",
                        local_context,
                    )

            elif isinstance(obj, list):
                for i, value in enumerate(obj):
                    walk(
                        value,
                        f"{path}[{i}]",
                        context,
                    )

        walk(metadata)

        # Remove duplicate matches.
        unique = []
        seen = set()

        for item in matches:
            if item in seen:
                continue
            seen.add(item)
            unique.append(item)

        log(
            f"Service/activity metadata matches found: "
            f"{len(unique)}"
        )

        for target, path, context in unique:
            log(
                f"SERVICE MATCH: {target!r} | "
                f"Context: {context!r} | "
                f"Path: {path}"
            )

    except Exception as exc:
        log(
            "Service/activity metadata diagnostic failed: "
            f"{type(exc).__name__}",
            "warning",
        )

def _staff_diag_text_values(value):
    """Extract text leaves without logging any values."""
    if isinstance(value, str):
        if value.strip():
            yield value.strip()

    elif isinstance(value, dict):
        for child in value.values():
            yield from _staff_diag_text_values(child)

    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _staff_diag_text_values(child)


def _staff_diag_normalize(value):
    """Normalise whitespace and case for exact name matching."""
    return " ".join(str(value).casefold().split())

async def airtable_mcp_get_all_records(session, limit=2000):
    """Fetch the client list, then enrich each record with its Interface detail fields.

    The CoSS Support Provided lookup is the sole staff-ID filtering source. Only the last AIRTABLE_RECORD_WINDOW records in the returned Interface
    order are enriched (default 50). Requests are serialised and rate-limited.
    No Trello calls happen in this function.
    """
    log("Fetching records via Airtable Interface MCP...")

    payload = await call_mcp_structured(
        session,
        "list_records_for_page",
        {
            "baseId": AIRTABLE_BASE_ID,
            "interfaceId": AIRTABLE_INTERFACE_ID,
            "pageId": AIRTABLE_PAGE_ID,
        },
    )

    if not isinstance(payload, dict):
        log("Could not retrieve the Airtable Interface page response.", "error")
        return []

    by_table = payload.get("recordsByTableId", {})
    if not isinstance(by_table, dict):
        log("MCP response has no recordsByTableId mapping.", "error")
        return []

    raw_records = by_table.get(AIRTABLE_TABLE_ID, [])
    if not isinstance(raw_records, list):
        log(
            f"Expected table {AIRTABLE_TABLE_ID} was not returned as a record list.",
            "error",
        )
        return []

    # Only process the last N records in the Interface's returned order.
    # For the current list of 1,630 records, N=50 selects positions 1,581-1,630.
    # This is a list-position window, not a date-based sort.
    valid_records_all = [
        item for item in raw_records
        if isinstance(item, dict) and item.get("id")
    ]

    try:
        configured_window = int(os.getenv("AIRTABLE_RECORD_WINDOW", "50"))
    except (TypeError, ValueError):
        configured_window = 50
    configured_window = max(1, configured_window)
    window_size = min(limit, configured_window, len(valid_records_all))
    valid_records = valid_records_all[-window_size:] if window_size else []
    first_position = len(valid_records_all) - len(valid_records) + 1 if valid_records else 0
    last_position = len(valid_records_all) if valid_records else 0

    log(
        f"List response contains {len(raw_records)} records for table "
        f"{AIRTABLE_TABLE_ID}."
    )
    log(
        f"Staff eligibility uses only linkedRecordIds from Organisation completing IA "
        f"(field {STAFF_ID_FILTER_FIELD_ID}); lookup display values are ignored."
    )
    log(
        f"Processing last {len(valid_records)} valid records in returned Interface order: "
        f"positions {first_position}-{last_position} of {len(valid_records_all)}; "
        "only these records will receive detail requests."
    )

    # The current client-list page may not expose the staff lookup. Discover
    # other Interface pages that expose the exact field, while keeping this
    # current page as the sole source of record ordering and selection.
    staff_field_pages = await find_interface_pages_for_field(
        session,
        COSS_SUPPORT_PROVIDED_FIELD_ID,
        AIRTABLE_TABLE_ID,
    )

    if not staff_field_pages:
        log(
            f"WARNING: no Interface page metadata exposes {COSS_SUPPORT_PROVIDED_FIELD_ID}; "
            "staff filtering will remain fail-closed.",
            "warning",
        )

    formatted_records = []
    detail_failures = 0
    staff_field_detail_matches = 0

    for index, rec in enumerate(valid_records, start=1):
        record_id = rec["id"]
        detail_cells = {}

        try:
            detail = await call_mcp_structured(
                session,
                "get_record_for_page",
                {
                    "baseId": AIRTABLE_BASE_ID,
                    "interfaceId": AIRTABLE_INTERFACE_ID,
                    "path": {
                        "root": {
                            "pageId": AIRTABLE_PAGE_ID,
                            "recordId": record_id,
                        },
                        "edges": [],
                    },
                },
            )
            # Keep the existing largest-map extraction for normal client
            # fields, but do NOT rely on it for staff eligibility. Interface
            # detail responses can contain multiple cell maps; the confirmed
            # CoSS Support Provided lookup may be in a different nested map.
            detail_cells = find_largest_cell_map(detail)
            staff_cells = find_cell_map_for_field(
                detail,
                COSS_SUPPORT_PROVIDED_FIELD_ID,
                expected_record_id=record_id,
            )

            # If the current client page does not expose the staff lookup,
            # query pages discovered from Interface metadata. The same client
            # record ID is used, so selection/order is unchanged.
            if not staff_cells:
                for source_page in staff_field_pages:
                    source_page_id = source_page.get("page_id")
                    if not source_page_id or source_page_id == AIRTABLE_PAGE_ID:
                        continue

                    source_detail = await call_mcp_structured(
                        session,
                        "get_record_for_page",
                        {
                            "baseId": AIRTABLE_BASE_ID,
                            "interfaceId": source_page.get("interface_id") or AIRTABLE_INTERFACE_ID,
                            "path": {
                                "root": {
                                    "pageId": source_page_id,
                                    "recordId": record_id,
                                },
                                "edges": [],
                            },
                        },
                    )

                    candidate_staff_cells = find_cell_map_for_field(
                        source_detail,
                        COSS_SUPPORT_PROVIDED_FIELD_ID,
                        expected_record_id=record_id,
                    )
                    if candidate_staff_cells:
                        staff_cells = candidate_staff_cells
                        break

            if staff_cells:
                staff_field_detail_matches += 1
                detail_cells = {
                    **(detail_cells if isinstance(detail_cells, dict) else {}),
                    **staff_cells,
                }

            if not detail_cells:
                detail_failures += 1
                log(
                    f"No detail field map returned for record {index}; "
                    "the staff filter will reject it if an allowlist is enabled.",
                    "warning",
                )
        except Exception as exc:
            detail_failures += 1
            log(
                f"Record detail request {index}/{len(valid_records)} failed: "
                f"{type(exc).__name__}; staff data may be unavailable.",
                "warning",
            )

        fields = {}
        list_cells = rec.get("cellValuesByFieldId", {})
        if isinstance(list_cells, dict):
            fields.update(list_cells)

        if isinstance(detail_cells, dict):
            fields.update(detail_cells)

        named_fields = rec.get("fields", {})
        if isinstance(named_fields, dict):
            fields.update(named_fields)

        formatted_records.append({
            "id": record_id,
            "createdTime": rec.get(
                "createdTime",
                datetime.now(timezone.utc).isoformat(),
            ),
            "fields": fields,
        })

        # Stay below Airtable's per-base request rate and avoid bursty MCP calls.
        await asyncio.sleep(0.2)

        if index % 25 == 0 or index == len(valid_records):
            log(
                f"Record detail progress in selected window: {index}/{len(valid_records)}; "
                f"successful records retained={len(formatted_records)}; "
                f"detail failures={detail_failures}."
            )

    log(
        f"Airtable fetch complete: {len(formatted_records)} records returned; "
        f"detail failures={detail_failures}; "
        f"detail responses containing {COSS_SUPPORT_PROVIDED_FIELD_ID}={staff_field_detail_matches}."
    )
    return formatted_records

# =========================================================
# MCP RECORD FETCHING
# =========================================================

async def airtable_mcp_get_all_records_legacy(session, limit=2000):
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

    payload = _to_plain_json(payload)

    if not isinstance(payload, dict):
        log(
            f"Failed to fetch structured records from MCP response "
            f"(type={type(payload).__name__}).",
            "error",
        )
        return []

    by_table = _to_plain_json(payload.get("recordsByTableId", {}))
    if not isinstance(by_table, dict):
        log("MCP response has no usable recordsByTableId mapping.", "error")
        return []

    raw_records = by_table.get(AIRTABLE_TABLE_ID, [])

    if not isinstance(raw_records, list):
        if by_table:
            raw_records = next(iter(by_table.values()))
        else:
            raw_records = []

    if not isinstance(raw_records, list):
        log("MCP table payload is not a list of records.", "error")
        return []

    log(
        f"MCP response contains {len(raw_records)} records for "
        f"table {AIRTABLE_TABLE_ID}"
    )

    formatted_records = []

    for rec in raw_records:
        rec = _to_plain_json(rec)
        if not isinstance(rec, dict):
            continue

        rec_id = rec.get("id")

        cell_map = _to_plain_json(
            rec.get("cellValuesByFieldId", {})
        )

        fields = {}

        if isinstance(cell_map, dict):
            for fid, val in cell_map.items():
                fields[str(fid)] = _to_plain_json(val)

        named_fields = _to_plain_json(rec.get("fields", {}))
        if isinstance(named_fields, dict):
            fields.update(named_fields)

        formatted_records.append({
            "id": rec_id,
            "createdTime": rec.get(
                "createdTime",
                datetime.now(timezone.utc).isoformat(),
            ),
            "fields": fields,
        })

    if formatted_records:
        first_field_count = len(
            formatted_records[0].get("fields", {})
        )
        log(
            f"First MCP record exposes {first_field_count} fields "
            f"through this Interface page"
        )

    log(
        f"Fetched {len(formatted_records)} Airtable records via Interface MCP"
    )
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
    """Extract a card's client record ID, preferring the explicit marker."""
    if isinstance(value, dict):
        texts = [
            str(value.get("desc", "") or ""),
            str(value.get("name", "") or ""),
        ]
    elif value:
        texts = [str(value)]
    else:
        return ""

    # Current sync-generated cards use this marker, avoiding accidental matches
    # against unrelated Airtable IDs that might appear elsewhere in a description.
    marker = re.compile(
        r"AIRTABLE_RECORD_ID\s*:\s*(rec[a-zA-Z0-9]{14})\b",
        re.IGNORECASE,
    )
    for text in texts:
        match = marker.search(text)
        if match:
            return match.group(1)

    # Backward compatibility for older cards that contain the record ID but
    # predate the explicit AIRTABLE_RECORD_ID marker.
    for text in texts:
        match = AIRTABLE_RECORD_ID_RE.search(text)
        if match:
            return match.group(0)
    return ""

def _normalise_field_value(value):
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, (str, int, float, bool)):
                parts.append(str(item))
            elif isinstance(item, dict):
                # Linked/select objects can expose a useful display value.
                for key in ("name", "text", "value", "id"):
                    candidate = item.get(key)
                    if candidate not in (None, ""):
                        parts.append(str(candidate))
                        break
        return ", ".join(parts).strip()

    if isinstance(value, dict):
        for key in ("name", "text", "value", "id"):
            candidate = value.get(key)
            if candidate not in (None, ""):
                return str(candidate).strip()
        return ""

    return str(value).strip() if value is not None else ""

FIELD_ID_BY_LABEL = {
    "ID & Forename Surname": "fld7l03Yg7qOpECmp",
    "Autonumber": "fldPf2R4oW9jQtHvO",
    "Created": "fldW1oINrdIcidIWx",
    "Contact number": "fldE8AdtJS1zTVH96",
    "WhatsApp number": "fldCGQj8U2DRaOQ7k",
    "Date of Birth": "fldrZw6Yoaq0u0y1n",
    "Date of initial assessment^": "flddo4p3Otm1nbmHo",
    "SPRING - Issue^": "fld971YcvkXq6S8mH",
    "Country of Origin": "fldDClPRHrgK1UGS7",
    "English Level": "fld0UgiXHFfnaY6VS",
    "Address": "fld8tLF2u32xo6qAH",
    "Postcode": "fld5qrchb1M9ajSki",
    "Email": "fldFyVLy7f7ak6jsL",
    "BRP Number": "fld5kYbibM4hgFvxn",
    "NI Number": "fldRe74sUCPeFQSOg",
    "Gender": "fldr2W8wJLLhXMmg9",
    "Interpreter needed?": "fldRuig8utUdvSgyu",
    "Organisation completing IA": "fldIOi1f33xWKzIJC",
    "Link": "fldummc1QJ9vEXOga",
    "CoSS Support Provided": "fldIae0zVo5CMLfOj",
    "Language(s) Spoken": "fldpddkNvjkDhGZ7B",
    "Client age": "fldJ9XiJz6WRsMSAI",
    "Risk!": "fldVpd1cDlvgeKb8V",
    "SCC - Referral MAX^": "fldmDnlxO1FvsDGLt",
    "CAS - Referral MAX^": "fldDVSdzb4O9krO2q",
    "CoSS - Referral MAX^": "fld4YZLlnBaUyYH0d",
    "SOLACE - Referral MAX^": "fld3yDQ4tclxVr3ma",
    "NB - Referral MAX^": "fldWuYgOi7BLyXLOB",
    "SAVTE - Referral MAX^": "fldnaPeZ0dWJXxiSa",
}

def _display_text_values(value):
    """Collect readable display strings from Interface lookup/select payloads."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        if not text or AIRTABLE_RECORD_ID_RE.fullmatch(text):
            return []
        return [text]
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_display_text_values(item))
        return result
    if isinstance(value, dict):
        # Prefer actual display labels, not IDs. A lookup's linkedRecordIds
        # identifies linked records but is not display text for a Trello card.
        for key in ("name", "label", "text", "displayValue", "value", "url"):
            if key in value:
                candidate = _display_text_values(value[key])
                if candidate:
                    return candidate
        values_by_id = value.get("valuesByLinkedRecordId")
        if isinstance(values_by_id, dict):
            result = []
            for mapped in values_by_id.values():
                result.extend(_display_text_values(mapped))
            return result
        # Some Interface lookup values expose only nested scalar structures.
        result = []
        for key, child in value.items():
            if key not in {"linkedRecordIds", "id", "recordId", "recordIds"}:
                result.extend(_display_text_values(child))
        return result
    return []


def get_field(fields, *keys):
    """Retrieve a field by its confirmed Airtable ID or exact Interface label.

    Supports normal scalars, lists, select objects, and lookup objects such as
    valuesByLinkedRecordId. It prefers display values and does not turn linked
    Airtable record IDs into card text.
    """
    if not isinstance(fields, dict):
        return ""

    for key in keys:
        field_id = FIELD_ID_BY_LABEL.get(key)
        for candidate in (field_id, key):
            if not candidate or candidate not in fields:
                continue
            value = fields[candidate]
            if value is None:
                continue
            values = _display_text_values(value)
            result = ", ".join(dict.fromkeys(text for text in values if text.strip()))
            if result and result.casefold() not in {"none", "null", "[]"}:
                return result.strip()
    return ""


def has_scc_referral(fields):
    """True only when the SCC Referral MAX field contains a valid ISO date.

    This is the sole eligibility criterion for the new SCC-only Trello sync.
    It deliberately does not inspect or require approved staff IDs.
    """
    referral_date = get_field(fields, "SCC - Referral MAX^")

    if not referral_date:
        return False

    date_text = str(referral_date).strip()[:10]

    try:
        datetime.strptime(date_text, "%Y-%m-%d")
    except (ValueError, TypeError):
        return False

    return True


COSS_SUPPORT_PROVIDED_FIELD_ID = "fldIae0zVo5CMLfOj"


def extract_staff_ids_from_organisation_lookup(fields):
    """Extract staff record IDs from the lookup's linkedRecordIds only.

    The organisation display values and valuesByLinkedRecordId values
    are deliberately ignored.
    """
    if not isinstance(fields, dict):
        return set()

    value = fields.get(STAFF_ID_FILTER_FIELD_ID)

    if value is None:
        value = fields.get("Organisation completing IA")

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            decoded = None

        if isinstance(decoded, (dict, list)):
            value = decoded
        elif AIRTABLE_RECORD_ID_RE.fullmatch(value.strip()):
            value = {"linkedRecordIds": [value.strip()]}
        else:
            return set()

    if isinstance(value, dict):
        linked_ids = value.get("linkedRecordIds", [])
    elif isinstance(value, list):
        linked_ids = value
    else:
        return set()

    if not isinstance(linked_ids, list):
        return set()

    return {
        item.strip().casefold()
        for item in linked_ids
        if isinstance(item, str)
        and AIRTABLE_RECORD_ID_RE.fullmatch(item.strip())
    }




def extract_approved_organisation_ids(fields):
    """Read only linkedRecordIds from Organisation completing IA."""

    if not isinstance(fields, dict):
        return set()

    value = fields.get(STAFF_ID_FILTER_FIELD_ID)

    if value is None:
        value = fields.get("Organisation completing IA")

    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            decoded = None

        if isinstance(decoded, (dict, list)):
            value = decoded
        elif AIRTABLE_RECORD_ID_RE.fullmatch(value.strip()):
            value = {"linkedRecordIds": [value.strip()]}
        else:
            return set()

    if isinstance(value, dict):
        linked_ids = value.get("linkedRecordIds", [])
    elif isinstance(value, list):
        linked_ids = value
    else:
        return set()

    if not isinstance(linked_ids, list):
        return set()

    return {
        item.strip().casefold()
        for item in linked_ids
        if isinstance(item, str)
        and AIRTABLE_RECORD_ID_RE.fullmatch(item.strip())
    }


def record_has_approved_organisation_link(fields):
    """Eligible when an Organisation lookup linked ID matches the allowlist."""

    if not ALLOWED_STAFF_IDS:
        return False

    linked_ids = extract_approved_organisation_ids(fields)
    return bool(linked_ids.intersection(ALLOWED_STAFF_IDS))


def verify_staff_allowed(fields, allowed_staff_set=None, allowed_staff_ids=None):
    """Compatibility wrapper for the existing sync filtering calls."""

    if allowed_staff_ids is None:
        allowed_staff_ids = ALLOWED_STAFF_IDS

    if not isinstance(fields, dict) or not allowed_staff_ids:
        return False

    linked_ids = extract_approved_organisation_ids(fields)

    valid_allowed_ids = {
        str(item).strip().casefold()
        for item in allowed_staff_ids
        if AIRTABLE_RECORD_ID_RE.fullmatch(str(item).strip())
    }

    return bool(linked_ids.intersection(valid_allowed_ids))


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

def ensure_referral_label(card_id, referral_requested_date=None):
    """Remove the obsolete label; never add 'No Referral made to SCC'."""

    labels = trello_get(f"/cards/{card_id}/labels")
    removed = 0

    for label in labels:
        if label.get("name") == "No Referral made to SCC":
            trello_delete(f"/cards/{card_id}/idLabels/{label['id']}")
            removed += 1

    if removed:
        log(
            f"Removed obsolete 'No Referral made to SCC' label "
            f"from card {card_id}."
        )

# =========================================================
# TRELLO API
# =========================================================

class TrelloAPIError(RuntimeError):
    """Sanitised Trello API error; never includes the request URL or auth query."""
    def __init__(self, method, path, status_code, response_text=""):
        self.method = method
        self.path = path
        self.status_code = status_code
        self.response_text = response_text
        detail = (response_text or "(no response body)").strip()
        # Do not allow credentials to leak even if echoed by an unexpected response.
        for secret in (TRELLO_API_KEY, TRELLO_API_TOKEN):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        detail = detail[:1200]
        super().__init__(
            f"Trello {method} {path} failed with HTTP {status_code}: {detail}"
        )


def _trello_check_response(response, method, path):
    if response.ok:
        return
    # The response body often identifies the invalid Trello field. Do not use
    # raise_for_status(), because requests includes the URL and its auth query.
    raise TrelloAPIError(
        method,
        path,
        response.status_code,
        response.text,
    )


def trello_get(path, extra_params=None):
    params = dict(AUTH)
    if extra_params:
        params.update(extra_params)
    try:
        r = requests.get(f"{BASE_URL}{path}", params=params, timeout=30)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Trello GET {path} request failed ({type(exc).__name__}); "
            "request URL and credentials omitted."
        ) from None
    _trello_check_response(r, "GET", path)
    return r.json()


def trello_post(path, data):
    try:
        r = requests.post(f"{BASE_URL}{path}", params=AUTH, data=data, timeout=30)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Trello POST {path} request failed ({type(exc).__name__}); "
            "request URL and credentials omitted."
        ) from None
    _trello_check_response(r, "POST", path)
    return r.json()


def trello_put(path, data):
    try:
        r = requests.put(f"{BASE_URL}{path}", params=AUTH, data=data, timeout=30)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Trello PUT {path} request failed ({type(exc).__name__}); "
            "request URL and credentials omitted."
        ) from None
    _trello_check_response(r, "PUT", path)
    # Trello may return an empty body for some successful operations.
    return r.json() if r.text.strip() else {}


def trello_delete(path):
    try:
        r = requests.delete(f"{BASE_URL}{path}", params=AUTH, timeout=30)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Trello DELETE {path} request failed ({type(exc).__name__}); "
            "request URL and credentials omitted."
        ) from None
    _trello_check_response(r, "DELETE", path)

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

def _card_activity_datetime(card):
    """Return a timezone-aware last-activity timestamp, or UTC minimum."""
    try:
        value = parse_iso(card.get("dateLastActivity") or "")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def load_existing_cards():
    """Index existing Trello cards to prevent duplicate Airtable client cards.

    All lists on the board containing CREATE_LIST_ID are scanned by default,
    including archived cards. Explicit TRELLO_EXISTING_LIST_IDS are included
    as well. A card with the same Airtable record ID is updated/reused rather
    than recreated, even if it was archived a long time ago.
    """
    if not CREATE_LIST_ID:
        raise RuntimeError("TRELLO_CREATE_LIST_ID is required before scanning Trello.")

    configured_list_ids = list(dict.fromkeys(
        [lid for lid in EXISTING_LIST_IDS if lid] + [CREATE_LIST_ID]
    ))
    list_ids = list(configured_list_ids)

    if TRELLO_SCAN_ALL_BOARD_LISTS:
        # Fail closed if full-board discovery fails: incomplete scanning could
        # lead to duplicate card creation in another list.
        list_info = trello_get(f"/lists/{CREATE_LIST_ID}", {"fields": "id,idBoard"})
        board_id = list_info.get("idBoard") if isinstance(list_info, dict) else None
        if not board_id:
            raise RuntimeError("Could not determine the Trello board for TRELLO_CREATE_LIST_ID.")

        board_lists = trello_get(f"/boards/{board_id}/lists", {"filter": "all"})
        if not isinstance(board_lists, list):
            raise RuntimeError("Trello did not return a usable list of board lists; refusing to sync.")
        discovered = [item.get("id") for item in board_lists if isinstance(item, dict) and item.get("id")]
        list_ids = list(dict.fromkeys(configured_list_ids + discovered))

    log(
        f"Trello duplicate scan: checking {len(list_ids)} list(s); "
        f"board-wide scan={'enabled' if TRELLO_SCAN_ALL_BOARD_LISTS else 'disabled'}."
    )

    cards_by_airtable_id = {}
    phone_index = {}
    seen_card_ids = set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_ARCHIVE_DAYS)

    for lid in list_ids:
        fetched_cards = trello_get(f"/lists/{lid}/cards", {
            "cards": "all",
            "fields": "id,name,desc,closed,dateLastActivity,shortUrl,idList"
        })
        if not isinstance(fetched_cards, list):
            raise RuntimeError(f"Trello returned an invalid card list for list {lid}; refusing to sync.")

        for card in fetched_cards:
            if not isinstance(card, dict) or not card.get("id"):
                continue
            if card["id"] in seen_card_ids:
                continue
            seen_card_ids.add(card["id"])
            card.setdefault("idList", lid)

            is_archived = bool(card.get("closed", False))
            activity = _card_activity_datetime(card)
            stale_archived = is_archived and activity < cutoff

            airtable_id = extract_airtable_id_from_card(card)
            if airtable_id:
                cards_by_airtable_id.setdefault(airtable_id, []).append({
                    "card": card,
                    "closed": is_archived,
                    "archived_stale": stale_archived,
                    "activity": activity,
                })

            # Phone-based legacy cleanup is limited to open or recently archived
            # cards; very old archived cards still count for Airtable-ID dedupe,
            # but should not be treated as active phone matches.
            if not stale_archived:
                phone = extract_phone_from_card(card)
                if phone:
                    phone_index.setdefault(phone, []).append(card)

    cards = {}
    duplicate_groups = 0
    duplicate_cards_found = 0

    for airtable_id, candidates in cards_by_airtable_id.items():
        # Prefer an open card; within the same state choose most recently active.
        candidates.sort(
            key=lambda item: (not item["closed"], item["activity"]),
            reverse=True,
        )
        chosen = candidates[0]
        chosen_card = chosen["card"]
        cards[airtable_id] = {
            "card": chosen_card,
            "closed": chosen["closed"],
            "archived_stale": chosen["archived_stale"],
        }

        if len(candidates) > 1:
            duplicate_groups += 1
            duplicate_cards_found += len(candidates) - 1
            log(
                f"Duplicate Trello cards already exist for Airtable record "
                f"{airtable_id}: found {len(candidates)}; using card "
                f"{chosen_card['id']} as the canonical card.",
                "warning",
            )
            if TRELLO_AUTO_CLOSE_DUPLICATE_CARDS:
                for duplicate in candidates[1:]:
                    duplicate_card = duplicate["card"]
                    if not duplicate["closed"]:
                        trello_put(f"/cards/{duplicate_card['id']}", {"closed": "true"})
                        log(
                            f"Closed duplicate Trello card {duplicate_card['id']} "
                            f"for Airtable record {airtable_id}.",
                            "warning",
                        )

    log(
        f"Trello duplicate scan complete: unique Airtable IDs={len(cards)}; "
        f"duplicate record groups={duplicate_groups}; "
        f"extra duplicate cards={duplicate_cards_found}."
    )
    return cards, phone_index

# =========================================================
# MAIN EXECUTION VIA INTERFACE PAGE 2
# =========================================================

async def diagnose_creator_fields(session):
    """Find creator/staff fields using the correct Airtable MCP schema input."""

    # First collect field IDs exposed through the Interface metadata.
    metadata = await call_mcp_structured(
        session,
        "list_pages_for_base",
        {"baseId": AIRTABLE_BASE_ID},
    )

    if not isinstance(metadata, dict):
        log("Could not retrieve Interface metadata.", "warning")
        return

    field_ids = set()

    for interface in metadata.get("interfaces", []):
        for page in interface.get("pages", []):
            tables = page.get("tablesByTableId", {})

            if not isinstance(tables, dict):
                continue

            table = tables.get(AIRTABLE_TABLE_ID)

            if not isinstance(table, dict):
                continue

            for field in table.get("fields", []):
                if not isinstance(field, dict):
                    continue

                field_id = field.get("id") or field.get("fieldId")

                if isinstance(field_id, str) and field_id.startswith("fld"):
                    field_ids.add(field_id)

    if not field_ids:
        log("No field IDs found in Interface metadata.", "warning")
        return

    log(
        f"Requesting schema for {len(field_ids)} Interface-exposed fields."
    )

    # CORRECT MCP ARGUMENT FORMAT:
    schema = await call_mcp_structured(
        session,
        "get_table_schema",
        {
            "baseId": AIRTABLE_BASE_ID,
            "tables": [
                {
                    "tableId": AIRTABLE_TABLE_ID,
                    "fieldIds": sorted(field_ids),
                }
            ],
        },
    )

    if not isinstance(schema, dict):
        log("Could not retrieve table schema.", "warning")
        return

    keywords = (
        "created by",
        "creator",
        "added by",
        "entered by",
        "submitted by",
        "record owner",
        "staff",
        "volunteer",
        "assigned",
        "completed by",
    )

    candidates = {}
    seen = set()

    def walk(obj):
        if isinstance(obj, dict):
            field_id = obj.get("id") or obj.get("fieldId")
            name = obj.get("name") or obj.get("label") or ""
            field_type = obj.get("type", "")

            if (
                isinstance(field_id, str)
                and field_id.startswith("fld")
                and field_id not in seen
                and isinstance(name, str)
                and any(k in name.casefold() for k in keywords)
            ):
                seen.add(field_id)
                candidates[field_id] = (name, field_type)

            for value in obj.values():
                walk(value)

        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(schema)

    log("ACTUAL AIRTABLE CREATOR FIELD DISCOVERY:")

    if not candidates:
        log(
            "No creator/staff fields found in the returned schema. "
            "The Interface may not expose the creator field.",
            "warning",
        )
        return

    for field_id, (name, field_type) in sorted(
        candidates.items(),
        key=lambda item: item[1][0].casefold(),
    ):
        log(
            f"FIELD CANDIDATE: {field_id} | "
            f"name={name!r} | type={field_type!r}"
        )



async def diagnose_client_navigation_targets(session, records):
    """Inspect linked-record navigation exposed by the client Interface page."""

    if not records:
        log("No client records available for navigation diagnostic.")
        return

    # Use the first accessible client record purely to inspect the
    # Interface navigation metadata. No values are printed.
    client_id = records[0].get("id")

    if not client_id:
        log("First client record has no Airtable record ID.")
        return

    log(
        "INSPECTING CLIENT NAVIGATION TARGETS: "
        f"page={AIRTABLE_PAGE_ID}; record={client_id}"
    )

    detail = await call_mcp_structured(
        session,
        "get_record_for_page",
        {
            "baseId": AIRTABLE_BASE_ID,
            "interfaceId": AIRTABLE_INTERFACE_ID,
            "path": {
                "root": {
                    "pageId": AIRTABLE_PAGE_ID,
                    "recordId": client_id,
                },
                "edges": [],
            },
        },
    )

    if not isinstance(detail, (dict, list)):
        log("Client record detail could not be retrieved.")
        return

    navigation_targets = []

    def walk(obj):
        if isinstance(obj, dict):
            value = obj.get("navigationTargets")

            if isinstance(value, list):
                navigation_targets.extend(value)

            for child in obj.values():
                walk(child)

        elif isinstance(obj, list):
            for child in obj:
                walk(child)

    walk(detail)

    # Deduplicate structurally.
    unique = []
    seen = set()

    for target in navigation_targets:
        try:
            marker = json.dumps(
                target,
                sort_keys=True,
                default=str,
            )
        except Exception:
            marker = repr(target)

        if marker not in seen:
            seen.add(marker)
            unique.append(target)

    log(
        f"CLIENT NAVIGATION TARGETS FOUND: {len(unique)}"
    )

    for target in unique:
        if not isinstance(target, dict):
            log(
                "NAVIGATION TARGET: "
                f"type={type(target).__name__}"
            )
            continue

        field_id = target.get("fieldId")
        field_name = (
            target.get("fieldName")
            or target.get("name")
            or target.get("label")
        )

        linked_ids = target.get("linkedRecordIds")

        if not isinstance(linked_ids, list):
            linked_ids = []

        valid_ids = {
            item.casefold()
            for item in linked_ids
            if isinstance(item, str)
            and AIRTABLE_RECORD_ID_RE.fullmatch(item.strip())
        }

        log(
            "NAVIGATION TARGET: "
            f"fieldId={field_id} | "
            f"fieldName={field_name!r} | "
            f"linkedRecordIds={len(valid_ids)}"
        )

        # If this is the Interventions relationship, remember the exact field.
        if (
            isinstance(field_name, str)
            and "intervention" in field_name.casefold()
        ) or (
            isinstance(field_id, str)
            and field_id.startswith("fld")
        ):
            log(
                "  INTERVENTION-CANDIDATE TARGET: "
                f"fieldId={field_id} | "
                f"fieldName={field_name!r} | "
                f"linkedRecordIds={len(valid_ids)}"
            )

    log(
        "CLIENT NAVIGATION DIAGNOSTIC COMPLETE. "
        "No Airtable or Trello records were modified."
    )



async def diagnose_intervention_query_contexts(session):
    """Test query-origin context from the supplied Airtable detail URLs."""

    detail_page = "pagB5URnGwh70uwTB"
    parent_page = "pagLsUMtGqYgvu2a5"

    samples = [
        {
            "record": "rectIxDdQpg6pTsD7",
            "element": "pelHRj8byhL6mKTOC",
            "container": "pelM7yUGKwjysu2um",
        },
        {
            "record": "rec7C1gTkiif7ahNY",
            "element": "pelEamfvazVCkY2Os",
            "container": "pelyqyfUb2bni6O0h",
        },
    ]

    successes = 0

    for sample in samples:
        for page_id in (detail_page, parent_page):
            for element_id in (
                sample["element"],
                sample["container"],
            ):
                label = (
                    f"page={page_id}; "
                    f"record={sample['record']}; "
                    f"element={element_id}"
                )

                log("QUERY CONTEXT TEST: " + label)

                try:
                    result = await call_mcp_structured(
                        session,
                        "get_record_for_page",
                        {
                            "baseId": AIRTABLE_BASE_ID,
                            "interfaceId": AIRTABLE_INTERFACE_ID,
                            "path": {
                                "root": {
                                    "pageId": page_id,
                                    "recordId": sample["record"],
                                    "elementId": element_id,
                                },
                                "edges": [],
                            },
                        },
                    )
                except Exception as exc:
                    log(
                        f"QUERY CONTEXT RESULT: failed ({type(exc).__name__})",
                        "warning",
                    )
                    continue

                if not isinstance(result, (dict, list)):
                    log("QUERY CONTEXT RESULT: no record returned.")
                    continue

                successes += 1
                log("QUERY CONTEXT RESULT: SUCCESS")

                maps = list_cell_maps(result)

                log(f"Returned cell maps: {len(maps)}")

                for path, cells in maps:
                    log(
                        f"INTERVENTION RECORD FIELD MAP: {path} | "
                        f"field count={len(cells)}"
                    )
                    log(
                        "RETURNED FIELD IDS: "
                        + ", ".join(sorted(str(k) for k in cells.keys()))
                    )

                navigation = []

                def walk(obj):
                    if isinstance(obj, dict):
                        targets = obj.get("navigationTargets")
                        if isinstance(targets, list):
                            navigation.extend(targets)
                        for child in obj.values():
                            walk(child)
                    elif isinstance(obj, list):
                        for child in obj:
                            walk(child)

                walk(result)

                log(
                    f"Returned navigation targets: {len(navigation)}"
                )

                for target in navigation:
                    if isinstance(target, dict):
                        log(
                            "INTERVENTION NAVIGATION FIELD: "
                            f"fieldId={target.get('fieldId')}; "
                            f"name={target.get('fieldName') or target.get('name')}; "
                            f"linked records="
                            f"{len(target.get('linkedRecordIds', [])) if isinstance(target.get('linkedRecordIds'), list) else 0}"
                        )

                await asyncio.sleep(0.2)

    log(
        f"INTERVENTION QUERY CONTEXT TEST COMPLETE: "
        f"successful requests={successes}/8."
    )



async def diagnose_intervention_parent_elements(session):
    """Test parent Interface elements identified by supplied detail URLs."""

    candidates = [
        ("element A", "pelHRj8byhL6mKTOC"),
        ("container A", "pelM7yUGKwjysu2um"),
        ("element B", "pelEamfvazVCkY2Os"),
        ("container B", "pelyqyfUb2bni6O0h"),
    ]

    metadata = await call_mcp_structured(
        session,
        "list_pages_for_base",
        {"baseId": AIRTABLE_BASE_ID},
    )

    table_names = {}
    field_names = {}

    if isinstance(metadata, dict):
        for interface in metadata.get("interfaces", []):
            for page in interface.get("pages", []):
                tables = page.get("tablesByTableId", {})
                if not isinstance(tables, dict):
                    continue

                for table_id, table in tables.items():
                    if not isinstance(table, dict):
                        continue

                    table_names[table_id] = (
                        table.get("name")
                        or table.get("label")
                        or "Unknown"
                    )

                    for field in table.get("fields", []):
                        if not isinstance(field, dict):
                            continue
                        fid = field.get("id") or field.get("fieldId")
                        name = field.get("name") or field.get("label")
                        if fid and name:
                            field_names[fid] = name

    total_successes = 0

    for label, element_id in candidates:
        log(
            f"PARENT ELEMENT TEST: {label}; "
            f"page={AIRTABLE_PAGE_ID}; element={element_id}"
        )

        result = await call_mcp_structured(
            session,
            "list_records_for_page",
            {
                "baseId": AIRTABLE_BASE_ID,
                "interfaceId": AIRTABLE_INTERFACE_ID,
                "pageId": AIRTABLE_PAGE_ID,
                "elementId": element_id,
            },
        )

        if not isinstance(result, dict):
            log(f"PARENT ELEMENT RESULT: {label}; no usable records.")
            continue

        total_successes += 1
        by_table = result.get("recordsByTableId", {})

        if not isinstance(by_table, dict) or not by_table:
            log(f"PARENT ELEMENT RESULT: {label}; empty table result.")
            continue

        for table_id, records in by_table.items():
            if not isinstance(records, list):
                continue

            log(
                f"PARENT ELEMENT TABLE: {label}; "
                f"name={table_names.get(table_id, 'Unknown')}; "
                f"tableId={table_id}; records={len(records)}"
            )

            returned_fields = set()

            for record in records:
                if not isinstance(record, dict):
                    continue

                cells = record.get("cellValuesByFieldId", {})
                if isinstance(cells, dict):
                    returned_fields.update(cells.keys())

            for field_id in sorted(returned_fields):
                field_name = field_names.get(field_id, "Name not exposed")
                if any(
                    term in str(field_name).casefold()
                    for term in (
                        "client", "staff", "volunteer", "date",
                        "organisation", "support", "notes",
                    )
                ):
                    log(
                        f"PARENT ELEMENT FIELD: {label}; "
                        f"{field_id} | {field_name}"
                    )

        await asyncio.sleep(0.2)

    log(
        f"PARENT ELEMENT TEST COMPLETE: "
        f"successful element requests={total_successes}/4."
    )


async def execute_sync_pipeline(session):

    await diagnose_service_activity_source(session)
    log("🚀 Starting Airtable Interface MCP → Trello Sync")

    # Fetch-only must return before any Trello API is called. In this mode we
    # can safely validate the staff lookup and count eligible records.
    records = await airtable_mcp_get_all_records(session, limit=2000)


    # =========================================================
    # SINGLE CLIENT PRIVATE DETAIL EXPORT
    # Requires AIRTABLE_MCP_FETCH_ONLY=true.
    # Writes one selected client's complete returned field map
    # to a private JSON file. Does not print field values.
    # =========================================================
    target_record_id = os.getenv("AIRTABLE_DETAIL_RECORD_ID", "").strip()

    if target_record_id:
        if not MCP_FETCH_ONLY:
            raise RuntimeError(
                "Single-client detail export requires "
                "AIRTABLE_MCP_FETCH_ONLY=true."
            )

        target_record = next(
            (
                record for record in records
                if isinstance(record, dict)
                and record.get("id") == target_record_id
            ),
            None,
        )

        if target_record is None:
            log(
                "SINGLE CLIENT EXPORT: requested record was not found "
                "in the selected Interface record window. "
                "Check the ID and AIRTABLE_RECORD_WINDOW."
            )
        else:
            target_fields = target_record.get("fields", {})
            if not isinstance(target_fields, dict):
                target_fields = {}

            field_labels = {
                str(field_id): label
                for label, field_id in FIELD_ID_BY_LABEL.items()
            }

            detailed_fields = []

            for field_id, value in sorted(
                target_fields.items(),
                key=lambda item: str(item[0]),
            ):
                field_id = str(field_id)

                detailed_fields.append({
                    "field_id": field_id,
                    "field_label": field_labels.get(field_id, field_id),
                    "value": value,
                })

            import csv

            safe_id = re.sub(
                r"[^A-Za-z0-9_-]",
                "",
                target_record_id,
            )
            output_path = Path("/tmp") / (
                f"airtable_client_detail_{safe_id}.csv"
            )

            with output_path.open(
                "w",
                encoding="utf-8-sig",
                newline="",
            ) as csv_file:
                writer = csv.DictWriter(
                    csv_file,
                    fieldnames=[
                        "record_id",
                        "field_id",
                        "field_label",
                        "value",
                    ],
                )
                writer.writeheader()

                for item in detailed_fields:
                    value = item.get("value")

                    if isinstance(value, (dict, list)):
                        value_text = json.dumps(
                            value,
                            ensure_ascii=False,
                            default=str,
                        )
                    elif value is None:
                        value_text = ""
                    else:
                        value_text = str(value)

                    writer.writerow({
                        "record_id": target_record_id,
                        "field_id": item["field_id"],
                        "field_label": item["field_label"],
                        "value": value_text,
                    })

            output_path.chmod(0o600)

            log(
                f"SINGLE CLIENT PRIVATE DETAIL EXPORT: "
                f"{len(detailed_fields)} fields written."
            )
            log(f"Private JSON file: {output_path}")
            log(
                "Field labels/IDs: "
                + ", ".join(
                    item["field_label"]
                    for item in detailed_fields
                )
            )

        # Stop here. Never continue into the Trello sync.
        return


    # =========================================================
    # NONAPPROVED ORGANISATION RECORD ID DISCOVERY
    # EXACT SAME RULE AS THE 43/50 ELIGIBILITY SCAN:
    # linked IDs from Organisation completing IA intersect the
    # configured approved Airtable organisation IDs.
    # =========================================================
    nonapproved_record_ids = []
    approved_record_ids = []

    for record in records:
        if not isinstance(record, dict):
            continue

        fields = record.get("fields", {})

        if not isinstance(fields, dict):
            continue

        value = fields.get("fldIOi1f33xWKzIJC")

        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                pass

        try:
            serialised = json.dumps(
                value,
                ensure_ascii=False,
                default=str,
            )
        except Exception:
            serialised = str(value)

        linked_ids = {
            match.casefold()
            for match in AIRTABLE_RECORD_ID_RE.findall(serialised)
        }

        approved = bool(
            linked_ids.intersection(ALLOWED_STAFF_IDS)
        )

        record_id = record.get("id")

        if not record_id:
            continue

        if approved:
            approved_record_ids.append(record_id)
        else:
            nonapproved_record_ids.append(record_id)

    log(
        f"EXACT ORGANISATION FILTER CHECK: "
        f"approved={len(approved_record_ids)}/{len(records)}; "
        f"nonapproved={len(nonapproved_record_ids)}/{len(records)}."
    )

    for index, record_id in enumerate(nonapproved_record_ids, start=1):
        log(
            f"NONAPPROVED ORGANISATION RECORD {index}: {record_id}"
        )

    # =========================================================
    nonapproved_record_ids = []

    for record in records:
        if not isinstance(record, dict):
            continue

        fields = record.get("fields", {})

        if not isinstance(fields, dict):
            continue

        approved = verify_staff_allowed(
            fields,
            set(),
            ALLOWED_STAFF_IDS,
        )

        if not approved:
            record_id = record.get("id")

            if record_id:
                nonapproved_record_ids.append(record_id)

    log(
        f"NONAPPROVED STAFF RECORD ID DISCOVERY: "
        f"{len(nonapproved_record_ids)} records failed the approved "
        f"Organisation completing IA filter."
    )

    for index, record_id in enumerate(nonapproved_record_ids, start=1):
        log(
            f"NONAPPROVED RECORD {index}: {record_id}"
        )


    # =========================================================
    # COSS CLIENT FIELD POPULATION DIAGNOSTIC

    # =========================================================
    # =========================================================
    # APPROVED + COSS OVERLAP DIAGNOSTIC
    # Uses the same Organisation-completing-IA logic as the
    # real eligibility filter.
    # =========================================================
    log("APPROVED + COSS OVERLAP DIAGNOSTIC:")

    approved_count = 0
    approved_and_coss = 0
    approved_without_coss = 0
    nonapproved_and_coss = 0
    nonapproved_without_coss = 0

    coss_fields_for_overlap = {
        "fldK2muwDjOv0swUz",   # CoSS Grouping
        "fld9T6W0FyxyMw4E8",   # CoSS Notes
        "fldIae0zVo5CMLfOj",   # CoSS Support Provided
        "fldTWFlUNRO46IX4O",   # CoSS Support Provided (2026)
    }

    for record in records:
        fields = record.get("fields", {})

        if not isinstance(fields, dict):
            continue

        # IMPORTANT:
        # Reuse the same tested Organisation completing IA
        # eligibility function as the real pipeline.
        approved = verify_staff_allowed(
            fields,
            set(),
            ALLOWED_STAFF_IDS,
        )

        coss = False

        for field_id in coss_fields_for_overlap:
            value = fields.get(field_id)

            if value in (None, "", [], {}, False):
                continue

            text = str(value).strip()

            if text and text.casefold() not in {
                "none",
                "null",
                "[]",
                "{}",
            }:
                coss = True
                break

        if approved:
            approved_count += 1

            if coss:
                approved_and_coss += 1
            else:
                approved_without_coss += 1
        else:
            if coss:
                nonapproved_and_coss += 1
            else:
                nonapproved_without_coss += 1

    log(
        f"Approved organisation clients={approved_count}; "
        f"approved + CoSS={approved_and_coss}; "
        f"approved without CoSS={approved_without_coss}; "
        f"nonapproved + CoSS={nonapproved_and_coss}; "
        f"nonapproved without CoSS={nonapproved_without_coss}."
    )

    # =========================================================
    log("APPROVED + COSS OVERLAP DIAGNOSTIC:")

    approved_count = 0

    coss_fields_for_overlap = {
        "fldK2muwDjOv0swUz",   # CoSS Grouping
        "fld9T6W0FyxyMw4E8",   # CoSS Notes
        "fldIae0zVo5CMLfOj",   # CoSS Support Provided
        "fldTWFlUNRO46IX4O",   # CoSS Support Provided (2026)
    }

    approved_and_coss = 0
    approved_without_coss = 0
    nonapproved_and_coss = 0
    nonapproved_without_coss = 0

    for record in records:
        fields = record.get("fields", {})

        if not isinstance(fields, dict):
            continue

        organisation_value = fields.get("fldIOi1f33xWKzIJC")

        if isinstance(organisation_value, str):
            try:
                organisation_value = json.loads(organisation_value)
            except (TypeError, ValueError):
                organisation_value = None

        serialised_org = json.dumps(
            organisation_value,
            ensure_ascii=False,
            default=str,
        )

        organisation_ids = {
            match.casefold()
            for match in AIRTABLE_RECORD_ID_RE.findall(serialised_org)
        }

        approved = bool(
            organisation_ids.intersection(ALLOWED_STAFF_IDS)
        )

        coss = False

        for field_id in coss_fields_for_overlap:
            value = fields.get(field_id)

            if value not in (None, "", [], {}, False):
                text = str(value).strip()

                if text and text.lower() not in {
                    "none",
                    "null",
                    "[]",
                    "{}",
                }:
                    coss = True
                    break

        if approved:
            approved_count += 1

            if coss:
                approved_and_coss += 1
            else:
                approved_without_coss += 1
        else:
            if coss:
                nonapproved_and_coss += 1
            else:
                nonapproved_without_coss += 1

    log(
        f"Approved organisation clients={approved_count}; "
        f"approved + CoSS={approved_and_coss}; "
        f"approved without CoSS={approved_without_coss}; "
        f"nonapproved + CoSS={nonapproved_and_coss}; "
        f"nonapproved without CoSS={nonapproved_without_coss}."
    )

    # Counts only; never prints client values or record IDs.
    # =========================================================
    log("COSS CLIENT FIELD POPULATION DIAGNOSTIC:")

    coss_candidate_fields = {
        "fldH6Q298A8tSJ8mI": "CoSS - Action taken during assessment",
        "fldK2muwDjOv0swUz": "CoSS Grouping",
        "fldq0IjjnNYvWwYfZ": "CoSS Nature of Issue (2026)",
        "fld9T6W0FyxyMw4E8": "CoSS Notes",
        "fldIae0zVo5CMLfOj": "CoSS Support Provided",
        "fldTWFlUNRO46IX4O": "CoSS Support Provided (2026)",
        "fld4YZLlnBaUyYH0d": "CoSS - Referral MAX",
    }

    coss_counts = {
        field_id: 0
        for field_id in coss_candidate_fields
    }

    coss_any_count = 0

    for record in records:
        fields = record.get("fields", {})

        if not isinstance(fields, dict):
            continue

        record_has_coss = False

        for field_id in coss_candidate_fields:
            value = fields.get(field_id)

            if value not in (None, "", [], {}, False):
                text = str(value).strip()

                if text and text.lower() not in {
                    "none",
                    "null",
                    "[]",
                    "{}",
                }:
                    coss_counts[field_id] += 1
                    record_has_coss = True

        if record_has_coss:
            coss_any_count += 1

    for field_id, label in coss_candidate_fields.items():
        log(
            f"COSS CLIENT FIELD: {field_id} | "
            f"label={label!r} | "
            f"populated={coss_counts[field_id]}/{len(records)}"
        )

    log(
        f"COSS CLIENT FIELDS: at least one candidate populated="
        f"{coss_any_count}/{len(records)}"
    )
    await diagnose_intervention_parent_elements(session)
    await diagnose_intervention_query_contexts(session)


    before_filter = len(records)
    drive_link_count = sum(
        bool(get_field(record.get("fields", {}), "Link"))
        for record in records
        if isinstance(record, dict)
    )
    log(
        "Google Drive Link availability: "
        f"field=fldummc1QJ9vEXOga; "
        f"populated={drive_link_count}/{len(records)} clients."
    )

    await log_interface_field_labels(session)
    # DIAGNOSTIC: find which client fields contain approved staff record IDs.
    # Values and actual record IDs are never printed.
    approved_id_matches_by_field = {}
    unique_approved_ids_by_field = {}

    for record in records:
        fields = record.get("fields", {})

        if not isinstance(fields, dict):
            continue

        for field_id, value in fields.items():
            try:
                serialised = json.dumps(
                    value,
                    ensure_ascii=False,
                    default=str,
                )
            except Exception:
                serialised = str(value)

            found_ids = {
                match.casefold()
                for match in AIRTABLE_RECORD_ID_RE.findall(serialised)
            }

            matched_ids = found_ids.intersection(ALLOWED_STAFF_IDS)

            if matched_ids:
                approved_id_matches_by_field[field_id] = (
                    approved_id_matches_by_field.get(field_id, 0) + 1
                )

                unique_approved_ids_by_field.setdefault(
                    field_id, set()
                ).update(matched_ids)
    # Compare exact linked IDs with lookup-map keys.
    # Do not use other IDs or names as staff identifiers.
    org_direct_clients = 0
    org_map_key_clients = 0
    org_direct_ids = set()
    org_map_key_ids = set()

    for record in records:
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            continue

        value = fields.get("fldIOi1f33xWKzIJC")

        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                value = None

        if not isinstance(value, dict):
            continue

        linked = value.get("linkedRecordIds", [])
        mapped = value.get("valuesByLinkedRecordId", {})

        direct_ids = {
            item.strip().casefold()
            for item in linked
            if isinstance(item, str)
            and AIRTABLE_RECORD_ID_RE.fullmatch(item.strip())
        } if isinstance(linked, list) else set()

        mapped_ids = {
            key.strip().casefold()
            for key in mapped
            if isinstance(key, str)
            and AIRTABLE_RECORD_ID_RE.fullmatch(key.strip())
        } if isinstance(mapped, dict) else set()

        direct_matches = direct_ids.intersection(ALLOWED_STAFF_IDS)
        mapped_matches = mapped_ids.intersection(ALLOWED_STAFF_IDS)

        if direct_matches:
            org_direct_clients += 1
            org_direct_ids.update(direct_matches)

        if mapped_matches:
            org_map_key_clients += 1
            org_map_key_ids.update(mapped_matches)

    log(
        "ORGANISATION LOOKUP EXACT-ID TEST: "
        f"clients matching linkedRecordIds={org_direct_clients}/50; "
        f"distinct approved IDs in linkedRecordIds={len(org_direct_ids)}/16; "
        f"clients matching valuesByLinkedRecordId keys={org_map_key_clients}/50; "
        f"distinct approved IDs in lookup keys={len(org_map_key_ids)}/16."
    )

    log("THE 14 MATCHED APPROVED STAFF RECORD IDs:")

    for index, staff_id in enumerate(sorted(org_direct_ids), start=1):
        log(f"{index:02d}. {staff_id}")
    log("APPROVED STAFF ID FIELD DISCOVERY:")

    for field_id, client_count in sorted(
        approved_id_matches_by_field.items(),
        key=lambda item: -item[1],
    ):
        log(
            f"Candidate field: {field_id} | "
            f"clients with approved staff IDs={client_count}/"
            f"{before_filter} | "
            f"distinct approved IDs found="
            f"{len(unique_approved_ids_by_field[field_id])}"
        )

    if approved_id_matches_by_field:
        await diagnose_client_field_metadata(
            session,
            approved_id_matches_by_field.keys(),
        )
    else:
        log(
            "No approved staff IDs found in the returned client field maps. "
            "Creator metadata may be outside cellValuesByFieldId."
        )

    # =========================================================
    # REFERRAL FIELD ELIGIBILITY CSV DIAGNOSTIC
    # FETCH-ONLY: no Trello operations.
    # =========================================================
    if MCP_FETCH_ONLY:
        import csv

        log("REFERRAL FIELD ELIGIBILITY CSV DIAGNOSTIC:")

        referral_fields = {
            "SCC": "fldmDnlxO1FvsDGLt",
            "CAS": "fldDVSdzb4O9krO2q",
            "CoSS": "fld4YZLlnBaUyYH0d",
            "SOLACE": "fld3yDQ4tclxVr3ma",
            "SAVTE": "fldnaPeZ0dWJXxiSa",
        }

        def diagnostic_value_is_populated(value):
            if value is None or value is False:
                return False

            if isinstance(value, str):
                return value.strip().casefold() not in {
                    "", "none", "null", "[]", "{}"
                }

            if isinstance(value, dict):
                return any(
                    diagnostic_value_is_populated(v)
                    for v in value.values()
                )

            if isinstance(value, (list, tuple, set)):
                return any(
                    diagnostic_value_is_populated(v)
                    for v in value
                )

            return True

        field_counts = {name: 0 for name in referral_fields}
        referral_total = 0
        approved_total = 0
        both_total = 0
        referral_only_total = 0
        approved_only_total = 0

        output_path = Path(
            "/tmp/airtable_referral_field_diagnostic_last50.csv"
        )

        columns = [
            "Airtable_Record_ID",
            "Approved_Organisation_ID",
            "SCC_Referral",
            "SCC_Referral_Display_Value",
            "SCC_Referral_Data_Shape",
            "CAS_Referral",
            "CoSS_Referral",
            "SOLACE_Referral",
            "SAVTE_Referral",
            "Any_Partner_Referral",
            "Populated_Referral_Fields",
        ]

        with output_path.open(
            "w", encoding="utf-8-sig", newline=""
        ) as csv_file:

            writer = csv.DictWriter(
                csv_file,
                fieldnames=columns,
            )
            writer.writeheader()

            for record in records:
                if not isinstance(record, dict):
                    continue

                fields = record.get("fields", {})
                if not isinstance(fields, dict):
                    fields = {}

                linked_ids = extract_approved_organisation_ids(fields)
                approved = bool(
                    linked_ids.intersection(ALLOWED_STAFF_IDS)
                )

                populated = {}

                for name, field_id in referral_fields.items():
                    populated[name] = diagnostic_value_is_populated(
                        fields.get(field_id)
                    )

                    if populated[name]:
                        field_counts[name] += 1

                has_referral = any(populated.values())
                populated_names = [
                    name for name, present in populated.items()
                    if present
                ]

                if approved:
                    approved_total += 1

                if has_referral:
                    referral_total += 1

                if approved and has_referral:
                    both_total += 1
                elif has_referral:
                    referral_only_total += 1
                elif approved:
                    approved_only_total += 1

                scc_raw_value = fields.get(referral_fields["SCC"])

                row = {
                    "Airtable_Record_ID": record.get("id", ""),
                    "Approved_Organisation_ID": "YES" if approved else "NO",
                    "SCC_Referral": "YES" if populated["SCC"] else "NO",
                    "SCC_Referral_Display_Value": get_field(
                        fields, "SCC - Referral MAX^"
                    ),
                    "SCC_Referral_Data_Shape": (
                        "dict:" + ",".join(sorted(scc_raw_value.keys()))
                        if isinstance(scc_raw_value, dict)
                        else (
                            f"list[{len(scc_raw_value)}]"
                            if isinstance(scc_raw_value, list)
                            else (
                                "None"
                                if scc_raw_value is None
                                else type(scc_raw_value).__name__
                            )
                        )
                    ),
                    "CAS_Referral": "YES" if populated["CAS"] else "NO",
                    "CoSS_Referral": "YES" if populated["CoSS"] else "NO",
                    "SOLACE_Referral": "YES" if populated["SOLACE"] else "NO",
                    "SAVTE_Referral": "YES" if populated["SAVTE"] else "NO",
                    "Any_Partner_Referral": "YES" if has_referral else "NO",
                    "Populated_Referral_Fields": ", ".join(populated_names),
                }

                writer.writerow(row)

        output_path.chmod(0o600)

        log(f"Referral field comparison: {len(records)} clients.")

        for name, count in field_counts.items():
            log(f"REFERRAL FIELD {name}: populated={count}/{len(records)}")

        log(
            f"REFERRAL SUMMARY: any referral={referral_total}; "
            f"approved organisation={approved_total}; "
            f"both={both_total}; "
            f"referral only={referral_only_total}; "
            f"approved only={approved_only_total}."
        )

        log(f"CSV created: {output_path}")

    organisation_field_present = 0
    clients_with_linked_ids = 0
    eligible_count = 0
    eligible_with_drive = 0
    eligible_without_drive = 0
    nonapproved_record_ids = []

    for record in records:
        fields = record.get("fields", {}) if isinstance(record, dict) else {}
        if not isinstance(fields, dict):
            continue

        if (
            STAFF_ID_FILTER_FIELD_ID in fields
            or "Organisation completing IA" in fields
        ):
            organisation_field_present += 1

        linked_ids = extract_approved_organisation_ids(fields)

        if linked_ids:
            clients_with_linked_ids += 1

        if linked_ids.intersection(ALLOWED_STAFF_IDS):
            eligible_count += 1
            if get_field(fields, "Link"):
                eligible_with_drive += 1
            else:
                eligible_without_drive += 1
        else:
            record_id = record.get("id")
            if record_id:
                nonapproved_record_ids.append(record_id)

    log(
        f"Eligible clients with Google Drive links: "
        f"{eligible_with_drive}/ {eligible_count}; "
        f"without links: {eligible_without_drive}."
    )

    log(
        "Organisation completing IA eligibility scan: "
        f"field={STAFF_ID_FILTER_FIELD_ID}; "
        f"field present={organisation_field_present}/{before_filter}; "
        f"clients with linked IDs={clients_with_linked_ids}/{before_filter}; "
        f"clients matching approved IDs={eligible_count}/{before_filter}; "
        f"approved IDs configured={len(ALLOWED_STAFF_IDS)}."
    )

    log(
        "NONAPPROVED IDS FROM THE SAME ELIGIBILITY SCAN: "
        f"{len(nonapproved_record_ids)} records."
    )

    for index, record_id in enumerate(nonapproved_record_ids, start=1):
        log(f"NONAPPROVED CLIENT {index}: {record_id}")

    if MCP_FETCH_ONLY:
        scc_eligible_count = sum(
            1
            for record in records
            if isinstance(record, dict)
            and has_scc_referral(record.get("fields", {}))
        )
        log(
            f"FETCH-ONLY mode enabled. Evaluated {before_filter} clients; "
            f"{scc_eligible_count} passed the SCC referral-date filter; "
            "no Trello endpoints were called and no Trello changes were made."
        )
        return

    if not ELIGIBILITY_RULE_CONFIRMED:
        log(
            "LIVE SYNC BLOCKED: actual Airtable creator identity has not "
            "been verified. No Trello endpoints were called.",
            "warning",
        )
        return

    # A live sync must never silently disable or bypass staff-ID filtering.

    # Filter clients before making any Trello API calls that could change cards.
    records = [
        record for record in records
        if has_scc_referral(record.get("fields", {}))
    ]
    log(
        f"SCC referral-date filter retained {len(records)}/{before_filter} Airtable records."
    )

    if not records:
        log("No Airtable records passed the staff allowlist; no Trello changes were made.")
        return

    existing_cards, phone_index = load_existing_cards()
    created = updated = 0

    for record in records:
        try:
            airtable_id = record.get("id")
            fields = record.get("fields", {})

            existing = existing_cards.get(airtable_id)
            if existing and existing.get("archived_stale"):
                log(
                    f"Skipping {airtable_id}: matching Trello card "
                    "is archived and older than the reopen window; "
                    "not creating a duplicate or reopening it."
                )
                continue

            created_at = parse_iso(record.get("createdTime") or datetime.now(timezone.utc).isoformat())
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

            # Defence-in-depth: never write a record whose staff IDs are not approved.
            if not has_scc_referral(fields):
                log(
                    f"⏭ Skipped record {airtable_id}: no valid SCC referral date is recorded",
                    "warning",
                )
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
                # Create with Trello's essential fields only. Optional fields such
                # as due/pos are applied by the common update below after the card
                # exists. This isolates a bad optional field from card creation.
                card = trello_post(
                    "/cards",
                    {
                        "idList": CREATE_LIST_ID,
                        "name": card_name,
                        "desc": f"AIRTABLE_RECORD_ID:\n{airtable_id}\n",
                    }
                )
                card_id = card["id"]
                card.setdefault("desc", f"AIRTABLE_RECORD_ID:\n{airtable_id}\n")
                card.setdefault("idList", CREATE_LIST_ID)
                existing_cards[airtable_id] = {
                    "card": card,
                    "closed": False,
                    "archived_stale": False,
                }
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

                phone = clean_phone(whatsapp_phone or contact_phone)
                if phone:
                    phone_index.setdefault(phone, []).append({
                        **card,
                        "id": card_id,
                        "idList": CREATE_LIST_ID,
                        "closed": False,
                    })

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

            # Optional compatibility with the supplied source script. These
            # destructive cleanups are disabled by default; enable them explicitly
            # only if you want to remove existing checklists/attachments on sync.
            if TRELLO_CLEAR_CHECKLISTS_ON_SYNC:
                try:
                    clean_card(card_id)
                except Exception as cleanup_exc:
                    log(
                        f"Could not clear checklists for Trello card {card_id}: "
                        f"{type(cleanup_exc).__name__}",
                        "warning",
                    )

            if TRELLO_DELETE_ATTACHMENTS_ON_SYNC:
                try:
                    for attachment in trello_get(f"/cards/{card_id}/attachments"):
                        attachment_id = attachment.get("id") if isinstance(attachment, dict) else None
                        if attachment_id:
                            trello_delete(f"/cards/{card_id}/attachments/{attachment_id}")
                except Exception as cleanup_exc:
                    log(
                        f"Could not delete existing attachments for Trello card {card_id}: "
                        f"{type(cleanup_exc).__name__}",
                        "warning",
                    )

            # Update essential card fields first. Apply optional fields separately
            # so one invalid due date/position cannot prevent the name, description,
            # or Airtable marker from being saved.
            trello_put(
                f"/cards/{card_id}",
                {
                    "name": card_name,
                    "desc": desc,
                }
            )

            if eviction_date:
                try:
                    trello_put(
                        f"/cards/{card_id}",
                        {"due": eviction_date.isoformat()},
                    )
                except Exception as due_exc:
                    log(
                        f"Warning: card {card_id} was saved, but Trello rejected "
                        f"the due date for Airtable record {airtable_id}: {due_exc}",
                        "warning",
                    )

            # Automatic Trello position updates disabled:
            # timestamp-derived positions were rejected with HTTP 400.

            # Preserve existing Trello attachments. The sync only adds/updates
            # its managed description and comment; it does not purge user files.

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
    if MCP_FETCH_ONLY:
        log("SAFETY MODE: AIRTABLE_MCP_FETCH_ONLY is enabled; Trello writes are disabled.")
    elif not ELIGIBILITY_RULE_CONFIRMED:
        log(
            "SAFETY LOCK: live Trello writes are blocked until creator "
            "identity is verified.",
            "warning",
        )
    else:
        missing = []
        if not TRELLO_API_KEY:
            missing.append("TRELLO_API_KEY")
        if not TRELLO_API_TOKEN:
            missing.append("TRELLO_API_TOKEN")
        if not CREATE_LIST_ID:
            missing.append("TRELLO_CREATE_LIST_ID")
        if missing:
            raise RuntimeError(
                "Refusing live sync because required environment configuration is missing: "
                + ", ".join(missing)
            )
        log("LIVE MODE: Trello writes are enabled; SCC referral-date filtering and duplicate protection are active.", "warning")

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