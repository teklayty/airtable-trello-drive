from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .airtable_source import extract_referral_events, extract_support_events
from .config import REPORT_DB_PATH


class ReportingDatabase:
    def __init__(self, path: Path | str = REPORT_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _initialise(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS client_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    airtable_id TEXT NOT NULL,
                    fullname TEXT,
                    staff TEXT,
                    created_at TEXT,
                    last_modified TEXT,
                    issue TEXT,
                    homelessness_risk TEXT,
                    eviction_date TEXT,
                    housing_status TEXT,
                    housing_provider TEXT,
                    housing_outcome TEXT,
                    referral_outcome TEXT,
                    overall_outcome TEXT,
                    housing_change_date TEXT,
                    english_level TEXT,
                    listening_level TEXT,
                    speaking_level TEXT,
                    reading_level TEXT,
                    writing_level TEXT,
                    country_of_origin TEXT,
                    nationality TEXT,
                    gender TEXT,
                    town TEXT,
                    employment_status_initial TEXT,
                    employment_outcome TEXT,
                    secure_tenancy TEXT,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_snapshot_client
                    ON client_snapshots(airtable_id, observed_at);

                CREATE TABLE IF NOT EXISTS referral_events (
                    event_key TEXT PRIMARY KEY,
                    airtable_id TEXT NOT NULL,
                    organisation TEXT NOT NULL,
                    event_date TEXT NOT NULL,
                    source_field TEXT NOT NULL,
                    value TEXT,
                    observed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_referral_date
                    ON referral_events(event_date);

                CREATE TABLE IF NOT EXISTS support_events (
                    event_key TEXT PRIMARY KEY,
                    airtable_id TEXT NOT NULL,
                    organisation TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_date TEXT NOT NULL,
                    source_field TEXT NOT NULL,
                    value TEXT,
                    observed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_support_date
                    ON support_events(event_date);

                CREATE TABLE IF NOT EXISTS housing_events (
                    event_key TEXT PRIMARY KEY,
                    airtable_id TEXT NOT NULL,
                    event_date TEXT NOT NULL,
                    housing_status TEXT,
                    housing_provider TEXT,
                    housing_outcome TEXT,
                    source_field TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_housing_date
                    ON housing_events(event_date);

                CREATE TABLE IF NOT EXISTS client_links (
                    source TEXT NOT NULL,
                    source_record_id TEXT NOT NULL,
                    airtable_id TEXT,
                    match_method TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (source, source_record_id)
                );

                CREATE INDEX IF NOT EXISTS idx_client_links_airtable
                    ON client_links(airtable_id, source);

                CREATE TABLE IF NOT EXISTS trello_cards (
                    card_id TEXT PRIMARY KEY,
                    airtable_id TEXT,
                    name TEXT NOT NULL,
                    fullname TEXT,
                    phone TEXT,
                    whatsapp_phone TEXT,
                    id_list TEXT,
                    closed INTEGER NOT NULL DEFAULT 0,
                    labels_json TEXT,
                    short_link TEXT,
                    url TEXT,
                    date_last_activity TEXT,
                    observed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_trello_cards_client
                    ON trello_cards(airtable_id);

                CREATE TABLE IF NOT EXISTS trello_comments (
                    action_id TEXT PRIMARY KEY,
                    card_id TEXT NOT NULL,
                    airtable_id TEXT,
                    comment_date TEXT,
                    text TEXT NOT NULL,
                    source_classification TEXT NOT NULL DEFAULT 'trello',
                    outlook_message_id TEXT,
                    observed_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_trello_comments_date
                    ON trello_comments(comment_date);

                CREATE INDEX IF NOT EXISTS idx_trello_comments_client
                    ON trello_comments(airtable_id, comment_date);

                CREATE TABLE IF NOT EXISTS communication_events (
                    event_key TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_record_id TEXT NOT NULL,
                    airtable_id TEXT,
                    card_id TEXT,
                    direction TEXT,
                    event_at TEXT,
                    sender TEXT,
                    recipients TEXT,
                    subject TEXT,
                    body_preview TEXT,
                    has_attachments INTEGER NOT NULL DEFAULT 0,
                    attachment_count INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_communication_date
                    ON communication_events(event_at, source);

                CREATE INDEX IF NOT EXISTS idx_communication_client
                    ON communication_events(airtable_id, event_at);

                CREATE TABLE IF NOT EXISTS source_sync_runs (
                    sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    records_seen INTEGER NOT NULL DEFAULT 0,
                    records_stored INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    error TEXT
                );
                """
            )

    @staticmethod
    def _safe_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        try:
            offset = value.utcoffset()
            if offset is not None and -timedelta(hours=24) < offset < timedelta(hours=24):
                return value.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
        return value.replace(tzinfo=timezone.utc)

    @classmethod
    def _dt(cls, value) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return cls._safe_datetime(value).isoformat()
        return str(value)

    @classmethod
    def _event_iso(cls, value: datetime) -> str:
        return cls._safe_datetime(value).isoformat()

    @staticmethod
    def _key(*parts: object) -> str:
        raw = "|".join("" if p is None else str(p) for p in parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def record_clients(self, clients: Iterable[dict], observed_at: datetime | None = None) -> int:
        observed_at = observed_at or datetime.now().astimezone()
        count = 0
        with self.connect() as con:
            for client in clients:
                con.execute(
                    """
                    INSERT INTO client_snapshots (
                        observed_at, airtable_id, fullname, staff, created_at,
                        last_modified, issue, homelessness_risk, eviction_date,
                        housing_status, housing_provider, housing_outcome,
                        referral_outcome, overall_outcome, housing_change_date,
                        english_level, listening_level, speaking_level,
                        reading_level, writing_level, country_of_origin,
                        nationality, gender, town, employment_status_initial,
                        employment_outcome, secure_tenancy, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._dt(observed_at),
                        client["airtable_id"],
                        client.get("fullname", ""),
                        client.get("staff", ""),
                        self._dt(client.get("created_at")),
                        self._dt(client.get("last_modified")),
                        client.get("issue", ""),
                        client.get("homelessness_risk", ""),
                        self._dt(client.get("eviction_date")),
                        client.get("housing_status", ""),
                        client.get("housing_provider", ""),
                        client.get("housing_outcome", ""),
                        client.get("referral_outcome", ""),
                        client.get("overall_outcome", ""),
                        self._dt(client.get("housing_change_date")),
                        client.get("english_level", ""),
                        client.get("listening_level", ""),
                        client.get("speaking_level", ""),
                        client.get("reading_level", ""),
                        client.get("writing_level", ""),
                        client.get("country_of_origin", ""),
                        client.get("nationality", ""),
                        client.get("gender", ""),
                        client.get("town", ""),
                        client.get("employment_status_initial", ""),
                        client.get("employment_outcome", ""),
                        client.get("secure_tenancy", ""),
                        json.dumps(client.get("raw_fields", {}), ensure_ascii=False, default=str),
                    ),
                )
                count += 1

                for event in extract_referral_events(client):
                    key = self._key(
                        event["airtable_id"],
                        event["organisation"],
                        self._event_iso(event["event_date"]),
                        event["source_field"],
                    )
                    con.execute(
                        """
                        INSERT OR IGNORE INTO referral_events
                        (event_key, airtable_id, organisation, event_date, source_field, value, observed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            key,
                            event["airtable_id"],
                            event["organisation"],
                            self._event_iso(event["event_date"]),
                            event["source_field"],
                            event.get("value", ""),
                            self._dt(observed_at),
                        ),
                    )

                for event in extract_support_events(client):
                    key = self._key(
                        event["airtable_id"],
                        event["organisation"],
                        self._event_iso(event["event_date"]),
                        event["source_field"],
                    )
                    con.execute(
                        """
                        INSERT OR IGNORE INTO support_events
                        (event_key, airtable_id, organisation, event_type, event_date, source_field, value, observed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            key,
                            event["airtable_id"],
                            event["organisation"],
                            event["event_type"],
                            self._event_iso(event["event_date"]),
                            event["source_field"],
                            event.get("value", ""),
                            self._dt(observed_at),
                        ),
                    )

                if client.get("housing_change_date"):
                    key = self._key(
                        client["airtable_id"],
                        "housing",
                        self._event_iso(client["housing_change_date"]),
                        client.get("housing_status", ""),
                        client.get("housing_provider", ""),
                        client.get("housing_outcome", ""),
                    )
                    con.execute(
                        """
                        INSERT OR IGNORE INTO housing_events
                        (event_key, airtable_id, event_date, housing_status, housing_provider, housing_outcome, source_field, observed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            key,
                            client["airtable_id"],
                            self._event_iso(client["housing_change_date"]),
                            client.get("housing_status", ""),
                            client.get("housing_provider", ""),
                            client.get("housing_outcome", ""),
                            "Date of change in housing situation",
                            self._dt(observed_at),
                        ),
                    )
            con.commit()
        return count

    def snapshot_count(self) -> int:
        with self.connect() as con:
            return int(con.execute("SELECT COUNT(*) FROM client_snapshots").fetchone()[0])

    def latest_clients(self, end: datetime | None = None) -> list[sqlite3.Row]:
        end_iso = self._dt(end) if end else self._dt(datetime.now().astimezone())
        with self.connect() as con:
            return con.execute(
                """
                SELECT s.*
                FROM client_snapshots s
                INNER JOIN (
                    SELECT airtable_id, MAX(observed_at) AS observed_at
                    FROM client_snapshots
                    WHERE observed_at <= ?
                    GROUP BY airtable_id
                ) latest
                    ON latest.airtable_id = s.airtable_id
                   AND latest.observed_at = s.observed_at
                ORDER BY s.fullname
                """,
                (end_iso,),
            ).fetchall()

    def referral_events(self, start: datetime, end: datetime) -> list[sqlite3.Row]:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM referral_events WHERE event_date >= ? AND event_date < ? ORDER BY event_date",
                (self._dt(start), self._dt(end)),
            ).fetchall()

    def support_events(self, start: datetime, end: datetime) -> list[sqlite3.Row]:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM support_events WHERE event_date >= ? AND event_date < ? ORDER BY event_date",
                (self._dt(start), self._dt(end)),
            ).fetchall()

    def housing_events(self, start: datetime, end: datetime) -> list[sqlite3.Row]:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM housing_events WHERE event_date >= ? AND event_date < ? ORDER BY event_date",
                (self._dt(start), self._dt(end)),
            ).fetchall()

    def upsert_client_link(
        self,
        source: str,
        source_record_id: str,
        airtable_id: str | None,
        match_method: str,
        confidence: float,
        observed_at: datetime | None = None,
    ) -> None:
        observed_at = observed_at or datetime.now().astimezone()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO client_links
                (source, source_record_id, airtable_id, match_method, confidence, observed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, source_record_id) DO UPDATE SET
                    airtable_id=excluded.airtable_id,
                    match_method=excluded.match_method,
                    confidence=excluded.confidence,
                    observed_at=excluded.observed_at
                """,
                (
                    source,
                    source_record_id,
                    airtable_id,
                    match_method,
                    float(confidence),
                    self._dt(observed_at),
                ),
            )
            con.commit()

    def upsert_trello_card(self, card: dict, observed_at: datetime | None = None) -> None:
        observed_at = observed_at or datetime.now().astimezone()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO trello_cards (
                    card_id, airtable_id, name, fullname, phone, whatsapp_phone,
                    id_list, closed, labels_json, short_link, url,
                    date_last_activity, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(card_id) DO UPDATE SET
                    airtable_id=excluded.airtable_id,
                    name=excluded.name,
                    fullname=excluded.fullname,
                    phone=excluded.phone,
                    whatsapp_phone=excluded.whatsapp_phone,
                    id_list=excluded.id_list,
                    closed=excluded.closed,
                    labels_json=excluded.labels_json,
                    short_link=excluded.short_link,
                    url=excluded.url,
                    date_last_activity=excluded.date_last_activity,
                    observed_at=excluded.observed_at
                """,
                (
                    card["card_id"],
                    card.get("airtable_id"),
                    card.get("name", ""),
                    card.get("fullname", ""),
                    card.get("phone", ""),
                    card.get("whatsapp_phone", ""),
                    card.get("id_list", ""),
                    int(bool(card.get("closed"))),
                    json.dumps(card.get("labels_json", []), ensure_ascii=False, default=str),
                    card.get("short_link", ""),
                    card.get("url", ""),
                    self._dt(card.get("date_last_activity")),
                    self._dt(observed_at),
                ),
            )
            con.commit()

    def upsert_trello_comment(
        self,
        action_id: str,
        card_id: str,
        airtable_id: str | None,
        comment_date: datetime | None,
        text: str,
        source_classification: str = "trello",
        outlook_message_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        observed_at = observed_at or datetime.now().astimezone()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO trello_comments (
                    action_id, card_id, airtable_id, comment_date, text,
                    source_classification, outlook_message_id, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(action_id) DO UPDATE SET
                    card_id=excluded.card_id,
                    airtable_id=excluded.airtable_id,
                    comment_date=excluded.comment_date,
                    text=excluded.text,
                    source_classification=excluded.source_classification,
                    outlook_message_id=excluded.outlook_message_id,
                    observed_at=excluded.observed_at
                """,
                (
                    action_id,
                    card_id,
                    airtable_id,
                    self._dt(comment_date),
                    text,
                    source_classification,
                    outlook_message_id,
                    self._dt(observed_at),
                ),
            )
            con.commit()

    def upsert_communication_event(self, event: dict) -> None:
        event_key = self._key(
            event.get("source"),
            event.get("source_record_id"),
        )
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO communication_events (
                    event_key, source, source_record_id, airtable_id, card_id,
                    direction, event_at, sender, recipients, subject, body_preview,
                    has_attachments, attachment_count, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    source=excluded.source,
                    source_record_id=excluded.source_record_id,
                    airtable_id=excluded.airtable_id,
                    card_id=excluded.card_id,
                    direction=excluded.direction,
                    event_at=excluded.event_at,
                    sender=excluded.sender,
                    recipients=excluded.recipients,
                    subject=excluded.subject,
                    body_preview=excluded.body_preview,
                    has_attachments=excluded.has_attachments,
                    attachment_count=excluded.attachment_count,
                    metadata_json=excluded.metadata_json
                """,
                (
                    event_key,
                    event.get("source", "unknown"),
                    event.get("source_record_id", ""),
                    event.get("airtable_id"),
                    event.get("card_id"),
                    event.get("direction"),
                    self._dt(event.get("event_at")),
                    event.get("sender", ""),
                    event.get("recipients", ""),
                    event.get("subject", ""),
                    event.get("body_preview", ""),
                    int(bool(event.get("has_attachments"))),
                    int(event.get("attachment_count") or 0),
                    json.dumps(event.get("metadata", {}), ensure_ascii=False, default=str),
                ),
            )
            con.commit()

    def begin_sync(self, source: str) -> int:
        started = datetime.now().astimezone()
        with self.connect() as con:
            cur = con.execute(
                "INSERT INTO source_sync_runs (source, started_at, status) VALUES (?, ?, 'running')",
                (source, self._dt(started)),
            )
            sync_id = int(cur.lastrowid)
            con.commit()
        return sync_id

    def finish_sync(
        self,
        sync_id: int,
        records_seen: int,
        records_stored: int,
        status: str = "ok",
        error: str | None = None,
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                UPDATE source_sync_runs
                SET finished_at=?, records_seen=?, records_stored=?, status=?, error=?
                WHERE sync_id=?
                """,
                (
                    self._dt(datetime.now().astimezone()),
                    int(records_seen),
                    int(records_stored),
                    status,
                    error,
                    sync_id,
                ),
            )
            con.commit()

    def communication_events(
        self,
        start: datetime,
        end: datetime,
        source: str | None = None,
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM communication_events WHERE event_at >= ? AND event_at < ?"
        params: list[object] = [self._dt(start), self._dt(end)]
        if source:
            query += " AND source = ?"
            params.append(source)
        query += " ORDER BY event_at"
        with self.connect() as con:
            return con.execute(query, params).fetchall()

    def trello_cards_summary(self) -> dict[str, int]:
        with self.connect() as con:
            total = int(con.execute("SELECT COUNT(*) FROM trello_cards").fetchone()[0])
            active = int(con.execute("SELECT COUNT(*) FROM trello_cards WHERE closed=0").fetchone()[0])
            linked = int(
                con.execute(
                    "SELECT COUNT(*) FROM trello_cards WHERE airtable_id IS NOT NULL AND TRIM(airtable_id) <> ''"
                ).fetchone()[0]
            )
            return {
                "total": total,
                "active": active,
                "linked": linked,
                "unlinked": total - linked,
            }

    def trello_comment_count(self, start: datetime, end: datetime) -> int:
        with self.connect() as con:
            return int(
                con.execute(
                    "SELECT COUNT(*) FROM trello_comments WHERE comment_date >= ? AND comment_date < ?",
                    (self._dt(start), self._dt(end)),
                ).fetchone()[0]
            )

    def trello_comment_classifications(self, start: datetime, end: datetime) -> dict[str, int]:
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT source_classification, COUNT(*)
                FROM trello_comments
                WHERE comment_date >= ? AND comment_date < ?
                GROUP BY source_classification
                """,
                (self._dt(start), self._dt(end)),
            ).fetchall()
        return {row[0]: int(row[1]) for row in rows}
