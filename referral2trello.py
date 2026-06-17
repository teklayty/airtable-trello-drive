import re
import requests
import os
import shutil
import pickle
from docx import Document

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ================================
# 🔧 CONFIG
# ================================

INPUT_FOLDER = "/mnt/chromeos/MyFiles/Downloads/referrals"
PROCESSED_FOLDER = os.path.join(INPUT_FOLDER, "processed")

TRELLO_API_KEY = os.getenv("TRELLO_API_KEY")
TRELLO_TOKEN = os.getenv("TRELLO_API_TOKEN")
TRELLO_LIST_ID = os.getenv("TRELLO_CREATE_IA_LIST_ID")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

CREDENTIALS_PATH = "/home/springvolunteer/Airtable2Trello/credentials.json"
SCOPES = ['https://www.googleapis.com/auth/drive.file']

os.makedirs(PROCESSED_FOLDER, exist_ok=True)

# ================================
# 🚫 DUPLICATES
# ================================

SEEN_FILE = os.path.join(INPUT_FOLDER, "seen_records.txt")

def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r") as f:
        return set(line.strip() for line in f)

def save_seen(record):
    with open(SEEN_FILE, "a") as f:
        f.write(record + "\n")

# ================================
# 🔐 GOOGLE AUTH
# ================================

def get_drive_service():
    creds = None

    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            CREDENTIALS_PATH, SCOPES
        )
        creds = flow.run_local_server(port=0)

        with open("token.pickle", "wb") as token:
            pickle.dump(creds, token)

    return build("drive", "v3", credentials=creds)

# ================================
# 🧠 FIELD EXTRACTION (FORM AWARE)
# ================================

def extract_fields(file_path):
    doc = Document(file_path)

    data = {"notes": ""}

    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]

            if len(cells) < 2:
                continue

            key = cells[0].lower()
            val = cells[1].strip()

            if not val:
                continue

            if "first name" in key:
                data["first_name"] = val
            elif "surname" in key:
                data["surname"] = val

            elif "telephone" in key or "contact" in key:
                data["phone"] = val
            elif "whatsapp" in key:
                data["whatsapp"] = val

            elif "email" in key:
                data["email"] = val
            elif "date of birth" in key:
                data["dob"] = val
            elif "address" in key:
                data["address"] = val

            # ✅ Referrer
            elif "form completed by" in key:
                data["referrer"] = val

            # ✅ Notes from multiple sections
            elif "other" in key:
                data["notes"] += val + "\n"
            elif "support" in key:
                data["notes"] += val + "\n"
            elif "household" in key:
                data["notes"] += val + "\n"

    # Combine name
    first = data.get("first_name", "")
    surname = data.get("surname", "")
    data["name"] = f"{first} {surname}".strip()

    return data

# ================================
# 🧹 HELPERS
# ================================

def get_field(data, key, default="unknown"):
    val = data.get(key, "").strip()
    return val if val else default

def clean_text(text):
    return re.sub(r"[^A-Za-z0-9]", "_", text)

def clean_phone(phone):
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0"):
        digits = "44" + digits[1:]
    return digits or "no_phone"

# ================================
# 📄 FILENAME
# ================================

def generate_filename(data):
    name = clean_text(get_field(data, "name"))
    phone = clean_phone(get_field(data, "phone"))
    return f"{name}_{phone}.docx"[:120]

def ensure_unique(path):
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(path):
        path = f"{base}_{i}{ext}"
        i += 1
    return path

# ================================
# 🚫 DUP CHECK
# ================================

def is_duplicate(data, seen):
    key = f"{clean_text(get_field(data,'name'))}_{clean_phone(get_field(data,'phone'))}"
    if key in seen:
        print(f"🚫 Duplicate: {key}")
        return True, key
    return False, key

# ================================
# ☁️ UPLOAD TO DRIVE
# ================================

def upload_to_drive(file_path):
    print("☁️ Uploading to Drive...")

    service = get_drive_service()

    file_metadata = {
        "name": os.path.basename(file_path),
        "parents": [DRIVE_FOLDER_ID],
    }

    media = MediaFileUpload(file_path)

    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()

    link = f"https://drive.google.com/file/d/{file['id']}/view"
    print(f"✅ Uploaded: {link}")
    return link

# ================================
# 🚀 CREATE TRELLO CARD
# ================================

def create_card(data, link):
    name = get_field(data, "name")
    contact_phone = get_field(data, "phone", "")
    whatsapp_phone = get_field(data, "whatsapp", "") or contact_phone

    whatsapp_number = clean_phone(whatsapp_phone)

    # clean referrer formatting
    referrer_raw = data.get("referrer", "").strip()
    referrer = " ".join(referrer_raw.split()) if referrer_raw else "unknown"

    desc = f"""
Name: {name}
Phone: {contact_phone}
Email: {get_field(data,'email')}
DOB: {get_field(data,'dob')}
Address: {get_field(data,'address')}

Referrer:
{referrer}

Notes:
{get_field(data,'notes')}

Other (please explain):
{get_field(data,'notes')}

📱 WhatsApp:
https://wa.me/{whatsapp_number}

💬 WhatsApp Web:
https://web.whatsapp.com/send?phone={whatsapp_number}

📎 File:
{link}
"""

    response = requests.post(
        "https://api.trello.com/1/cards",
        params={
            "key": TRELLO_API_KEY,
            "token": TRELLO_TOKEN,
            "idList": TRELLO_LIST_ID,
            "name": f"{name} ({contact_phone})",
            "desc": desc.strip(),
        }
    )

    if response.status_code == 200:
        print("✅ Trello card created")
    else:
        print("❌ Trello error", response.text)

# ================================
# 🧠 PROCESS FILE
# ================================

def process_file(file_path, seen):
    print(f"\n📄 Processing: {file_path}")

    data = extract_fields(file_path)

    duplicate, key = is_duplicate(data, seen)
    if duplicate:
        return

    filename = generate_filename(data)
    new_path = ensure_unique(os.path.join(INPUT_FOLDER, filename))

    os.rename(file_path, new_path)
    print(f"✏️ Renamed: {filename}")

    link = upload_to_drive(new_path)

    create_card(data, link)

    save_seen(key)
    seen.add(key)

    shutil.move(new_path, os.path.join(PROCESSED_FOLDER, os.path.basename(new_path)))
    print("📦 Moved to processed")

# ================================
# ▶️ MAIN
# ================================

def main():
    print("📂 Processing folder...")
    seen = load_seen()

    for f in os.listdir(INPUT_FOLDER):
        if f.endswith(".docx"):
            process_file(os.path.join(INPUT_FOLDER, f), seen)

# ================================

if __name__ == "__main__":
    main()
