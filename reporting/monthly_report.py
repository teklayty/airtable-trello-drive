from __future__ import annotations

from .metrics import month_boundaries
from .report_builder import run_report


def main() -> int:
    start, end, label = month_boundaries()
    outputs = run_report("Monthly", start, end, label)
    for key, path in outputs.items():
        print(f"{key.upper()}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
