#!/bin/bash
set -euo pipefail

RUNTIME_ROOT=/home/springvolunteer/Airtable2Trello

mkdir -p \
  "$RUNTIME_ROOT/reporting_data" \
  "$RUNTIME_ROOT/reports/weekly" \
  "$RUNTIME_ROOT/reports/monthly" \
  "$RUNTIME_ROOT/reports/quarterly"

chmod 750 "$RUNTIME_ROOT/reporting_data" "$RUNTIME_ROOT/reports" \
  "$RUNTIME_ROOT/reports/weekly" "$RUNTIME_ROOT/reports/monthly" "$RUNTIME_ROOT/reports/quarterly"

echo "SPRING reporting runtime directories ready under $RUNTIME_ROOT"
