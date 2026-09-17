from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from dateutil.parser import parse as parse_datetime

from .config import (
    AIRTABLE_BASE_ID,
    AIRTABLE_TABLE_ID,
    AIRTABLE_TOKEN,
    AIRTABLE_VIEW_ID,
    REPORT_HOUSING_OUTCOME_FIELD,
    REPORT_HOUSING_PROVIDER_FIELD,
    REPORT_OVERALL_OUTCOME_FIELD,
    REPORT_REFERRAL_OUTCOME_FIELD,
    REPORT_ALLOWED_STAFF,
    require_airtable_config,
    staff_allowed,
)


API_ROOT = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}"
HEADERS = {"Authorization": f"Bearer {AIRTABLE_TOKEN}"}

REFERRAL_FIELDS = {
    "CoSS": ["CoSS Referral Requested (date).", "CoSS - Referral requested (date)."],
    "CAS": ["CAS - Referral requested (date)."],
    "New Beginnings": ["NB - Referral requested (date)."],
    "SAVTE": ["SAVTE - Referral requested (date)."],
    "SCC": ["SCC - Referral requested (date)."],
    "SOLACE": ["SOLACE - Referral requested (date)."],
    "Ukraine Therapeutic Project": ["Ukraine Therapeutic Project referral date"],
}

SUPPORT_DATE_FIELDS = [
    ("CoSS", "CoSS - Date support provided"),
    ("CoSS", "CoSS - Date support provided (Q3)^"),
    ("CAS", "CAS Contact with Client (Phone/Email) date (2026)"),
    ("CAS", "CAS Client Attends Support Meeting (date)"),
    ("SAVTE", "SAVTE - Client contacted (date)"),
    ("SAVTE", "SAVTE Client attends support meeting (date)"),
    ("SCC", "SCC Client attends support meeting (date)"),
    ("SOLACE", "SOLACE Client attends support meeting (date)"),
]


def first_nonempty(fields: dict[str, Any], names: list[str]) -> str:
    for name in names:
        value = fields.get(name)
        if value not in (None, "", [], {}):
            return value_to_text(value)
    return ""


def value_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(value_to_text(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items())
    return str(value).strip()


def _normalise_datetime(dt: datetime) -> datetime:
    """Return a safe UTC-aware datetime.

    Airtable can return date/time strings that dateutil parses with unusual or
    malformed tzinfo objects. Calling ``isoformat()`` on such an object can
    raise ``ValueError: offset must be a timedelta strictly between ...``.
    Reporting stores all parsed datetimes as valid UTC-aware values so both
    SQLite serialisation and period comparisons are deterministic.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    try:
        offset = dt.utcoffset()
        if offset is not None and -timedelta(hours=24) < offset < timedelta(hours=24):
            return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass

    # Defensive fallback for malformed timezone information. Keep the parsed
    # clock/date components and attach UTC rather than propagating a bad tzinfo.
    return dt.replace(tzinfo=timezone.utc)


def parse_date(value: Any) -> datetime | None:
    text = value_to_text(value)
    if not text:
        return None
    try:
        return _normalise_datetime(parse_datetime(text, dayfirst=True, fuzzy=True))
    except Exception:
        for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", value_to_text(value))
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("44"):
        digits = "0" + digits[2:]
    return digits


def _yes(value: Any) -> bool:
    text = value_to_text(value).lower()
    return text in {"yes", "y", "true", "1", "checked", "complete", "completed"}


def _contains_any(value: Any, terms: tuple[str, ...]) -> bool:
    text = value_to_text(value).lower()
    return any(term in text for term in terms)


def infer_housing_status(fields: dict[str, Any]) -> str:
    direct = value_to_text(fields.get(REPORT_HOUSING_OUTCOME_FIELD))
    if direct:
        return direct

    secure_a = value_to_text(fields.get("Secure tenancy (12months or more)"))
    secure_b = value_to_text(fields.get("(If changed) Secure tenancy? (12 Months or more)"))
    tenancy = value_to_text(fields.get("What type of tenancy?"))
    risk = value_to_text(fields.get("Risk of Homelessness.")) or value_to_text(
        fields.get("Risk of Homelessness.^")
    )

    if _contains_any(secure_b or secure_a, ("yes", "secure")):
        return "Secure tenancy"
    if _contains_any(tenancy, ("temporary", "emergency", "hostel")):
        return "Temporary accommodation"
    if _contains_any(tenancy, ("social", "council", "housing association")):
        return "Social housing"
    if _contains_any(tenancy, ("private", "prs", "private rented")):
        return "Private rented"
    if _contains_any(tenancy, ("supported",)):
        return "Supported accommodation"
    if _contains_any(risk, ("homeless", "homelessness")):
        return "At risk / homeless"
    if tenancy:
        return tenancy
    if risk:
        return risk
    return "Unknown"


def infer_homelessness_risk(fields: dict[str, Any]) -> str:
    return value_to_text(
        fields.get("Risk of Homelessness.^")
        or fields.get("Risk of Homelessness.")
    )


def get_fields(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("fields", {}) or {}


def fetch_all_records() -> list[dict[str, Any]]:
    require_airtable_config()
    records: list[dict[str, Any]] = []
    offset = None

    while True:
        params: dict[str, Any] = {"pageSize": 100}
        if AIRTABLE_VIEW_ID:
            params["view"] = AIRTABLE_VIEW_ID
        if offset:
            params["offset"] = offset

        response = requests.get(API_ROOT, headers=HEADERS, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        records.extend(payload.get("records", []))
        offset = payload.get("offset")
        if not offset:
            break

    return records


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    fields = get_fields(record)
    first = first_nonempty(fields, ["Forename/First name(s)", "Forename"])
    last = first_nonempty(fields, ["Surname/Last Name(s)", "Surname"])
    fullname = " ".join(x for x in (first, last) if x).strip()
    staff = first_nonempty(fields, ["(IA) Name of staff/volunteer", "CoSS Staff/Volunteer Name"])

    created_at = parse_date(record.get("createdTime")) or parse_date(fields.get("Created"))
    last_modified = parse_date(fields.get("Last modified"))
    eviction_date = parse_date(fields.get("MEARS Eviction Date"))
    referral_requested = parse_date(
        first_nonempty(fields, [
            "SCC - Referral requested (date).",
            "CoSS Referral Requested (date).",
            "CAS - Referral requested (date).",
        ])
    )

    return {
        "airtable_id": record.get("id", ""),
        "fullname": fullname,
        "staff": staff,
        "created_at": created_at,
        "last_modified": last_modified,
        "phone": normalize_phone(first_nonempty(fields, ["WhatsApp number", "Contact number"])),
        "issue": first_nonempty(fields, ["SPRING - Issue^"] ,),
        "support_provided": first_nonempty(fields, ["CoSS Support Provided (2026)", "CoSS Support Provided"]),
        "supporting_documents": first_nonempty(fields, ["Supporting Documents attached"]),
        "homelessness_risk": infer_homelessness_risk(fields),
        "eviction_date": eviction_date,
        "housing_status": infer_housing_status(fields),
        "housing_provider": first_nonempty(fields, [REPORT_HOUSING_PROVIDER_FIELD]),
        "housing_outcome": first_nonempty(fields, [REPORT_HOUSING_OUTCOME_FIELD]),
        "referral_outcome": first_nonempty(fields, [REPORT_REFERRAL_OUTCOME_FIELD]),
        "overall_outcome": first_nonempty(fields, [REPORT_OVERALL_OUTCOME_FIELD]),
        "housing_change_date": parse_date(fields.get("Date of change in housing situation")),
        "english_level": first_nonempty(fields, ["English Level"]),
        "listening_level": first_nonempty(fields, ["Listening level"]),
        "speaking_level": first_nonempty(fields, ["Speaking level"]),
        "reading_level": first_nonempty(fields, ["Reading level"]),
        "writing_level": first_nonempty(fields, ["Writing level"]),
        "gender": first_nonempty(fields, ["Gender"]),
        "country_of_origin": first_nonempty(fields, ["Country of Origin"]),
        "nationality": first_nonempty(fields, ["Nationality"]),
        "town": first_nonempty(fields, ["Town"]),
        "employment_status_initial": first_nonempty(fields, [
            "NB is the client working, unemployed, a student etc when you first meet them? (from Interventions)"
        ]),
        "employment_outcome": first_nonempty(fields, [
            "NB Following your support, did the client find work, become a student, remain unemployed etc?"
        ]),
        "secure_tenancy": first_nonempty(fields, [
            "(If changed) Secure tenancy? (12 Months or more)",
            "Secure tenancy (12months or more)",
        ]),
        "referral_requested_date": referral_requested,
        "raw_fields": fields,
    }


def load_clients() -> list[dict[str, Any]]:
    clients = []
    for record in fetch_all_records():
        client = normalize_record(record)
        if not client["airtable_id"]:
            continue
        if not staff_allowed(client["staff"], REPORT_ALLOWED_STAFF):
            continue
        clients.append(client)
    return clients


def extract_referral_events(client: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    fields = client["raw_fields"]
    for organisation, candidates in REFERRAL_FIELDS.items():
        for field_name in candidates:
            if field_name not in fields:
                continue
            dt = parse_date(fields.get(field_name))
            if not dt:
                continue
            events.append({
                "airtable_id": client["airtable_id"],
                "organisation": organisation,
                "event_date": dt,
                "source_field": field_name,
                "value": value_to_text(fields.get(field_name)),
            })
            break
    return events


def extract_support_events(client: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    fields = client["raw_fields"]
    for organisation, field_name in SUPPORT_DATE_FIELDS:
        if field_name not in fields:
            continue
        dt = parse_date(fields.get(field_name))
        if not dt:
            continue
        events.append({
            "airtable_id": client["airtable_id"],
            "organisation": organisation,
            "event_type": "support / engagement",
            "event_date": dt,
            "source_field": field_name,
            "value": value_to_text(fields.get(field_name)),
        })
    return events
