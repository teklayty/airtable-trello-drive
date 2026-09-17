from __future__ import annotations

import csv
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from .airtable_source import load_clients
from .config import REPORT_OUTPUT_DIR, ensure_output_dirs
from .excel_report import build_xlsx
from .metrics import build_metrics
from .pdf_report import build_pdf
from .reporting_database import ReportingDatabase


def _safe_sheet_value(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value is not None else ""


def export_csv(metrics: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []

    for label, counter_key in [
        ("Support Need", "support_needs"),
        ("Referral Organisation", "referrals_by_org"),
        ("Housing Status", "current_housing"),
        ("Homelessness Risk", "current_homelessness"),
        ("Housing Provider", "current_housing_providers"),
        ("Eviction Risk", "eviction_risk"),
    ]:
        for category, count in metrics[counter_key].items():
            rows.append([label, category, int(count)])

    rows.extend([
        ["KPI", "People supported", metrics["people_supported"]],
        ["KPI", "New clients", metrics["new_clients"]],
        ["KPI", "Referrals", metrics["referrals"]],
        ["KPI", "Support / engagement events", metrics["support_events"]],
        ["KPI", "Housing changes", metrics["housing_changes"]],
        ["KPI", "WhatsApp messages", metrics["whatsapp"]["total"]],
    ])

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Section", "Category", "Count"])
        writer.writerows(rows)
    return path


def run_report(
    report_kind: str,
    start: datetime,
    end: datetime,
    period_label: str,
    output_dir: Path | None = None,
    generate_xlsx: bool = True,
) -> dict[str, Path]:
    ensure_output_dirs()
    output_dir = output_dir or (REPORT_OUTPUT_DIR / report_kind.lower())
    output_dir.mkdir(parents=True, exist_ok=True)

    clients = load_clients()
    db = ReportingDatabase()
    db.record_clients(clients)
    metrics = build_metrics(db, start, end, clients, period_label)

    stamp = {
        "Weekly": start.strftime("%G-W%V"),
        "Monthly": start.strftime("%Y-%m"),
        "Quarterly": f"{start:%Y}-Q{((start.month - 1) // 3) + 1}",
    }[report_kind]
    base = output_dir / f"SPRING_{report_kind}_Report_{stamp}"

    outputs = {
        "pdf": build_pdf(metrics, base.with_suffix(".pdf"), report_kind),
        "csv": export_csv(metrics, base.with_suffix(".csv")),
    }

    if generate_xlsx:
        try:
            outputs["xlsx"] = build_xlsx(metrics, base.with_suffix(".xlsx"))
        except Exception as exc:
            print(f"WARNING: Excel export skipped: {exc}")

    return outputs
