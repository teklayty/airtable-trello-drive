from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = Path("/home/springvolunteer/Airtable2Trello")
REPORT_DB_PATH = Path(os.getenv("REPORT_DB_PATH", str(RUNTIME_ROOT / "reporting_data" / "reporting.db")))
REPORT_OUTPUT_DIR = Path(os.getenv("REPORT_OUTPUT_DIR", str(RUNTIME_ROOT / "reports")))

AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN", "")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "")
AIRTABLE_TABLE_ID = os.getenv("AIRTABLE_TABLE_ID", "")
AIRTABLE_VIEW_ID = os.getenv("REPORTING_AIRTABLE_VIEW_ID", os.getenv("AIRTABLE_VIEW_ID", ""))

WHATSAPP_DB_PATH = Path(
    os.getenv(
        "WHATSAPP_DB_PATH",
        "/home/springvolunteer/Airtable2Trello/whatsapp_service.db",
    )
)

REPORT_ALLOWED_STAFF = {
    value.strip().lower()
    for value in os.getenv("REPORT_ALLOWED_STAFF", os.getenv("TRELLO_ALLOWED_STAFF", "")).split(",")
    if value.strip()
}

REPORT_HOUSING_PROVIDER_FIELD = os.getenv(
    "REPORT_HOUSING_PROVIDER_FIELD",
    "Housing Provider / Organisation",
)
REPORT_HOUSING_OUTCOME_FIELD = os.getenv(
    "REPORT_HOUSING_OUTCOME_FIELD",
    "Housing Outcome",
)
REPORT_REFERRAL_OUTCOME_FIELD = os.getenv(
    "REPORT_REFERRAL_OUTCOME_FIELD",
    "Referral Outcome",
)
REPORT_OVERALL_OUTCOME_FIELD = os.getenv(
    "REPORT_OVERALL_OUTCOME_FIELD",
    "Overall Case Outcome",
)
REPORT_INCLUDE_PII = os.getenv("REPORT_INCLUDE_PII", "false").strip().lower() in {
    "1", "true", "yes", "y", "on"
}

REPORT_TITLE = os.getenv("REPORT_TITLE", "SPRING Service Reporting")
REPORT_ORGANISATION = os.getenv("REPORT_ORGANISATION", "SPRING")


def require_airtable_config() -> None:
    missing = []
    for name, value in {
        "AIRTABLE_TOKEN": AIRTABLE_TOKEN,
        "AIRTABLE_BASE_ID": AIRTABLE_BASE_ID,
        "AIRTABLE_TABLE_ID": AIRTABLE_TABLE_ID,
    }.items():
        if not value:
            missing.append(name)
    if missing:
        raise RuntimeError(
            "Missing Airtable reporting configuration: " + ", ".join(missing)
        )


def ensure_output_dirs() -> None:
    REPORT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    for name in ("weekly", "monthly", "quarterly"):
        (REPORT_OUTPUT_DIR / name).mkdir(parents=True, exist_ok=True)


def staff_allowed(staff: str, allowed: Iterable[str] | None = None) -> bool:
    rules = REPORT_ALLOWED_STAFF if allowed is None else {
        str(v).strip().lower() for v in allowed if str(v).strip()
    }
    if not rules:
        return True
    return str(staff or "").strip().lower() in rules
