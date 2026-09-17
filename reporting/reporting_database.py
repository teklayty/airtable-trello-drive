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
                """
            )

    @staticmethod
    def _safe_datetime(value: datetime) -> datetime:
        """Normalise a datetime before serialising it to ISO text."""
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
