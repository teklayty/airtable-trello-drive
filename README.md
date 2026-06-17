# 🚀 Airtable → Trello Sync

https://img.shields.io/badge/Python-3.10+-blue?logo=python
https://img.shields.io/badge/License-MIT-green
https://img.shields.io/badge/Status-Active-success
https://img.shields.io/badge/Maintained-Yes-brightgreen

A Python script that syncs Airtable records to Trello cards with automated updates, formatted notes, and duplicate handling.

---

## ✨ Features

- 🔄 Create & update Trello cards from Airtable  
- 📅 Set due dates and card positions automatically  
- 💬 Format notes into clean Trello comments (Markdown + bullets)  
- 🏷️ Smart status labels (Urgent / Pending / Active)  
- 👤 Preserve manual staff labels  
- 🔍 Detect duplicates using phone numbers (smart normalization)  
- 🧹 Move manual duplicates to a **cleanup list**

---

## ⚙️ Setup

Configure environment variables:

```bash
TRELLO_API_KEY=your_key
TRELLO_API_TOKEN=your_token

TRELLO_CREATE_LIST_ID=your_list_id
TRELLO_EXISTING_LIST_IDS=list1,list2,list3
TRELLO_CLEANUP_LIST_ID=your_cleanup_list

TRELLO_ALLOWED_STAFF=staff1,staff2

AIRTABLE_TOKEN=your_airtable_token


▶️ Usage
Run the script:

python airtable_trello_sync.py

📝 Notes Format (Recommended)
To include timestamps in Trello comments, store notes in Airtable like:

[16/06/2026 10:30] Called client
[16/06/2026 14:00] Documents received

⚠️ Notes
•Airtable Comments API may be restricted (403 errors depending on account)
•Timestamp extraction depends on consistent formatting
•Duplicate detection relies on phone number matching

📌 Purpose
Designed to streamline case management workflows between Airtable and Trello while keeping boards clean, structured, and up-to-date.
🤝 Contributing
Feel free to fork the repo and submit pull requests.

📄 License
MIT License
