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
