from datetime import datetime, timezone

from reporting.metrics import quarter_boundaries, month_boundaries, week_boundaries
from reporting.airtable_source import parse_date


def test_week_boundaries_are_previous_complete_week():
    ref = datetime(2026, 9, 17, 15, tzinfo=timezone.utc)
    start, end, label = week_boundaries(ref)
    assert start.weekday() == 0
    assert end.weekday() == 0
    assert end > start
    assert "Week" in label


def test_month_boundaries_are_previous_month():
    ref = datetime(2026, 9, 17, 15, tzinfo=timezone.utc)
    start, end, label = month_boundaries(ref)
    assert start.month == 8
    assert end.month == 9
    assert "August 2026" == label


def test_quarter_boundaries_are_previous_quarter():
    ref = datetime(2026, 9, 17, 15, tzinfo=timezone.utc)
    start, end, label = quarter_boundaries(ref)
    assert start.month == 4
    assert end.month == 7
    assert label == "Q2 2026"


def test_parse_date_normalises_timezone():
    parsed = parse_date("2026-09-17T14:30:00+01:00")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def test_parse_date_date_only_is_utc_aware():
    parsed = parse_date("17/09/2026")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)


from reporting.reporting_database import ReportingDatabase
from reporting.trello_source import classify_trello_comment, parse_card_identity, parse_outlook_comment


def test_trello_card_identity_prefers_phone_suffix():
    fullname, phone = parse_card_identity("John Walter – Phone: 07990123456")
    assert fullname == "John Walter"
    assert phone == "447990123456"


def test_outlook_comment_parser_extracts_direction_and_id():
    message_id = "abc123=="
    text = (
        "📧⬆️ **Outgoing Outlook** (06-10-2026)\n\n"
        "To: **john@example.com**\n\n"
        "Subject: **Fw: John Walter**\n\n"
        f"[View message](https://outlook.office365.com/owa/?ItemID=abc123%3D%3D&exvsurl=1&viewmodel=ReadMessageItem)"
    )
    parsed = parse_outlook_comment(text)
    assert parsed is not None
    assert parsed["direction"] == "outgoing"
    assert parsed["person"] == "john@example.com"
    assert parsed["subject"] == "Fw: John Walter"
    assert parsed["message_id"] == message_id
    assert classify_trello_comment(text) == "outlook"


def test_reporting_database_communication_event_is_idempotent(tmp_path):
    db = ReportingDatabase(tmp_path / "reporting.db")
    event = {
        "source": "outlook",
        "source_record_id": "msg-1",
        "airtable_id": None,
        "direction": "incoming",
        "event_at": datetime(2026, 9, 5, 10, tzinfo=timezone.utc),
        "body_preview": "hello",
    }
    db.upsert_communication_event(event)
    event["airtable_id"] = "rec123"
    db.upsert_communication_event(event)
    rows = db.communication_events(
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    assert len(rows) == 1
    assert rows[0]["airtable_id"] == "rec123"
