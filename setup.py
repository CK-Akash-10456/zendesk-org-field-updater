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
    """Prompt the user until a non-empty value (or a default) is given.

    Args:
        msg: The label shown before the input field.
        default: Returned if the user submits an empty line; shown in brackets.
        secret: When True, hide typed characters (used for the client secret).

    Returns:
        The entered value, or the default when input is left blank.
    """
    suffix = f" [{default}]" if default else ""
    # Loop so a blank answer with no default re-asks instead of returning "".
    while True:
        val = getpass(f"{msg}{suffix}: ") if secret else input(f"{msg}{suffix}: ").strip()
        if val:
            return val
        if default:
            return default


def generate_authorize_url(subdomain: str, client_id: str) -> str:
    """Build the Zendesk OAuth authorize URL the user opens in a browser.

    Args:
        subdomain: The Zendesk account subdomain (e.g. "acme").
        client_id: The OAuth client identifier of the registered app.

    Returns:
        A tuple of (authorize_url, redirect_uri). The redirect_uri is returned
        too so the caller can reuse the exact value during code exchange.
    """
    return (
        f"https://{subdomain}.zendesk.com/oauth/authorizations/new"
        f"?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope=read%20write"
    ), REDIRECT_URI


def extract_code_from_url(url: str) -> str:
    """Pull the ``code`` query parameter out of the pasted redirect URL.

    After authorizing, Zendesk redirects to ``REDIRECT_URI?code=...``; the page
    fails to load but the address bar still holds the authorization code.

    Args:
        url: The full redirect URL the user copied from the browser.

    Returns:
        The OAuth authorization code.

    Raises:
        ValueError: If the URL has no ``code`` parameter.
    """
    params = parse_qs(urlparse(url).query)
    code = params.get("code", [None])[0]
    if not code:
        raise ValueError("No 'code' parameter found in the URL.")
    return code


def exchange_code(subdomain: str, client_id: str, client_secret: str,
                  code: str, redirect_uri: str) -> dict:
    """Trade the one-time authorization code for OAuth tokens.

    Args:
        subdomain: The Zendesk account subdomain.
        client_id: The OAuth client identifier.
        client_secret: The OAuth client secret.
        code: The authorization code from ``extract_code_from_url``.
        redirect_uri: The same redirect URI used to request the code.

    Returns:
        The token response dict (access_token, refresh_token, etc.).
        Exits the process if Zendesk returns a non-OK status.
    """
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
    """Verify the access token by calling the /users/me endpoint.

    Confirms the token is valid and has access before we save anything.

    Args:
        subdomain: The Zendesk account subdomain.
        access_token: The freshly obtained OAuth access token.

    Returns:
        A dict with the authenticated user's name, email, and role.
        Exits the process on 401 (invalid token), 403 (denied), or other errors.
    """
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
    """Fetch the account's organization field definitions.

    These are the custom fields the user later maps CSV columns onto.

    Args:
        subdomain: The Zendesk account subdomain.
        access_token: A valid OAuth access token.

    Returns:
        A list of organization field dicts (each with key/title/type).
        Exits the process if the request fails.
    """
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
    """Run the interactive OAuth setup wizard and write config.json.

    Guides the user through entering app credentials, completing the browser
    authorization flow, exchanging the code for tokens, verifying the
    connection, and mapping CSV columns to organization field keys.
    """
    print("=" * 55)
    print("  Zendesk Org Field Updater — OAuth Setup")
    print("=" * 55)

    # Confirm before clobbering an existing configuration.
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

    # Ask the user which org field key each known CSV column should write to.
    field_map = {}
    available = {f["key"]: f["title"] for f in org_fields}

    for csv_col in ZENDESK_FIELDS:
        if available:
            print(f"  Available keys: {', '.join(available.keys())}")
        key = prompt(f"  Field key for '{csv_col}'", default="")
        # Warn but still accept unknown keys, in case the field is created later.
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
