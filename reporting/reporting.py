from __future__ import annotations

import argparse

from .metrics import month_boundaries, quarter_boundaries, week_boundaries
from .report_builder import run_report


def main() -> int:
    parser = argparse.ArgumentParser(description="SPRING reporting engine")
    parser.add_argument("report", choices=["weekly", "monthly", "quarterly"])
    parser.add_argument("--no-xlsx", action="store_true", help="Skip Excel output")
    args = parser.parse_args()

    if args.report == "weekly":
        start, end, label = week_boundaries()
        kind = "Weekly"
    elif args.report == "monthly":
        start, end, label = month_boundaries()
        kind = "Monthly"
    else:
        start, end, label = quarter_boundaries()
        kind = "Quarterly"

    outputs = run_report(
        kind,
        start,
        end,
        label,
        generate_xlsx=not args.no_xlsx,
    )
    for key, path in outputs.items():
        print(f"{key.upper()}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
