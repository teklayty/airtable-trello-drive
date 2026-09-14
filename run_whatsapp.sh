#!/bin/bash

source /home/springvolunteer/.env

exec /home/springvolunteer/pyenv/bin/python \
    /home/springvolunteer/airtable-trello-drive/whatsapp_multi_listener_in_and_out_attachment.py
