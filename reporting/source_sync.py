from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .config import TRELLO_API_KEY, TRELLO_API_TOKEN, TRELLO_BOARD_ID, REPORT_SYNC_EXTERNAL_SOURCES
from .reporting_database import ReportingDatabase
from .trello_source import TrelloClient, classify_trello_comment, normalize_person_name, parse_outlook_comment
from .whatsapp_source import collect_whatsapp_events

logger = logging.getLogger(__name__)


def _normalise_client_name(value: Any) -> str:
    return normalize_person_name(value)


def _normalise_client_phone(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("07") and len(digits) == 11:
        digits = "44" + digits[1:]
    elif digits.startswith("7") and len(digits) == 10:
        digits = "44" + digits
    elif digits.startswith("0") and len(digits) == 11:
        digits = "44" + digits[1:]
    return digits if 8 <= len(digits) <= 15 else ""


def _build_client_indexes(clients: list[dict]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    by_phone: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for client in clients:
        phone = _normalise_client_phone(client.get("phone"))
        name = _normalise_client_name(client.get("fullname"))
        if phone:
            by_phone.setdefault(phone, []).append(client)
        if name:
            by_name.setdefault(name, []).append(client)
    return by_phone, by_name


def _match_card_to_client(card: dict, by_phone: dict[str, list[dict]], by_name: dict[str, list[dict]]) -> tuple[str | None, str, float]:
    for raw_phone in (card.get("phone"), card.get("whatsapp_phone")):
        phone = _normalise_client_phone(raw_phone)
        if not phone:
            continue
        candidates = by_phone.get(phone, [])
        if len(candidates) == 1:
            return candidates[0]["airtable_id"], "exact_phone", 1.0
        if len(candidates) > 1:
            return None, "ambiguous_phone", 0.0

    name = _normalise_client_name(card.get("fullname"))
    candidates = by_name.get(name, [])
    if len(candidates) == 1:
        return candidates[0]["airtable_id"], "exact_full_name", 0.95
    if len(candidates) > 1:
        return None, "ambiguous_full_name", 0.0
    return None, "unmatched", 0.0


def sync_whatsapp(db: ReportingDatabase) -> dict[str, int]:
    sync_id = db.begin_sync("whatsapp")
    events = collect_whatsapp_events()
    stored = 0
    try:
        for event in events:
            # Attachment records are intentionally retained as separate source events,
            # but are not counted as messages by the metrics layer.
            db.upsert_communication_event(event)
            stored += 1
        db.finish_sync(sync_id, len(events), stored, "ok")
        return {"records_seen": len(events), "records_stored": stored}
    except Exception as exc:
        logger.exception("WhatsApp reporting sync failed")
        db.finish_sync(sync_id, len(events), stored, "error", str(exc))
        raise


def sync_trello(db: ReportingDatabase, clients: list[dict]) -> dict[str, int]:
    if not (TRELLO_API_KEY and TRELLO_API_TOKEN and TRELLO_BOARD_ID):
        logger.warning(
            "Trello reporting sync skipped: set TRELLO_API_KEY, TRELLO_API_TOKEN and TRELLO_BOARD_ID."
        )
        return {"records_seen": 0, "records_stored": 0, "skipped": 1}

    sync_id = db.begin_sync("trello")
    client = TrelloClient()
    by_phone, by_name = _build_client_indexes(clients)
    cards = []
    stored = 0
    try:
        cards = client.list_cards()
        for raw_card in cards:
            card = client.parse_card_metadata(raw_card)
            airtable_id, method, confidence = _match_card_to_client(card, by_phone, by_name)
            card["airtable_id"] = airtable_id
            db.upsert_trello_card(card)
            db.upsert_client_link(
                "trello_card",
                card["card_id"],
                airtable_id,
                method,
                confidence,
            )
            stored += 1

            comments = client.list_card_comments(card["card_id"])
            for action in comments:
                text = ((action.get("data") or {}).get("text") or "").strip()
                if not text:
                    continue
                comment_date = None
                if action.get("date"):
                    raw_date = str(action["date"]).replace("Z", "+00:00")
                    try:
                        comment_date = datetime.fromisoformat(raw_date)
                    except ValueError:
                        comment_date = None

                classification = classify_trello_comment(text)
                outlook = parse_outlook_comment(text) if classification == "outlook" else None
                outlook_message_id = outlook.get("message_id") if outlook else None
                db.upsert_trello_comment(
                    action_id=str(action.get("id") or db._key(card["card_id"], comment_date, text)),
                    card_id=card["card_id"],
                    airtable_id=airtable_id,
                    comment_date=comment_date,
                    text=text,
                    source_classification=classification,
                    outlook_message_id=outlook_message_id,
                )

                if outlook:
                    event_source_id = outlook_message_id or str(action.get("id"))
                    db.upsert_communication_event(
                        {
                            "source": "outlook",
                            "source_record_id": event_source_id,
                            "airtable_id": airtable_id,
                            "card_id": card["card_id"],
                            "direction": outlook["direction"],
                            "event_at": comment_date or outlook.get("event_date"),
                            "sender": outlook.get("person", "") if outlook["direction"] == "incoming" else "",
                            "recipients": outlook.get("person", "") if outlook["direction"] == "outgoing" else "",
                            "subject": outlook.get("subject", ""),
                            "body_preview": "",
                            "has_attachments": False,
                            "attachment_count": 0,
                            "metadata": {
                                "trello_card_id": card["card_id"],
                                "trello_action_id": action.get("id"),
                                "view_url": outlook.get("view_url", ""),
                                "comment_event_date": comment_date,
                            },
                        }
                    )

        db.finish_sync(sync_id, len(cards), stored, "ok")
        return {"records_seen": len(cards), "records_stored": stored}
    except Exception as exc:
        logger.exception("Trello reporting sync failed")
        db.finish_sync(sync_id, len(cards), stored, "error", str(exc))
        raise


def sync_external_sources(db: ReportingDatabase, clients: list[dict]) -> dict[str, dict[str, int]]:
    if not REPORT_SYNC_EXTERNAL_SOURCES:
        return {}

    results: dict[str, dict[str, int]] = {}
    results["whatsapp"] = sync_whatsapp(db)
    try:
        results["trello"] = sync_trello(db, clients)
    except Exception as exc:
        # External reporting should not make an otherwise-valid Airtable report fail.
        logger.exception("Trello sync failed; continuing report generation: %s", exc)
        results["trello"] = {"records_seen": 0, "records_stored": 0, "failed": 1}
    return results
