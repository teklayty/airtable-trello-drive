from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dateutil.parser import parse as parse_datetime

from .config import WHATSAPP_DB_PATH


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parse_datetime(value)
    except Exception:
        return None


def _normalise_phone(value: Any) -> str:
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


def _in_period(value: str | None, start: datetime, end: datetime) -> bool:
    dt = _parse(value)
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return start <= dt.astimezone(start.tzinfo) < end


def summarize_whatsapp(start: datetime, end: datetime) -> dict:
    if not WHATSAPP_DB_PATH.exists():
        return {
            "available": False,
            "incoming": 0,
            "outgoing": 0,
            "total": 0,
            "attachments": 0,
        }

    con = sqlite3.connect(WHATSAPP_DB_PATH)
    try:
        messages = con.execute(
            "SELECT direction, whatsapp_timestamp FROM whatsapp_messages "
            "WHERE whatsapp_timestamp IS NOT NULL"
        ).fetchall()
        incoming = 0
        outgoing = 0
        for direction, timestamp in messages:
            if not _in_period(timestamp, start, end):
                continue
            if direction == "incoming":
                incoming += 1
            elif direction == "outgoing":
                outgoing += 1

        attachments = con.execute(
            "SELECT created_at FROM whatsapp_attachments "
            "WHERE created_at IS NOT NULL"
        ).fetchall()
        attachment_count = sum(
            1 for (timestamp,) in attachments if _in_period(timestamp, start, end)
        )

        return {
            "available": True,
            "incoming": incoming,
            "outgoing": outgoing,
            "total": incoming + outgoing,
            "attachments": attachment_count,
        }
    finally:
        con.close()


def collect_whatsapp_events() -> list[dict[str, Any]]:
    """Read individual WhatsApp messages/attachments from the existing service DB."""
    if not WHATSAPP_DB_PATH.exists():
        return []

    con = sqlite3.connect(WHATSAPP_DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        contacts: list[sqlite3.Row] = con.execute(
            "SELECT phone, whatsapp_phone, airtable_id, fullname, trello_card_id FROM contacts"
        ).fetchall()
        contact_by_phone: dict[str, sqlite3.Row] = {}
        for row in contacts:
            for raw in (row["phone"], row["whatsapp_phone"]):
                phone = _normalise_phone(raw)
                if phone and phone not in contact_by_phone:
                    contact_by_phone[phone] = row

        rows = con.execute(
            """
            SELECT id, whatsapp_account, phone, message, direction,
                   whatsapp_timestamp, created_at
            FROM whatsapp_messages
            ORDER BY id
            """
        ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            phone = _normalise_phone(row["phone"])
            contact = contact_by_phone.get(phone)
            event_id = f"whatsapp-message:{row['id']}"
            events.append(
                {
                    "source": "whatsapp",
                    "source_record_id": event_id,
                    "airtable_id": contact["airtable_id"] if contact else None,
                    "card_id": contact["trello_card_id"] if contact else None,
                    "direction": row["direction"] or "unknown",
                    "event_at": _parse(row["whatsapp_timestamp"] or row["created_at"]),
                    "sender": phone if row["direction"] == "incoming" else "",
                    "recipients": phone if row["direction"] == "outgoing" else "",
                    "subject": "",
                    "body_preview": row["message"] or "",
                    "has_attachments": False,
                    "attachment_count": 0,
                    "metadata": {
                        "whatsapp_account": row["whatsapp_account"],
                        "phone": phone,
                        "message_row_id": row["id"],
                    },
                }
            )

        attachment_rows = con.execute(
            """
            SELECT id, whatsapp_account, phone, message_hash, filename,
                   mime_type, drive_file_id, drive_file_url, created_at
            FROM whatsapp_attachments
            ORDER BY id
            """
        ).fetchall()
        # The source DB stores attachment records separately. They are imported as
        # their own events so attachment counts remain historical and auditable.
        for event in events:
            event["has_attachments"] = False
            event["attachment_count"] = 0

        events.extend(
            {
                "source": "whatsapp_attachment",
                "source_record_id": f"whatsapp-attachment:{row['id']}",
                "airtable_id": (
                    contact_by_phone.get(_normalise_phone(row["phone"]))["airtable_id"]
                    if contact_by_phone.get(_normalise_phone(row["phone"]))
                    else None
                ),
                "card_id": (
                    contact_by_phone.get(_normalise_phone(row["phone"]))["trello_card_id"]
                    if contact_by_phone.get(_normalise_phone(row["phone"]))
                    else None
                ),
                "direction": "attachment",
                "event_at": _parse(row["created_at"]),
                "sender": _normalise_phone(row["phone"]),
                "recipients": "",
                "subject": "",
                "body_preview": row["filename"] or "WhatsApp attachment",
                "has_attachments": True,
                "attachment_count": 1,
                "metadata": {
                    "whatsapp_account": row["whatsapp_account"],
                    "phone": _normalise_phone(row["phone"]),
                    "filename": row["filename"],
                    "mime_type": row["mime_type"],
                    "drive_file_id": row["drive_file_id"],
                    "drive_file_url": row["drive_file_url"],
                    "message_hash": row["message_hash"],
                },
            }
            for row in attachment_rows
        )
        return events
    finally:
        con.close()
