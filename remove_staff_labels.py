#!/usr/bin/env python3

import argparse
import os
import sys
import requests

BASE_URL = "https://api.trello.com/1"
BOARD_NAME = "SPRING Daily Tasks"

# Trello label names are supplied through TRELLO_LABEL_NAMES_TO_REMOVE in .env.
LABEL_NAMES_TO_REMOVE = {
    name.strip()
    for name in os.getenv("TRELLO_LABEL_NAMES_TO_REMOVE", "").split(",")
    if name.strip()
}

TRELLO_API_KEY = os.getenv("TRELLO_API_KEY")
TRELLO_API_TOKEN = os.getenv("TRELLO_API_TOKEN")


def check_credentials():
    if not TRELLO_API_KEY:
        print("ERROR: TRELLO_API_KEY is not set.")
        sys.exit(1)

    if not TRELLO_API_TOKEN:
        print("ERROR: TRELLO_API_TOKEN is not set.")
        sys.exit(1)


AUTH = {
    "key": TRELLO_API_KEY,
    "token": TRELLO_API_TOKEN,
}


def trello_get(path, params=None):
    response = requests.get(
        BASE_URL + path,
        params={**AUTH, **(params or {})},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def trello_delete(path):
    response = requests.delete(
        BASE_URL + path,
        params=AUTH,
        timeout=30,
    )
    response.raise_for_status()
    return response.json() if response.text else None


def find_board():
    boards = trello_get(
        "/members/me/boards",
        {
            "filter": "open",
            "fields": "id,name,closed",
        },
    )

    for board in boards:
        if board.get("name") == BOARD_NAME:
            return board

    print(f"ERROR: Trello board not found: {BOARD_NAME}")
    sys.exit(1)


def get_board_labels(board_id):
    return trello_get(
        f"/boards/{board_id}/labels",
        {
            "limit": 1000,
            "fields": "id,name,color",
        },
    )


def get_board_cards(board_id):
    return trello_get(
        f"/boards/{board_id}/cards",
        {
            "filter": "all",
            "fields": "id,name,idLabels",
            "limit": 10000,
        },
    )


def main():
    parser = argparse.ArgumentParser(
        description="Remove selected Trello labels from all cards on the SPRING Daily Tasks board."
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without changing Trello.",
    )

    args = parser.parse_args()

    check_credentials()

    print("=" * 70)
    print("TRELLO STAFF/VOLUNTEER LABEL REMOVAL")
    print("=" * 70)
    print(f"Board: {BOARD_NAME}")
    print(f"Labels requested for removal: {sorted(LABEL_NAMES_TO_REMOVE)}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print()

    board = find_board()
    board_id = board["id"]

    print(f"Found board: {board['name']}")
    print(f"Board ID: {board_id}")
    print()

    labels = get_board_labels(board_id)

    labels_by_name = {
        label.get("name"): label
        for label in labels
        if label.get("name")
    }

    matching_labels = {
        name: labels_by_name[name]
        for name in LABEL_NAMES_TO_REMOVE
        if name in labels_by_name
    }

    missing_labels = LABEL_NAMES_TO_REMOVE - set(matching_labels)

    print("Matching Trello labels:")
    if matching_labels:
        for name, label in sorted(matching_labels.items()):
            print(
                f"  {name!r} -> ID {label['id']} "
                f"(colour={label.get('color')})"
            )
    else:
        print("  None")

    if missing_labels:
        print()
        print("Labels not found on the board:")
        for name in sorted(missing_labels):
            print(f"  {name!r}")

    print()

    cards = get_board_cards(board_id)

    print(f"Cards found: {len(cards)}")
    print()

    removals = []

    label_ids_to_remove = {
        label["id"]: label["name"]
        for label in matching_labels.values()
    }

    for card in cards:
        card_labels = set(card.get("idLabels", []))

        matching_card_labels = [
            label_ids_to_remove[label_id]
            for label_id in card_labels
            if label_id in label_ids_to_remove
        ]

        if matching_card_labels:
            removals.append(
                {
                    "card_id": card["id"],
                    "card_name": card.get("name", ""),
                    "labels": sorted(matching_card_labels),
                }
            )

    print(f"Cards requiring label removal: {len(removals)}")
    print()

    if not removals:
        print("No matching labels were found on any cards.")
        print()
        return

    for item in removals:
        print(
            f"  Card: {item['card_name']!r} "
            f"| Remove: {', '.join(item['labels'])}"
        )

    print()

    if args.dry_run:
        print("DRY RUN: No changes were made to Trello.")
        print("Run without --dry-run to perform the removals.")
        return

    print("Starting LIVE removal...")
    print()

    successful = 0
    failed = 0

    for item in removals:
        card_id = item["card_id"]

        for label_name in item["labels"]:
            label_id = matching_labels[label_name]["id"]

            try:
                trello_delete(
                    f"/cards/{card_id}/idLabels/{label_id}"
                )

                print(
                    f"REMOVED: {label_name!r} "
                    f"from {item['card_name']!r}"
                )
                successful += 1

            except requests.RequestException as exc:
                print(
                    f"ERROR removing {label_name!r} "
                    f"from {item['card_name']!r}: {exc}"
                )
                failed += 1

    print()
    print("=" * 70)
    print("VERIFYING REMOVALS")
    print("=" * 70)

    remaining_cards = get_board_cards(board_id)

    remaining = []

    for card in remaining_cards:
        card_labels = set(card.get("idLabels", []))

        remaining_labels = [
            label_ids_to_remove[label_id]
            for label_id in card_labels
            if label_id in label_ids_to_remove
        ]

        if remaining_labels:
            remaining.append(
                {
                    "card_name": card.get("name", ""),
                    "labels": sorted(remaining_labels),
                }
            )

    print()

    if remaining:
        print("WARNING: Some requested labels are still present:")
        for item in remaining:
            print(
                f"  Card: {item['card_name']!r} "
                f"| Remaining: {', '.join(item['labels'])}"
            )
    else:
        print("VERIFICATION PASSED.")
        print("No requested staff/volunteer labels remain on the board.")

    print()
    print("=" * 70)
    print("RESULT")
    print("=" * 70)
    print(f"Successful label removals: {successful}")
    print(f"Failed label removals:     {failed}")
    print(f"Remaining cards with labels: {len(remaining)}")
    print("=" * 70)


if __name__ == "__main__":
    main()
