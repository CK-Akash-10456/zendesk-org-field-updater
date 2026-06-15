"""
main.py — Batch-update Zendesk org fields from CSV using update_many API.

Skips fields that already have values. Use --force to overwrite all.

Usage:
  python main.py <path_to_csv>
  python main.py <path_to_csv> --dry-run
  python main.py <path_to_csv> --force
"""

import csv
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests
from requests.exceptions import ConnectionError, Timeout, RequestException

CONFIG_PATH = Path(__file__).parent / "config.json"
BATCH_SIZE = 100


def eprint(*args, **kwargs):
    """Print to stderr and flush immediately."""
    print(*args, file=sys.stderr, flush=True, **kwargs)


def log(msg: str):
    """Print with timestamp."""
    eprint(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def load_config() -> dict:
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        cfg = json.loads(raw)
    except FileNotFoundError:
        log(f"Config not found: {CONFIG_PATH}. Run setup.py first.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        log(f"Config file is corrupted (invalid JSON): {e}")
        sys.exit(1)

    required = ["subdomain", "access_token", "refresh_token", "client_id", "client_secret", "field_mapping"]
    missing = [k for k in required if k not in cfg]
    if missing:
        log(f"Config missing required keys: {', '.join(missing)}. Re-run setup.py.")
        sys.exit(1)
    if not isinstance(cfg["field_mapping"], dict) or not cfg["field_mapping"]:
        log(f"field_mapping in config is empty. Re-run setup.py to map CSV columns.")
        sys.exit(1)
    return cfg


def safe_json(resp: requests.Response) -> dict:
    """Safely decode JSON. Returns {} on failure."""
    if not resp.content:
        return {}
    try:
        return resp.json()
    except json.JSONDecodeError:
        preview = resp.content[:200].decode("utf-8", errors="replace")
        log(f"  Non-JSON response (HTTP {resp.status_code}): {preview}")
        return {}


def refresh_token(config: dict) -> str | None:
    try:
        resp = requests.post(
            f"https://{config['subdomain']}.zendesk.com/oauth/tokens",
            json={"grant_type": "refresh_token", "refresh_token": config["refresh_token"],
                  "client_id": config["client_id"], "client_secret": config["client_secret"]},
            timeout=15,
        )
        if not resp.ok:
            log(f"  Token refresh failed: HTTP {resp.status_code}")
            return None
        data = safe_json(resp)
        if not data:
            return None
        new_access = data.get("access_token")
        if new_access:
            config["access_token"] = new_access
            if data.get("refresh_token"):
                config["refresh_token"] = data["refresh_token"]
            try:
                CONFIG_PATH.write_text(json.dumps(config, indent=2))
            except OSError as e:
                log(f"  Warning: could not persist new token: {e}")
        return new_access
    except (ConnectionError, Timeout) as e:
        log(f"  Token refresh network error: {e}")
        return None
    except RequestException as e:
        log(f"  Token refresh request failed: {e}")
        return None


def api_get(config: dict, path: str) -> tuple:
    """GET from Zendesk. Auto-refresh on 401. Returns (status_code, data_dict)."""
    token = config["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://{config['subdomain']}.zendesk.com/api/v2/{path}"

    for attempt in range(2):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
        except (ConnectionError, Timeout) as e:
            if attempt == 0:
                log(f"  Network error on GET {path}, retrying: {e}")
                continue
            return 0, {}
        except RequestException as e:
            log(f"  Request failed on GET {path}: {e}")
            return 0, {}

        if resp.status_code == 401 and attempt == 0:
            new_token = refresh_token(config)
            if new_token:
                headers["Authorization"] = f"Bearer {new_token}"
                continue
        return resp.status_code, safe_json(resp)

    return 0, {}


def fetch_all_org_fields(config: dict) -> dict:
    """Return {org_id: {field_key: value}} for ALL orgs."""
    result = {}
    url = f"organizations.json?per_page=100"
    page = 0
    while url:
        page += 1
        status, data = api_get(config, url)
        if status != 200:
            if page == 1:
                log(f"Cannot fetch organizations: HTTP {status}. Check credentials.")
                sys.exit(1)
            else:
                log(f"  Stopped at page {page}: HTTP {status}")
                break

        orgs = data.get("organizations")
        if not isinstance(orgs, list):
            log(f"  Unexpected response format at page {page}, stopping")
            break

        for org in orgs:
            if not isinstance(org, dict):
                continue
            oid = org.get("id")
            if oid:
                result[oid] = org.get("organization_fields") or {}

        next_url = data.get("next_page") or (data.get("links") or {}).get("next")
        url = ""
        if next_url and isinstance(next_url, str):
            prefix = f"https://{config['subdomain']}.zendesk.com/api/v2/"
            if next_url.startswith(prefix):
                url = next_url[len(prefix):]
            else:
                url = next_url

    return result


def poll_job(config: dict, job_id: str) -> bool:
    """Poll job_status. Returns True if completed."""
    deadline = time.monotonic() + 300
    last_status = ""

    while time.monotonic() < deadline:
        time.sleep(2)
        status, data = api_get(config, f"job_statuses/{job_id}.json")
        if status != 200:
            continue
        js = data.get("job_status")
        if not isinstance(js, dict):
            continue

        s = js.get("status", "")
        if s == "completed":
            results = js.get("results")
            if isinstance(results, list):
                failures = [r for r in results if isinstance(r, dict) and r.get("status") == "Failed"]
                for r in failures:
                    log(f"    Batch item failed: {json.dumps(r)}")
            return True
        elif s == "failed":
            log(f"  Batch job failed: {js.get('message', 'unknown error')}")
            return False
        elif s != last_status:
            log(f"    Job status: {s} ({js.get('progress', '?')}/{js.get('total', '?')})")
            last_status = s

    log(f"  Batch job {job_id} timed out after 5 min")
    return False


def validate_csv(rows: list, field_mapping: dict) -> bool:
    """Check CSV has required columns. Returns True if valid."""
    if not rows:
        log("CSV is empty.")
        return False

    required_cols = {"organization_id"}.union(field_mapping.keys())
    available = set(rows[0].keys())
    missing = required_cols - available
    if missing:
        log(f"CSV missing columns: {', '.join(sorted(missing))}")
        log(f"Available columns: {', '.join(sorted(available))}")
        return False
    return True


def main():
    try:
        if len(sys.argv) < 2:
            print(__doc__.strip())
            sys.exit(1)

        csv_path = Path(sys.argv[1])
        if not csv_path.exists():
            log(f"File not found: {csv_path}")
            sys.exit(1)

        dry_run = "--dry-run" in sys.argv
        force = "--force" in sys.argv

        config = load_config()
        field_mapping = config["field_mapping"]

        try:
            with open(csv_path, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except csv.Error as e:
            log(f"CSV parse error: {e}")
            sys.exit(1)
        except OSError as e:
            log(f"Cannot read CSV: {e}")
            sys.exit(1)

        log(f"Loaded {len(rows)} rows")
        log(f"Mode: {'FORCE overwrite' if force else 'skip existing'}")

        if not validate_csv(rows, field_mapping):
            sys.exit(1)

        log("Fetching current org fields from Zendesk...")
        all_fields = fetch_all_org_fields(config)
        log(f"Got {len(all_fields)} org(s)")

        updates = []
        skipped = 0
        not_found = 0
        row_errors = 0

        for row in rows:
            try:
                oid_str = (row.get("organization_id") or "").strip()
                if not oid_str:
                    skipped += 1
                    continue
                if not oid_str.isdigit():
                    log(f"  Skipping invalid org_id: {oid_str}")
                    row_errors += 1
                    continue
                oid = int(oid_str)

                if oid not in all_fields:
                    log(f"  {row.get('name', '?')}: org {oid} NOT FOUND")
                    not_found += 1
                    continue

                existing = all_fields[oid]
                desired = {}
                for csv_col, fk in field_mapping.items():
                    val = (row.get(csv_col) or "").strip()
                    if val:
                        desired[fk] = val

                if not force and existing:
                    for key in list(desired.keys()):
                        cur = existing.get(key)
                        if cur is not None and cur != "":
                            del desired[key]

                if not desired:
                    skipped += 1
                    continue

                updates.append({"id": oid, "organization_fields": desired})
            except Exception as e:
                log(f"  Error processing row: {e}")
                row_errors += 1

        if not updates:
            log(f"Nothing to update. {skipped} complete, {not_found} not found, {row_errors} errors.")
            return

        log(f"{len(updates)} need update, {skipped} complete, {not_found} not found, {row_errors} errors")

        if dry_run:
            log("DRY-RUN — would send:")
            for u in updates:
                log(f"  Org {u['id']}: {u['organization_fields']}")
            return

        total_batches = (len(updates) - 1) // BATCH_SIZE + 1

        for batch_start in range(0, len(updates), BATCH_SIZE):
            batch = updates[batch_start:batch_start + BATCH_SIZE]
            batch_num = batch_start // BATCH_SIZE + 1
            log(f"Batch {batch_num}/{total_batches} ({len(batch)} orgs)...")

            payload = {"organizations": batch}
            headers = {"Authorization": f"Bearer {config['access_token']}",
                       "Content-Type": "application/json"}

            try:
                resp = requests.put(
                    f"https://{config['subdomain']}.zendesk.com/api/v2/organizations/update_many.json",
                    json=payload, headers=headers, timeout=60,
                )
            except (ConnectionError, Timeout) as e:
                log(f"  Network error sending batch: {e}")
                log("  Skipping this batch.")
                continue
            except RequestException as e:
                log(f"  Request failed: {e}")
                continue

            if resp.status_code == 401:
                new_token = refresh_token(config)
                if new_token:
                    headers["Authorization"] = f"Bearer {new_token}"
                    try:
                        resp = requests.put(
                            f"https://{config['subdomain']}.zendesk.com/api/v2/organizations/update_many.json",
                            json=payload, headers=headers, timeout=60,
                        )
                    except (ConnectionError, Timeout) as e:
                        log(f"  Network error on retry: {e}")
                        continue
                    except RequestException as e:
                        log(f"  Retry failed: {e}")
                        continue

            if resp.status_code == 200:
                log("  Completed synchronously.")
            elif resp.status_code == 202:
                js = safe_json(resp)
                jid = (js.get("job_status") or {}).get("id") if js else None
                if jid:
                    log(f"  Job {jid} queued, waiting...")
                    ok = poll_job(config, jid)
                    log("  Done." if ok else "  FAILED.")
                else:
                    log("  Queued but no job ID returned.")
            elif resp.status_code == 422:
                body = safe_json(resp)
                errs = body.get("details") or body.get("description") or resp.text[:300]
                log(f"  Validation error: {errs}")
            else:
                log(f"  HTTP {resp.status_code}: {resp.text[:200]}")

            time.sleep(1)

        log(f"All done. {len(updates)} updated, {skipped} complete, {not_found} not found.")

    except KeyboardInterrupt:
        log("\nInterrupted by user.")
        sys.exit(130)
    except Exception as e:
        log(f"Unexpected error: {e}")
        log(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
