#!/bin/bash
set -euo pipefail

cd /home/springvolunteer/airtable-trello-drive

python -m compileall -q reporting

git add reporting run_reporting.sh setup_reporting_runtime.sh
git diff --cached --check
git diff --cached --stat
git diff --cached

git commit -m "Add SPRING reporting system"
git push origin "$(git branch --show-current)"
