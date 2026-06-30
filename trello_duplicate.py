import os
import requests
from collections import defaultdict
from datetime import datetime
import re

# ==============================
# CONFIG
# ==============================

TRELLO_API_KEY = os.getenv("TRELLO_API_KEY")
TRELLO_API_TOKEN = os.getenv("TRELLO_API_TOKEN")
TRELLO_BOARD_IDS = os.getenv("TRELLO_BOARD_ID") 


# Safety + behaviour
DRY_RUN = True                # ✅ True = no changes, just print
DELETE_MODE = "archive"       # "archive" or "delete"
KEEP_STRATEGY = "newest"      # "newest" or "oldest"

if not all([TRELLO_API_KEY, TRELLO_API_TOKEN, TRELLO_BOARD_IDS]):
    raise RuntimeError("Missing Trello environment variables")

BOARD_IDS = [b.strip() for b in TRELLO_BOARD_IDS.split(",")]

BASE_URL = "https://api.trello.com/1"

AUTH = {
    "key": TRELLO_API_KEY,
    "token": TRELLO_API_TOKEN
}

# ==============================
# HTTP HELPERS
# ==============================

def trello_get(path, params=None):
    p = dict(AUTH)
    if params:
        p.update(params)

    url = f"{BASE_URL}{path}"
    r = requests.get(url, params=p, timeout=30)

    if r.status_code != 200:
        print("\n🚨 API ERROR")
        print("URL:", r.url)
        print("Status:", r.status_code)
        print("Response:", r.text)
        r.raise_for_status()

    return r.json()


def trello_delete(card_id):
    requests.delete(
        f"{BASE_URL}/cards/{card_id}",
        params=AUTH,
        timeout=30
    ).raise_for_status()


def trello_archive(card_id):
    requests.put(
        f"{BASE_URL}/cards/{card_id}",
        params={**AUTH, "closed": "true"},
        timeout=30
    ).raise_for_status()

# ==============================
# UTILITIES
# ==============================

def parse_date(card):
    return datetime.fromisoformat(
        card["dateLastActivity"].replace("Z", "+00:00")
    )





def extract_airtable_id(card):
    desc = card.get("desc", "")

    # Match Airtable record IDs (always start with "rec")
    matches = re.findall(r"\brec[a-zA-Z0-9]{8,}\b", desc)

    if matches:
        return matches[0]   # take the first one

    return None





# ==============================
# FETCH CARDS
# ==============================

def fetch_cards():
    all_cards = []

    for board_id in BOARD_IDS:
        print(f"Loading board: {board_id}")

        cards = trello_get(
            f"/boards/{board_id}/cards",
            params={
                "filter": "all",
                "fields": "id,name,desc,shortUrl,closed,dateLastActivity"
            }
        )

        all_cards.extend(cards)

    print(f"\n📊 Total cards loaded: {len(all_cards)}\n")
    return all_cards


# ==============================
# GROUP DUPLICATES
# ==============================

def group_duplicates(cards):
    groups = defaultdict(list)
    missing = 0

    for c in cards:
        aid = extract_airtable_id(c)

        if not aid:
            missing += 1
            continue

        groups[aid].append(c)

    print(f"⚠ Cards missing Airtable ID: {missing}")

    return {
        aid: entries
        for aid, entries in groups.items()
        if len(entries) > 1
    }


# ==============================
# PICK CARD TO KEEP
# ==============================

def pick_card(cards):
    sorted_cards = sorted(cards, key=parse_date)

    if KEEP_STRATEGY == "newest":
        return sorted_cards[-1]
    else:
        return sorted_cards[0]


# ==============================
# PROCESS DUPLICATES
# ==============================

def process_duplicates(duplicates):
    if not duplicates:
        print("✅ No duplicates found.")
        return

    print(f"\n❗ Found {len(duplicates)} duplicate Airtable IDs\n")

    for aid, cards in duplicates.items():
        print("=" * 80)
        print(f"Airtable ID: {aid}\n")

        keep = pick_card(cards)

        print(f"✅ KEEP: {keep['id']}")
        print(f"   {keep['shortUrl']}\n")

        for c in cards:
            if c["id"] == keep["id"]:
                continue

            state = "ARCHIVED" if c["closed"] else "OPEN"

            print(f"❌ REMOVE: {c['id']} [{state}]")
            print(f"   {c['shortUrl']}")

            if not DRY_RUN:
                if DELETE_MODE == "delete":
                    trello_delete(c["id"])
                else:
                    trello_archive(c["id"])

        print()


# ==============================
# MAIN
# ==============================

def main():
    cards = fetch_cards()

    # ✅ DEBUG BLOCK (inside main)
    print("\nDEBUG:")
    print("Total cards:", len(cards))

    duplicates = group_duplicates(cards)

    print("Duplicate groups found:", len(duplicates))

    process_duplicates(duplicates)
    for c in cards[:3]:
        print("\n--- CARD DEBUG ---")
        print("NAME:", c.get("name"))
        print("DESC:", repr(c.get("desc")))
        print("------------------\n")

    


if __name__ == "__main__":
    main()
