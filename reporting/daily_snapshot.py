from __future__ import annotations

from .airtable_source import load_clients
from .reporting_database import ReportingDatabase


def main() -> int:
    clients = load_clients()
    db = ReportingDatabase()
    count = db.record_clients(clients)
    print(f"REPORTING SNAPSHOT: stored {count} Airtable client records")
    print(f"TOTAL SNAPSHOTS: {db.snapshot_count()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
