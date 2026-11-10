#!/bin/bash
set -e

# Load the same environment used by the existing Airtable/Trello sync.
source /home/springvolunteer/.env

# Use the same Python virtual environment as the existing sync.
exec /home/springvolunteer/pyenv/bin/python \
    /home/springvolunteer/airtable-trello-drive/remove_staff_labels.py "$@"
