#!/usr/bin/env python3
"""
UK Job Market Tracker — Adzuna data collector

For each sector category, collects two different things:

  1. SALARY TREND — a 12-month average-salary series, via
     jobs/{country}/history. Confirmed response shape:
        {"month": {"2026-08": 68397.91, ...}, "location": {...}}
     Note there is no separate salary endpoint, and no "locations" list
     comes back when a category is included.

  2. VACANCY COUNT — how many jobs are currently advertised, via
     jobs/{country}/search/1 with results_per_page=1, reading the
     top-level "count" field.

Both matter and they move independently: a sector's average salary can
rise precisely because hiring collapsed and only senior roles are left
advertised. Salary alone is a pay-level signal, not a demand signal.

Collected for both UK-wide and London, so a London-focused app can show
local figures and the national comparison side by side.

Credentials come from environment variables — never hardcode them here,
this repo is public.

Required env vars:
    ADZUNA_APP_ID
    ADZUNA_APP_KEY

Local run:
    export ADZUNA_APP_ID=xxxx
    export ADZUNA_APP_KEY=xxxx
    python adzuna_uk_tracker.py
"""

import os
import sys
import json
import time
import requests
from datetime import datetime, timezone
from pathlib import Path

APP_ID = os.environ.get("ADZUNA_APP_ID")
APP_KEY = os.environ.get("ADZUNA_APP_KEY")
COUNTRY = "gb"
BASE = f"https://api.adzuna.com/v1/api/jobs/{COUNTRY}"

# Areas to collect for. None = UK-wide (no location1 filter).
# ~30 categories x 2 areas x 2 calls each = ~120 calls per run, which is
# comfortably inside the free tier's few-hundred-per-day allowance.
AREAS = [
    ("uk", None),
    ("london", "London"),
]

# Fail the run if more than this fraction of individual data cells come
# back empty. Measured per cell (sector x area x metric), NOT per sector:
# an earlier version counted only sectors where absolutely everything
# failed, so a run missing ~30% of its cells still passed at "13% empty"
# and committed a snapshot full of holes.
MAX_EMPTY_FRACTION = 0.15

# Keep only the N most recent dated snapshot files; latest.json and
# history.json are what the app actually reads.
KEEP_SNAPSHOTS = 12

DATA_DIR = Path("data")
HISTORY_FILE = DATA_DIR / "history.json"
LATEST_FILE = DATA_DIR / "latest.json"

REQUEST_DELAY = 2.0  # base pause between calls

# Adzuna rate-limits with 503s once a run gets long — a ~120-call run at
# 1s intervals hit them steadily through its back half, leaving roughly a
# third of cells empty. These are transient, so retry rather than give up.
MAX_RETRIES = 4
BACKOFF_BASE = 3.0  # seconds: 3, 6, 12, 24
RETRY_STATUSES = {429, 500, 502, 503, 504}


def require_credentials():
    if not APP_ID or not APP_KEY:
        sys.exit(
            "Missing ADZUNA_APP_ID / ADZUNA_APP_KEY environment variables.\n"
            "Set them locally, or as GitHub Actions repo secrets.\n"
            "Never hardcode API credentials directly in this file."
        )


def get(url, params):
    params = {
        **params,
        "app_id": APP_ID,
        "app_key": APP_KEY,
        "content-type": "application/json",
    }

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code in RETRY_STATUSES:
                wait = BACKOFF_BASE * (2 ** attempt)
                print(f"    HTTP {resp.status_code}, retrying in {wait:.0f}s "
                      f"(attempt {attempt + 1}/{MAX_RETRIES})")
                last_error = requests.HTTPError(f"HTTP {resp.status_code}")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return resp.json()
        except requests.Timeout as e:
            wait = BACKOFF_BASE * (2 ** attempt)
            print(f"    timeout, retrying in {wait:.0f}s "
                  f"(attempt {attempt + 1}/{MAX_RETRIES})")
            last_error = e
            time.sleep(wait)

    raise last_error or requests.RequestException("request failed after retries")


def fetch_categories():
    """All sector categories Adzuna applies to UK jobs."""
    data = get(f"{BASE}/categories", {})
    return [{"label": c["label"], "tag": c["tag"]} for c in data.get("results", [])]


def fetch_salary_trend(category_tag, location1=None):
    """Monthly average-salary series for a category, UK-wide or one area."""
    params = {"category": category_tag, "location0": "UK"}
    if location1:
        params["location1"] = location1
    data = get(f"{BASE}/history", params)
    return data.get("month", {})


def fetch_vacancy_count(category_tag, location1=None):
    """
    Current number of advertised vacancies for a category.

    Uses the search endpoint with results_per_page=1 — we only want the
    top-level "count", not the listings themselves.

    NOT verified live from the dev sandbox (api.adzuna.com is
    unreachable there), so smoke-test one URL by hand before trusting a
    scheduled run — see the note in the handoff.
    """
    params = {
        "category": category_tag,
        "location0": "UK",
        "results_per_page": 1,
    }
    if location1:
        params["location1"] = location1
    data = get(f"{BASE}/search/1", params)
    return data.get("count")


def build_snapshot():
    categories = fetch_categories()
    if not categories:
        sys.exit("No categories returned — aborting rather than writing an empty snapshot.")

    snapshot = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "areas": [name for name, _ in AREAS],
        "sectors": [],
    }

    total_cells = 0
    empty_cells = 0

    for cat in categories:
        print(f"Fetching {cat['label']} ({cat['tag']}) ...")
        entry = {"label": cat["label"], "tag": cat["tag"], "areas": {}}

        for area_name, location1 in AREAS:
            area_data = {}

            try:
                area_data["salary_trend"] = fetch_salary_trend(cat["tag"], location1)
            except requests.RequestException as e:
                print(f"  [{area_name}] salary trend failed: {e}")
                area_data["salary_trend"] = {}

            try:
                area_data["vacancy_count"] = fetch_vacancy_count(cat["tag"], location1)
            except requests.RequestException as e:
                print(f"  [{area_name}] vacancy count failed: {e}")
                area_data["vacancy_count"] = None

            total_cells += 2
            if not area_data["salary_trend"]:
                empty_cells += 1
            if area_data["vacancy_count"] is None:
                empty_cells += 1

            entry["areas"][area_name] = area_data

        snapshot["sectors"].append(entry)

    fraction_empty = empty_cells / total_cells if total_cells else 1.0
    print(f"\n{empty_cells}/{total_cells} data cells empty ({fraction_empty:.0%})")
    if fraction_empty > MAX_EMPTY_FRACTION:
        sys.exit(
            f"Too many empty cells ({fraction_empty:.0%} > "
            f"{MAX_EMPTY_FRACTION:.0%}) — failing rather than committing a "
            f"snapshot full of holes. Check the log above; if it's mostly "
            f"503s, the run is being rate-limited and REQUEST_DELAY / "
            f"BACKOFF_BASE need raising."
        )

    return snapshot


def prune_old_snapshots():
    snapshots = sorted(DATA_DIR.glob("snapshot_*.json"))
    for old in snapshots[:-KEEP_SNAPSHOTS]:
        old.unlink()
        print(f"Pruned {old}")


def save_snapshot(snapshot):
    DATA_DIR.mkdir(exist_ok=True)

    dated_file = DATA_DIR / f"snapshot_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"
    dated_file.write_text(json.dumps(snapshot, indent=2))
    LATEST_FILE.write_text(json.dumps(snapshot, indent=2))

    history = []
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text())

    # Vacancy counts are point-in-time — this accumulating record is the
    # ONLY way to get a demand trend over time, since Adzuna gives a
    # current count but no vacancy history.
    history.append(
        {
            "collected_at": snapshot["collected_at"],
            "sectors": [
                {
                    "tag": s["tag"],
                    "vacancy_counts": {
                        area: data.get("vacancy_count")
                        for area, data in s["areas"].items()
                    },
                }
                for s in snapshot["sectors"]
            ],
        }
    )
    HISTORY_FILE.write_text(json.dumps(history, indent=2))

    prune_old_snapshots()
    print(f"Saved {dated_file}, updated {LATEST_FILE} and {HISTORY_FILE}")


def main():
    require_credentials()
    snapshot = build_snapshot()
    save_snapshot(snapshot)


if __name__ == "__main__":
    main()
