"""
sync-org-ids.py — Replace stale org IDs in CSV with live IDs from Zendesk.

Matches organizations by name (case-insensitive) and updates the
organization_id column in-place.

Usage:
  python sync-org-ids.py <csv_path>
  python sync-org-ids.py "Org List Zendesk - Sheet1.csv"
"""

import csv
import json
import sys
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"Config not found at {CONFIG_PATH}")
        print("Run setup.py first.")
        sys.exit(1)
    return json.loads(CONFIG_PATH.read_text())


def fetch_all_orgs(subdomain: str, access_token: str) -> list[dict]:
    orgs = []
    url = f"https://{subdomain}.zendesk.com/api/v2/organizations.json?per_page=100"
    headers = {"Authorization": f"Bearer {access_token}"}
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 401:
            print("Token expired. Re-run setup.py or refresh manually.")
            sys.exit(1)
        if not resp.ok:
            print(f"API error: {resp.status_code} — {resp.text[:200]}")
            sys.exit(1)
        data = resp.json()
        orgs.extend(data.get("organizations", []))
        url = data.get("next_page") or (data.get("links") or {}).get("next")
    return orgs


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)

    csv_path = Path(sys.argv[1])
    if not csv_path.exists():
        print(f"File not found: {csv_path}")
        sys.exit(1)

    config = load_config()
    subdomain = config["subdomain"]
    access_token = config["access_token"]

    print("Fetching organizations from Zendesk...")
    orgs = fetch_all_orgs(subdomain, access_token)
    print(f"  Found {len(orgs)} organization(s)\n")

    # Build name -> id mapping (case-insensitive)
    name_to_id = {}
    for o in orgs:
        name = (o.get("name") or "").strip().lower()
        if name:
            name_to_id[name] = o["id"]

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames

    if not rows:
        print("CSV is empty.")
        sys.exit(0)

    matched = 0
    unmatched = 0
    skipped = 0
    updates = []

    for row in rows:
        old_id = row.get("organization_id", "").strip()
        name = (row.get("name") or "").strip()

        if not name:
            skipped += 1
            updates.append(row)
            continue

        live_id = name_to_id.get(name.lower())
        if live_id:
            row["organization_id"] = str(live_id)
            if old_id and old_id != str(live_id):
                print(f"  {name:35s} {old_id} → {live_id}")
            matched += 1
        else:
            print(f"  {name:35s} NOT FOUND in Zendesk")
            unmatched += 1
        updates.append(row)

    # Write updated CSV back
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(updates)

    print(f"\nDone. {matched} matched, {unmatched} unmatched, {skipped} skipped (no name).")
    print(f"Updated {csv_path}")


if __name__ == "__main__":
    main()
