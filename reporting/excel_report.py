from __future__ import annotations

from pathlib import Path


def build_xlsx(metrics: dict, output_path: Path) -> Path:
    """Create a compact reporting workbook using artifact_tool."""
    try:
        from artifact_tool import SpreadsheetFile, Workbook
    except Exception as exc:
        raise RuntimeError(
            "artifact_tool is required for Excel export. Install/enable artifact_tool or use the CSV outputs."
        ) from exc

    wb = Workbook.create()
    dashboard = wb.worksheets.add("Dashboard")
    support = wb.worksheets.add("Support Needs")
    referrals = wb.worksheets.add("Referrals")
    housing = wb.worksheets.add("Housing")
    whatsapp = wb.worksheets.add("WhatsApp")

    dashboard.get_range("A1:B1").values = [["SPRING Reporting", metrics["period_label"]]]
    dashboard.get_range("A3:B8").values = [
        ["People supported", metrics["people_supported"]],
        ["New clients", metrics["new_clients"]],
        ["Referrals", metrics["referrals"]],
        ["Support / engagement events", metrics["support_events"]],
        ["Housing changes", metrics["housing_changes"]],
        ["WhatsApp messages", metrics["whatsapp"]["total"]],
    ]
    dashboard.get_range("A1:B1").format = {
        "fill": "#1F2937",
        "font": {"bold": True, "color": "#FFFFFF", "size": 14},
        "horizontal_alignment": "center",
    }
    dashboard.get_range("A3:B8").format.borders = {
        "items": [
            {"color": "#D0D5DD", "style": "continuous", "weight": "thin"}
        ]
    }

    def write_counter(sheet, counter):
        rows = [["Category", "Count"]] + [[str(k), int(v)] for k, v in sorted(counter.items(), key=lambda x: x[1], reverse=True)]
        sheet.get_range("A1:B1").values = [rows[0]]
        if len(rows) > 1:
            sheet.get_range(f"A2:B{len(rows)}").values = rows[1:]
        sheet.get_range("A1:B1").format = {
            "fill": "#1F2937",
            "font": {"bold": True, "color": "#FFFFFF"},
        }
        sheet.get_range(f"A1:B{max(2, len(rows))}").format.wrap_text = True
        sheet.get_range("A:B").format.column_width = 28

    write_counter(support, metrics["support_needs"])
    write_counter(referrals, metrics["referrals_by_org"])
    write_counter(housing, metrics["current_housing"])
    write_counter(whatsapp, {
        "Incoming": metrics["whatsapp"]["incoming"],
        "Outgoing": metrics["whatsapp"]["outgoing"],
        "Attachments": metrics["whatsapp"]["attachments"],
    })

    dashboard.get_range("D3:E9").values = [
        ["Reporting notes", ""],
        ["Historical snapshots stored", "Yes"],
        ["Housing provider field", "Configurable"],
        ["Referral outcome field", "Configurable"],
        ["PII included", "No"],
        ["Drive link coverage", f"{(metrics['data_quality'].get('Drive link recorded', 0) / max(1, metrics['data_quality'].get('records in current reporting population', 0)) * 100):.0f}%"],
        ["Housing provider coverage", f"{(metrics['data_quality'].get('Housing provider recorded', 0) / max(1, metrics['data_quality'].get('records in current reporting population', 0)) * 100):.0f}%"],
    ]
    dashboard.get_range("D3:E3").format = {
        "fill": "#E5E7EB",
        "font": {"bold": True},
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    SpreadsheetFile.export_xlsx(wb).save(str(output_path))
    return output_path
