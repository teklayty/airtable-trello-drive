from __future__ import annotations

from .airtable_source import load_clients
from .reporting_database import ReportingDatabase
from .source_sync import sync_external_sources


def main() -> int:
    clients = load_clients()
    db = ReportingDatabase()
    count = db.record_clients(clients)
    sync_results = sync_external_sources(db, clients)
    print(f"REPORTING SNAPSHOT: stored {count} Airtable client records")
    print(f"EXTERNAL SOURCE SYNC: {sync_results}")
    print(f"TOTAL SNAPSHOTS: {db.snapshot_count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
