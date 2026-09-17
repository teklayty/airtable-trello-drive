from __future__ import annotations

import sqlite3
from datetime import datetime
from datetime import timezone
from pathlib import Path

from dateutil.parser import parse as parse_datetime

from .config import WHATSAPP_DB_PATH


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parse_datetime(value)
    except Exception:
        return None


def summarize_whatsapp(start: datetime, end: datetime) -> dict:
    if not WHATSAPP_DB_PATH.exists():
        return {
            "available": False,
            "incoming": 0,
            "outgoing": 0,
            "total": 0,
            "attachments": 0,
        }

    def in_period(value: str | None) -> bool:
        dt = _parse(value)
        if dt is None:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return start <= dt.astimezone(start.tzinfo) < end

    con = sqlite3.connect(WHATSAPP_DB_PATH)
    try:
        messages = con.execute(
            "SELECT direction, whatsapp_timestamp FROM whatsapp_messages "
            "WHERE whatsapp_timestamp IS NOT NULL"
        ).fetchall()
        incoming = 0
        outgoing = 0
        for direction, timestamp in messages:
            if not in_period(timestamp):
                continue
            if direction == "incoming":
                incoming += 1
            elif direction == "outgoing":
                outgoing += 1

        attachments = con.execute(
            "SELECT created_at FROM whatsapp_attachments "
            "WHERE created_at IS NOT NULL"
        ).fetchall()
        attachment_count = sum(1 for (timestamp,) in attachments if in_period(timestamp))

        return {
            "available": True,
            "incoming": incoming,
            "outgoing": outgoing,
            "total": incoming + outgoing,
            "attachments": attachment_count,
        }
    finally:
        con.close()
