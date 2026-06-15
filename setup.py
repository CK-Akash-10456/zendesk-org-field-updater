"""
setup.py — Interactive OAuth credential configuration for zendesk-org-field-updater.

Walks through:
  1. Zendesk subdomain, OAuth client ID, client secret
  2. Generates authorize URL → user opens in browser
  3. User pastes the redirect URL back → code auto-extracts the OAuth code
  4. Exchanges code for access + refresh tokens
  5. Tests connection (ping /users/me.json)
  6. Fetches organization fields from the account
  7. Maps CSV columns to field keys
  8. Saves config.json
"""

import json
import sys
import requests
from getpass import getpass
from pathlib import Path
from urllib.parse import urlparse, parse_qs

CONFIG_PATH = Path(__file__).parent / "config.json"
ZENDESK_FIELDS = ["billdesk_id", "support_type", "cloud", "offering"]
REDIRECT_URI = "http://localhost/callback"


def prompt(msg: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        val = getpass(f"{msg}{suffix}: ") if secret else input(f"{msg}{suffix}: ").strip()
        if val:
            return val
        if default:
            return default


def generate_authorize_url(subdomain: str, client_id: str) -> str:
    return (
        f"https://{subdomain}.zendesk.com/oauth/authorizations/new"
        f"?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope=read%20write"
    ), REDIRECT_URI


def extract_code_from_url(url: str) -> str:
    params = parse_qs(urlparse(url).query)
    code = params.get("code", [None])[0]
    if not code:
        raise ValueError("No 'code' parameter found in the URL.")
    return code


def exchange_code(subdomain: str, client_id: str, client_secret: str,
                  code: str, redirect_uri: str) -> dict:
    resp = requests.post(
        f"https://{subdomain}.zendesk.com/oauth/tokens",
        json={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "scope": "read write",
        },
        timeout=15,
    )
    if not resp.ok:
        print(f"  ERROR: {resp.status_code} — {resp.text[:200]}")
        sys.exit(1)
    return resp.json()


def test_connection(subdomain: str, access_token: str) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(
        f"https://{subdomain}.zendesk.com/api/v2/users/me.json",
        headers=headers, timeout=15,
    )
    if resp.status_code == 401:
        print("  ERROR: Invalid OAuth token (401).")
        sys.exit(1)
    if resp.status_code == 403:
        print("  ERROR: Access denied (403).")
        sys.exit(1)
    if not resp.ok:
        print(f"  ERROR: {resp.status_code} — {resp.text[:200]}")
        sys.exit(1)
    user = resp.json().get("user", {})
    return {"name": user.get("name", ""), "email": user.get("email", ""), "role": user.get("role", "")}


def fetch_org_fields(subdomain: str, access_token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(
        f"https://{subdomain}.zendesk.com/api/v2/organization_fields.json",
        headers=headers, timeout=15,
    )
    if not resp.ok:
        print(f"  ERROR fetching org fields: {resp.status_code} — {resp.text[:200]}")
        sys.exit(1)
    return resp.json().get("organization_fields", [])


def main():
    print("=" * 55)
    print("  Zendesk Org Field Updater — OAuth Setup")
    print("=" * 55)

    if CONFIG_PATH.exists():
        overwrite = input(f"\nConfig exists at {CONFIG_PATH}. Overwrite? [y/N]: ").strip().lower()
        if overwrite != "y":
            print("Setup cancelled.")
            return

    print()
    print("Before proceeding, create an OAuth app in Zendesk:")
    print("  Admin > Apps > OAuth > Add OAuth Client")
    print(f"  Redirect URL: {REDIRECT_URI}")
    print()

    subdomain = prompt("Zendesk subdomain", default="")
    client_id = prompt("OAuth client ID", default="")
    client_secret = prompt("OAuth client secret", secret=True)

    auth_url, redirect_uri = generate_authorize_url(subdomain, client_id)

    print("\n--- OAuth Authorization ---")
    print("1. Open this URL in your browser:\n")
    print(f"   {auth_url}\n")
    print("2. Authorize the app.")
    print(f"3. You'll be redirected to {REDIRECT_URI}?code=...")
    print("   (The page will fail to load — that's expected.)")
    print("4. Copy the FULL redirect URL from your browser's address bar")
    print("   and paste it below:\n")

    pasted_url = input("   Pasted URL: ").strip()
    if not pasted_url:
        print("  No URL provided.")
        sys.exit(1)

    try:
        code = extract_code_from_url(pasted_url)
    except ValueError as e:
        print(f"  ERROR: {e}")
        sys.exit(1)
    print(f"  Authorization code extracted.")

    print("\n--- Exchanging code for tokens ---")
    token_data = exchange_code(subdomain, client_id, client_secret, code, redirect_uri)
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    if not access_token:
        print("  ERROR: No access_token in response.")
        sys.exit(1)
    print(f"  Access token obtained.")
    if refresh_token:
        print(f"  Refresh token obtained.")

    print("\n--- Testing Connection ---")
    user_info = test_connection(subdomain, access_token)
    print(f"  Connected as: {user_info['name']} ({user_info['email']}) — Role: {user_info['role']}")

    print("\n--- Fetching Organization Fields ---")
    org_fields = fetch_org_fields(subdomain, access_token)
    if not org_fields:
        print("  No organization fields found in this account.")
        print("  Create them in Zendesk Admin > Organizations > Fields, then re-run setup.")
        sys.exit(1)

    print(f"  Found {len(org_fields)} organization field(s):\n")
    for f in org_fields:
        print(f"    [{f['key']}] {f['title']}  (type: {f['type']})")

    print("\n--- Map CSV Columns to Organization Fields ---")
    print("For each CSV column below, enter the matching field KEY.\n")

    field_map = {}
    available = {f["key"]: f["title"] for f in org_fields}

    for csv_col in ZENDESK_FIELDS:
        if available:
            print(f"  Available keys: {', '.join(available.keys())}")
        key = prompt(f"  Field key for '{csv_col}'", default="")
        if key not in available:
            print(f"  WARNING: '{key}' not in fetched org fields — will be used as-is.")
        field_map[csv_col] = key

    config = {
        "subdomain": subdomain,
        "auth_mode": "oauth",
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "field_mapping": field_map,
    }

    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    print(f"\nConfiguration saved to {CONFIG_PATH}")
    print("You can now run:  python main.py <path_to_csv>")


if __name__ == "__main__":
    main()
