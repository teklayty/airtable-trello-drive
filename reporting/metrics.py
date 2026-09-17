from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Iterable

from .reporting_database import ReportingDatabase
from .whatsapp_source import summarize_whatsapp


def _clean_label(value: str, default: str = "Unknown") -> str:
    value = str(value or "").strip()
    return value if value else default


def _within(value: str | None, start: datetime, end: datetime) -> bool:
    if not value:
        return False
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=start.tzinfo)
    return start <= dt < end


def support_category_counts(clients: Iterable[dict]) -> Counter:
    counts = Counter()
    for client in clients:
        issue = _clean_label(client.get("issue"), "Other / not recorded")
        counts[issue] += 1
    return counts


def current_housing_counts(rows) -> Counter:
    return Counter(_clean_label(row["housing_status"]) for row in rows)


def homelessness_counts(rows) -> Counter:
    return Counter(_clean_label(row["homelessness_risk"]) for row in rows)


def provider_counts(rows) -> Counter:
    providers = Counter()
    for row in rows:
        provider = _clean_label(row["housing_provider"], "Not recorded")
        providers[provider] += 1
    return providers


def outcome_counts(rows, field: str) -> Counter:
    return Counter(_clean_label(row[field]) for row in rows)


def eviction_risk_counts(rows, now: datetime) -> Counter:
    counts = Counter()
    for row in rows:
        raw = row["eviction_date"]
        if not raw:
            counts["No eviction date"] += 1
            continue
        try:
            date = datetime.fromisoformat(raw)
        except Exception:
            counts["Unparseable date"] += 1
            continue
        if date.tzinfo is None:
            date = date.replace(tzinfo=now.tzinfo)
        days = (date - now).days
        if days < 0:
            counts["Past eviction date"] += 1
        elif days <= 14:
            counts["Within 14 days"] += 1
        elif days <= 30:
            counts["15-30 days"] += 1
        else:
            counts["More than 30 days"] += 1
    return counts


def _unique_ids_from_events(events) -> set[str]:
    return {row["airtable_id"] for row in events}


def build_metrics(
    db: ReportingDatabase,
    start: datetime,
    end: datetime,
    clients: list[dict],
    period_label: str,
) -> dict:
    referral_events = db.referral_events(start, end)
    support_events = db.support_events(start, end)
    housing_events = db.housing_events(start, end)
    latest = db.latest_clients(end)

    new_clients = 0
    for client in clients:
        created = client.get("created_at")
        if created and start <= created < end:
            new_clients += 1

    supported_ids = _unique_ids_from_events(referral_events) | _unique_ids_from_events(support_events)
    supported_ids |= {
        client["airtable_id"]
        for client in clients
        if client.get("created_at") and start <= client["created_at"] < end
    }

    referrals_by_org = Counter(row["organisation"] for row in referral_events)
    support_by_org = Counter(row["organisation"] for row in support_events)
    housing_outcome_counts = Counter(row["housing_status"] or "Unknown" for row in housing_events)

    client_map = {client["airtable_id"]: client for client in clients}
    supported_clients = [client_map[client_id] for client_id in supported_ids if client_id in client_map]
    current_issues = support_category_counts(supported_clients)
    current_housing = current_housing_counts(latest)
    current_homelessness = homelessness_counts(latest)
    current_providers = provider_counts(latest)
    current_referral_outcomes = outcome_counts(latest, "referral_outcome")
    current_overall_outcomes = outcome_counts(latest, "overall_outcome")
    whatsapp = summarize_whatsapp(start, end)

    latest_payloads = []
    import json
    for row in latest:
        try:
            latest_payloads.append(json.loads(row["payload_json"] or "{}"))
        except Exception:
            latest_payloads.append({})

    total_latest = len(latest)
    drive_present = sum(1 for payload in latest_payloads if str(payload.get("Link") or "").strip())
    housing_provider_present = sum(1 for row in latest if str(row["housing_provider"] or "").strip())
    housing_outcome_present = sum(1 for row in latest if str(row["housing_outcome"] or "").strip())
    referral_outcome_present = sum(1 for row in latest if str(row["referral_outcome"] or "").strip())
    overall_outcome_present = sum(1 for row in latest if str(row["overall_outcome"] or "").strip())
    data_quality = {
        "records in current reporting population": total_latest,
        "Drive link recorded": drive_present,
        "Housing provider recorded": housing_provider_present,
        "Housing outcome recorded": housing_outcome_present,
        "Referral outcome recorded": referral_outcome_present,
        "Overall case outcome recorded": overall_outcome_present,
    }

    return {
        "period_label": period_label,
        "start": start,
        "end": end,
        "people_supported": len(supported_ids),
        "new_clients": new_clients,
        "referrals": len(referral_events),
        "support_events": len(support_events),
        "housing_changes": len(housing_events),
        "referral_people": len(_unique_ids_from_events(referral_events)),
        "support_people": len(_unique_ids_from_events(support_events)),
        "support_needs": current_issues,
        "referrals_by_org": referrals_by_org,
        "support_by_org": support_by_org,
        "housing_changes_by_status": housing_outcome_counts,
        "current_housing": current_housing,
        "current_homelessness": current_homelessness,
        "current_housing_providers": current_providers,
        "current_referral_outcomes": current_referral_outcomes,
        "current_overall_outcomes": current_overall_outcomes,
        "eviction_risk": eviction_risk_counts(latest, datetime.now().astimezone()),
        "whatsapp": whatsapp,
        "data_quality": data_quality,
        "latest_clients": latest,
        "referral_events": referral_events,
        "support_events_detail": support_events,
        "housing_events_detail": housing_events,
    }


def week_boundaries(reference: datetime | None = None) -> tuple[datetime, datetime, str]:
    reference = reference or datetime.now().astimezone()
    monday = (reference - timedelta(days=reference.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = monday - timedelta(days=7)
    end = monday
    iso_week = start.isocalendar().week
    return start, end, f"Week {iso_week} ({start:%d %b}-{(end - timedelta(days=1)):%d %b %Y})"


def month_boundaries(reference: datetime | None = None) -> tuple[datetime, datetime, str]:
    reference = reference or datetime.now().astimezone()
    first = reference.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_month_end = first - timedelta(seconds=1)
    start = previous_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = first
    return start, end, previous_month_end.strftime("%B %Y")


def quarter_boundaries(reference: datetime | None = None) -> tuple[datetime, datetime, str]:
    reference = reference or datetime.now().astimezone()
    current_quarter = (reference.month - 1) // 3
    current_start_month = current_quarter * 3 + 1
    current_start = reference.replace(
        month=current_start_month, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    previous_end = current_start - timedelta(seconds=1)
    previous_quarter = (previous_end.month - 1) // 3 + 1
    start_month = (previous_quarter - 1) * 3 + 1
    start = previous_end.replace(month=start_month, day=1, hour=0, minute=0, second=0, microsecond=0)
    end = current_start
    return start, end, f"Q{previous_quarter} {previous_end.year}"
