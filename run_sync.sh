#!/bin/bash
set -e

# Load environment and activate the project virtual environment.
source /home/springvolunteer/.env
source /home/springvolunteer/pyenv/bin/activate

# Run the Airtable -> Trello sync.
exec python /home/springvolunteer/airtable-trello-drive/airtable_trello_sync.py


