#!/bin/bash
set -e

source /home/springvolunteer/.env

# Run the WhatsApp multi-listener using the project's Python environment.
exec /home/springvolunteer/pyenv/bin/python \
    /home/springvolunteer/airtable-trello-drive/whatsapp_multi_listener_in_and_out_attachment.py

