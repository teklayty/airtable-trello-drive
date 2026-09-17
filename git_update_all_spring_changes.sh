#!/bin/bash
set -euo pipefail

cd /home/springvolunteer/airtable-trello-drive

python -m compileall -q reporting
python -m py_compile \
  airtable_trello_sync.py \
  whatsapp_multi_listener_in_and_out_attachment.py

# Stage source/configuration only. Generated reports, reporting DB, logs,
# credentials and other runtime data live under /home/springvolunteer/Airtable2Trello/.
git add \
  airtable_trello_sync.py \
  whatsapp_multi_listener_in_and_out_attachment.py \
  run_sync.sh \
  run_whatsapp.sh \
  run_reporting.sh \
  reporting

git diff --cached --check
git diff --cached --stat
echo
echo "Review the staged changes above. When they are correct, run:"
echo '  git commit -m "Add SPRING reporting and service updates"'
echo '  git push origin "$(git branch --show-current)"'
