#!/bin/bash
set -euo pipefail

source /home/springvolunteer/.env
source /home/springvolunteer/pyenv/bin/activate

cd /home/springvolunteer/airtable-trello-drive

# Generated reporting data is intentionally kept outside the Git repository.
export REPORT_DB_PATH="${REPORT_DB_PATH:-/home/springvolunteer/Airtable2Trello/reporting_data/reporting.db}"
export REPORT_OUTPUT_DIR="${REPORT_OUTPUT_DIR:-/home/springvolunteer/Airtable2Trello/reports}"

mkdir -p /home/springvolunteer/Airtable2Trello/reporting_data \
         /home/springvolunteer/Airtable2Trello/reports/weekly \
         /home/springvolunteer/Airtable2Trello/reports/monthly \
         /home/springvolunteer/Airtable2Trello/reports/quarterly

MODE="${1:-weekly}"

case "$MODE" in
  weekly|monthly|quarterly)
    exec python -m reporting.reporting "$MODE"
    ;;
  daily)
    exec python -m reporting.daily_snapshot
    ;;
  *)
    echo "Usage: $0 {daily|weekly|monthly|quarterly}" >&2
    exit 2
    ;;
esac
