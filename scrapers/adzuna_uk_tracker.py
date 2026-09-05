#!/usr/bin/env python3
"""
UK Job Market Tracker — Adzuna data collector

Fetches sector categories and, for each one, a real monthly average-salary
trend (UK-wide) via Adzuna's jobs/{country}/history endpoint. Writes a
dated snapshot and updates a rolling history file that the Flutter app
will read.

Confirmed response shape for jobs/gb/history?category=X&location0=UK:
    {
      "month": {"2026-08": 68397.91, "2026-07": 67645.79, ...},
      "location": {"display_name": "UK", "area": ["UK"]},
      "__CLASS__": "Adzuna::API::Response::HistoricalSalary"
    }
There is no separate salary endpoint and no "locations" list when a
category is included — that was the bug in the previous version.

Credentials come from environment variables — never hardcode them here,
especially since this repo is likely going to be public (GitHub Pages
free tier requires a public repo).

Required env vars:
    ADZUNA_APP_ID
    ADZUNA_APP_KEY

Local run:
    export ADZUNA_APP_ID=xxxx
    export ADZUNA_APP_KEY=xxxx
    python adzuna_uk_tracker.py

In GitHub Actions, set these as repo secrets (Settings > Secrets and
variables > Actions) and pass them into the job's `env:` block — see
job-tracker.yml.
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

# Region-level breakdown (category x region) is NOT fetched yet — that's
# 30 categories x ~12 regions = 300+ extra calls per run, and we haven't
# confirmed the free-tier daily call limit on this account. Flip this on
# once that's checked, and the fetch_category_trend() calls below can be
# looped per-region using the list fetch_uk_regions() returns.
INCLUDE_REGIONAL_BREAKDOWN = False

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


def fetch_uk_regions():
    """
    Top-level UK regions. Only works with NO category filter — adding
    category switches the response to a salary-trend dict instead of a
    locations list (see module docstring).
    """
    data = get(f"{BASE}/history", {"location0": "UK"})
    locations = data.get("locations", [])
    return [loc["location"]["display_name"] for loc in locations]


def fetch_category_trend(category_tag, location1=None):
    """Monthly average-salary trend for a category, UK-wide or one region."""
    params = {"category": category_tag, "location0": "UK"}
    if location1:
        params["location1"] = location1
    data = get(f"{BASE}/history", params)
    return data.get("month", {})


def build_snapshot():
    categories = fetch_categories()
    regions = []
    try:
        regions = fetch_uk_regions()
    except requests.RequestException as e:
        print(f"region list fetch failed: {e}")

    snapshot = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "uk_regions": regions,
        "sectors": [],
    }

    for cat in categories:
        print(f"Fetching {cat['label']} ({cat['tag']}) ...")
        try:
            uk_trend = fetch_category_trend(cat["tag"])
        except requests.RequestException as e:
            print(f"  trend fetch failed: {e}")
            uk_trend = {}

        sector_entry = {
            "label": cat["label"],
            "tag": cat["tag"],
            "salary_trend_uk": uk_trend,
        }

        if INCLUDE_REGIONAL_BREAKDOWN:
            sector_entry["salary_trend_by_region"] = {}
            for region in regions:
                try:
                    sector_entry["salary_trend_by_region"][region] = fetch_category_trend(
                        cat["tag"], location1=region
                    )
                except requests.RequestException as e:
                    print(f"  {region} trend fetch failed: {e}")

        snapshot["sectors"].append(sector_entry)

    return snapshot


def save_snapshot(snapshot):
    DATA_DIR.mkdir(exist_ok=True)

    dated_file = DATA_DIR / f"snapshot_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"
    dated_file.write_text(json.dumps(snapshot, indent=2))
    LATEST_FILE.write_text(json.dumps(snapshot, indent=2))

    history = []
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text())

    history.append(
        {
            "collected_at": snapshot["collected_at"],
            "sectors": [
                {"tag": s["tag"], "salary_trend_uk": s["salary_trend_uk"]}
                for s in snapshot["sectors"]
            ],
        }
    )
    HISTORY_FILE.write_text(json.dumps(history, indent=2))

    print(f"Saved {dated_file}, updated {LATEST_FILE} and {HISTORY_FILE}")


def main():
    require_credentials()
    snapshot = build_snapshot()
    save_snapshot(snapshot)


if __name__ == "__main__":
    main()
