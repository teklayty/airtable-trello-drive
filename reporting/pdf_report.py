from __future__ import annotations

from io import BytesIO
from pathlib import Path
from datetime import timedelta

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .charts import bar_chart, donut_chart, funnel_chart
from .config import REPORT_ORGANISATION, REPORT_TITLE


def _flowable_image(png: bytes, width: float = 175 * mm, height: float = 100 * mm):
    return Image(BytesIO(png), width=width, height=height)


def _card_table(metrics: dict):
    cards = [
        ("People supported", metrics["people_supported"]),
        ("New clients", metrics["new_clients"]),
        ("Referrals", metrics["referrals"]),
        ("Support / engagement events", metrics["support_events"]),
        ("Housing changes", metrics["housing_changes"]),
        ("WhatsApp messages", metrics["whatsapp"]["total"]),
    ]
    data = [cards[:3], cards[3:]]
    table = Table(
        [[Paragraph(f"<b>{label}</b><br/><font size='20'>{value}</font>", ParagraphStyle("card", alignment=TA_CENTER, leading=24)) for label, value in row] for row in data],
        colWidths=[58 * mm] * 3,
        rowHeights=[30 * mm] * 2,
        hAlign="CENTER",
    )
    table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D5DD")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D0D5DD")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
    ]))
    return table


def _counter_table(counter, heading: str, max_rows: int = 15):
    rows = [["Category", "Count"]]
    items = sorted(counter.items(), key=lambda item: item[1], reverse=True)[:max_rows]
    if not items:
        items = [("No data", 0)]
    rows.extend([[str(k), int(v)] for k, v in items])
    table = Table(rows, colWidths=[140 * mm, 25 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (-1, 1), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D0D5DD")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return [Paragraph(f"<b>{heading}</b>", ParagraphStyle("h", fontSize=12, leading=15)), Spacer(1, 3 * mm), table]


def build_pdf(metrics: dict, output_path: Path, report_kind: str) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "TitleCustom", parent=styles["Title"], fontSize=22, leading=26, alignment=TA_LEFT, spaceAfter=5 * mm
    )
    subtitle = ParagraphStyle(
        "Subtitle", parent=styles["Normal"], fontSize=10, textColor=colors.HexColor("#667085"), spaceAfter=6 * mm
    )
    h2 = ParagraphStyle("H2Custom", parent=styles["Heading2"], fontSize=15, leading=19, spaceBefore=4 * mm, spaceAfter=3 * mm)
    small = ParagraphStyle("Small", parent=styles["Normal"], fontSize=8.5, leading=11)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"{REPORT_ORGANISATION} {report_kind} Report - {metrics['period_label']}",
        author=REPORT_ORGANISATION,
    )

    story = [
        Paragraph(f"{REPORT_TITLE} - {report_kind.title()} Report", title),
        Paragraph(
            f"{metrics['period_label']} | {metrics['start']:%d %b %Y} to {(metrics['end'] - timedelta(days=1)):%d %b %Y}",
            subtitle,
        ),
        _card_table(metrics),
        Spacer(1, 7 * mm),
    ]

    story.extend(_counter_table(metrics["support_needs"], "Support needs / main issues"))
    story.append(_flowable_image(bar_chart(dict(metrics["support_needs"]), "Support needs"), 175 * mm, 98 * mm))
    story.append(PageBreak())

    story.append(Paragraph("Referral activity", h2))
    story.extend(_counter_table(metrics["referrals_by_org"], "Referrals by organisation"))
    story.append(_flowable_image(donut_chart(dict(metrics["referrals_by_org"]), "Referral mix"), 150 * mm, 120 * mm))

    story.append(PageBreak())
    story.append(Paragraph("Client journey", h2))
    funnel = {
        "People supported": metrics["people_supported"],
        "People referred": metrics["referral_people"],
        "People with support / engagement event": metrics["support_people"],
        "Housing changes": metrics["housing_changes"],
    }
    story.append(_flowable_image(funnel_chart(funnel, "Service activity funnel"), 175 * mm, 98 * mm))
    story.append(PageBreak())

    story.append(Paragraph("Housing and homelessness", h2))
    story.extend(_counter_table(metrics["current_housing"], "Current housing picture"))
    story.extend(_counter_table(metrics["current_homelessness"], "Current homelessness-risk field values"))
    story.append(_flowable_image(bar_chart(dict(metrics["current_housing"]), "Current housing picture"), 175 * mm, 98 * mm))

    story.append(Paragraph("Housing providers", h2))
    provider_note = (
        "Provider counts are only meaningful when the configured housing-provider field is populated. "
        "The default expected field is 'Housing Provider / Organisation'."
    )
    story.append(Paragraph(provider_note, small))
    story.extend(_counter_table(metrics["current_housing_providers"], "Housing provider / organisation"))

    story.append(PageBreak())
    story.append(Paragraph("Eviction risk", h2))
    story.extend(_counter_table(metrics["eviction_risk"], "Current eviction-date risk"))
    story.append(_flowable_image(donut_chart(dict(metrics["eviction_risk"]), "Eviction risk picture"), 150 * mm, 120 * mm))

    story.append(Paragraph("WhatsApp activity", h2))
    wa = metrics["whatsapp"]
    story.extend(_counter_table({
        "Incoming": wa["incoming"],
        "Outgoing": wa["outgoing"],
        "Attachments": wa["attachments"],
    }, "WhatsApp activity"))

    story.append(Paragraph("Data quality", h2))
    quality_rows = [["Measure", "Count", "Coverage"]]
    population = metrics["data_quality"].get("records in current reporting population", 0)
    for label, count in metrics["data_quality"].items():
        if label == "records in current reporting population":
            continue
        pct = f"{(count / population * 100):.0f}%" if population else "n/a"
        quality_rows.append([label, int(count), pct])
    quality_table = Table(quality_rows, colWidths=[120 * mm, 25 * mm, 30 * mm], repeatRows=1)
    quality_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D0D5DD")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
    ]))
    story.append(quality_table)
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(
        "Historical trend reporting depends on regular snapshots. Run this reporting collector daily to preserve "
        "change history. Housing-provider, referral-outcome and overall-case-outcome metrics remain blank/unknown "
        "until their fields are populated in Airtable.",
        small,
    ))

    def footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#667085"))
        canvas.drawString(15 * mm, 8 * mm, f"{REPORT_ORGANISATION} · {report_kind.title()} Report")
        canvas.drawRightString(195 * mm, 8 * mm, f"Page {doc_obj.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output_path
