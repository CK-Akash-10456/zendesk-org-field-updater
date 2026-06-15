# Zendesk Organization Field Updater

A standalone tool to bulk-update custom organization fields in Zendesk from a CSV file. Uses OAuth 2.0 with refresh-token support and the batch `update_many` API for speed.

---

## How It Works

### High-Level Flow

```
CSV (org IDs + field values)
        │
        ▼
  ┌─────────────┐     ┌──────────────────┐
  │  setup.py   │────▶│   config.json    │
  │  (OAuth)    │     │  (credentials +  │
  └─────────────┘     │   field mapping) │
                      └────────┬─────────┘
                               │
                               ▼
  ┌─────────────┐     ┌──────────────────┐
  │  main.py    │────▶│  Zendesk API     │
  │  (updater)  │     │  update_many     │
  └─────────────┘     └──────────────────┘
```

### Step-by-Step

1. **Setup** (`setup.py`)
   - Prompts for Zendesk subdomain, OAuth client ID, and client secret
   - Generates the OAuth authorize URL (redirects to `http://localhost/callback`)
   - You open the URL in a browser, authorize the app, and paste the redirect URL back
   - The tool extracts the `?code=` parameter, exchanges it for an access token + refresh token, and verifies the connection
   - Fetches all organization fields from the account and lets you map each CSV column to a field key
   - Saves everything to `config.json`

2. **Sync Org IDs** (`sync-org-ids.py`) — *optional, one-time*
   - If your CSV has stale org IDs from a different Zendesk account, this script matches organizations by name and replaces the IDs with the live ones from your account

3. **Update** (`main.py`)
   - Fetches **all** organizations and their current field values from Zendesk in one pass
   - Compares CSV values against existing values — skips fields that are already populated (use `--force` to overwrite)
   - Groups orgs that need changes into batches of 100 and sends them via `PUT /api/v2/organizations/update_many` (the Zendesk batch API)
   - Handles token refresh, network retries, async job polling, and all error cases

### Batch API vs Individual Requests

| Approach | API Calls for 500 orgs | Time Estimate |
|----------|----------------------|---------------|
| Individual PUTs (v1) | 500 | ~3 min |
| Batch `update_many` (v2) | 1 GET + 5 PUTs | ~30 sec |

The batch endpoint accepts up to 100 organizations per request and processes them asynchronously.

### Smart Update (Skip Existing)

By default, `main.py` fetches the current field values for every org, then only sends fields that are empty or missing. This means:
- Re-running the script after a partial update will only process the remaining orgs
- If all fields are already set, the script does zero write API calls
- Use `--force` to override and write every CSV value regardless

---

## Prerequisites

- Python 3.10+
- `requests` library (`pip install requests`)
- A Zendesk account with admin privileges
- An OAuth app registered in Zendesk:
  - Admin > Apps > OAuth > Add OAuth Client
  - Redirect URL: `http://localhost/callback`
  - Scopes: read, write

---

## Files

| File | Purpose |
|------|---------|
| `setup.py` | One-time interactive OAuth setup |
| `main.py` | Batch-update org fields from CSV |
| `sync-org-ids.py` | Replace stale org IDs with live ones |
| `sample.csv` | Example CSV format |
| `config.json` | Credentials + field mapping (auto-generated) |

---

## Usage

### 1. Setup OAuth

```bash
cd zendesk-org-field-updater
pip install requests
python setup.py
```

You'll be prompted for:
- **Zendesk subdomain** — e.g., `mycompany` (from `mycompany.zendesk.com`)
- **OAuth client ID** — from the app you registered in Zendesk
- **OAuth client secret** — from the same app

Then:
1. Open the printed URL in your browser
2. Authorize the app
3. The browser will redirect to `http://localhost/callback?code=...` (the page will fail to load — that's expected)
4. **Copy the full URL from the browser's address bar** and paste it into the terminal

The tool will exchange the code for tokens, test the connection, fetch your organization fields, and ask you to map the 4 CSV columns to field keys.

### 2. Prepare Your CSV

Required columns:

```
organization_id,billdesk_id,support_type,cloud,offering
12345,BD-001,Premium,AWS,SaaS
67890,BD-002,Standard,GCP,PaaS
```

- `organization_id` must match Zendesk org IDs
- Other columns should match whatever you mapped in setup
- If your CSV has stale org IDs from another source, run `sync-org-ids.py` first

### 3. Sync Org IDs (if needed)

```bash
python sync-org-ids.py "Org List Zendesk - Sheet1.csv"
```

This matches each row's `name` column against Zendesk org names and updates `organization_id` to the live ID.

### 4. Update Organizations

```bash
# Smart update — only fills empty fields
python main.py your-orgs.csv

# Force overwrite — replaces all fields even if populated
python main.py your-orgs.csv --force

# Preview what would be updated without making changes
python main.py your-orgs.csv --dry-run
```

### Output

```
[12:42:22] Loaded 552 rows
[12:42:22] Fetching current org fields from Zendesk...
[12:42:27] Got 567 org(s)
[12:42:27]   1800 Accountant: org 28055150769820 -> 1 field(s) to update
[12:42:27] Batch 1/2 (100 orgs)...
[12:42:28]   Job 2d318d60... queued, waiting...
[12:42:32]   Done.
[12:42:33] Batch 2/2 (91 orgs)...
[12:42:34]   Completed synchronously.
[12:42:34] All done. 191 updated, 351 complete, 10 not found.
```

---

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Token expired | Auto-refreshes using refresh token, retries the request |
| Network timeout | Retries once, then skips the batch |
| Non-JSON response | Logs the raw response body, continues |
| Org not found | Reports it, skips (no crash) |
| CSV parse error | Reports details, exits cleanly |
| Config corrupted | Detects invalid JSON/missing keys, asks to re-run setup |
| Keyboard interrupt (Ctrl+C) | Exits gracefully with status 130 |
| Async job failure | Polls `job_status`, reports which items failed |

---

## config.json Structure

```json
{
  "subdomain": "mycompany",
  "auth_mode": "oauth",
  "client_id": "my_oauth_client_id",
  "client_secret": "abc123...",
  "access_token": "eyJraWQiOi...",
  "refresh_token": "def456...",
  "field_mapping": {
    "billdesk_id": "billdesk_id",
    "support_type": "support_type",
    "cloud": "cloud",
    "offering": "offering"
  }
}
```

The field mapping maps CSV column names → Zendesk organization field keys. If your Zendesk org fields have different keys, you can edit this manually.

---

## Rate Limits

The batch API respects Zendesk's rate limits:
- 100 orgs max per request
- Rate-limited by your plan (200–700 req/min)
- Async jobs for larger batches have their own queue

The tool includes a 1-second delay between batches to stay safe.

---

## Limitations

- The 10 unmatched orgs (Big Picture Medical, example, Flamapp, Kyvos Insights, etc.) either don't exist in the Zendesk account or have different names. They need to be created manually in Zendesk.
- Attachment files, user passwords, and webhook private keys are not migratable (Zendesk limitations).
