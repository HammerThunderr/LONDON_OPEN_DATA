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

# Fail the run if more than this fraction of sector fetches come back
# empty. Two earlier runs of this script reported success while
# collecting nothing at all, because every failure was caught and logged
# rather than raised — this guard makes that show up as a red build.
MAX_EMPTY_FRACTION = 0.33

# Keep only the N most recent dated snapshot files; latest.json and
# history.json are what the app actually reads.
KEEP_SNAPSHOTS = 12

DATA_DIR = Path("data")
HISTORY_FILE = DATA_DIR / "history.json"
LATEST_FILE = DATA_DIR / "latest.json"

REQUEST_DELAY = 1.0  # be polite to the API between calls


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
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return resp.json()


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

    empty_sectors = 0

    for cat in categories:
        print(f"Fetching {cat['label']} ({cat['tag']}) ...")
        entry = {"label": cat["label"], "tag": cat["tag"], "areas": {}}
        got_something = False

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

            if area_data["salary_trend"] or area_data["vacancy_count"]:
                got_something = True

            entry["areas"][area_name] = area_data

        if not got_something:
            empty_sectors += 1
            print(f"  !! nothing collected for {cat['label']}")

        snapshot["sectors"].append(entry)

    fraction_empty = empty_sectors / len(categories)
    print(f"\n{empty_sectors}/{len(categories)} sectors came back empty "
          f"({fraction_empty:.0%})")
    if fraction_empty > MAX_EMPTY_FRACTION:
        sys.exit(
            f"Too many empty sectors ({fraction_empty:.0%} > "
            f"{MAX_EMPTY_FRACTION:.0%}) — failing the run rather than "
            f"committing a snapshot full of holes. Check the log above "
            f"for the underlying API errors."
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
