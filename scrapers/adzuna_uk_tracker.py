#!/usr/bin/env python3
"""
UK Job Market Tracker — Adzuna data collector

Fetches sector categories, current regional vacancy breakdown per sector,
and historical salary trend per sector, then writes a dated snapshot and
updates a rolling history file that the Flutter app will read.

Credentials come from environment variables — never hardcode them here,
especially since this repo is likely going to be public (GitHub Pages
free tier requires a public repo).

Required env vars:
    ADZUNA_APP_ID
    ADZUNA_APP_KEY

Local run:
    export ADZUNA_APP_ID=d29b54a5
    export ADZUNA_APP_KEY=cc9007ac1a9e262c15315550386ce309
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
SALARY_BASE = f"https://api.adzuna.com/v1/api/salary/{COUNTRY}"

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


def fetch_regional_breakdown(category_tag):
    """
    Current vacancy counts by top-level UK region, for one category.

    NOTE: per Adzuna's published "Regional data" docs this uses the
    /history path with location0=UK (not a separate /geodata endpoint) —
    it returns child locations of "UK" with their job counts. This
    hasn't been live-tested from this environment (api.adzuna.com isn't
    reachable from this sandbox), so smoke-test one call by hand before
    relying on the scheduled run — check developer.adzuna.com/activedocs
    if the shape doesn't match.
    """
    data = get(f"{BASE}/history", {"category": category_tag, "location0": "UK"})
    locations = data.get("locations", [])
    return [
        {"region": loc["location"]["display_name"], "count": loc["count"]}
        for loc in locations
    ]


def fetch_salary_trend(title):
    """Historical average salary trend for a representative job title."""
    data = get(f"{SALARY_BASE}/history", {"title_only": title})
    return data.get("month", data)


def build_snapshot():
    categories = fetch_categories()
    snapshot = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "sectors": [],
    }

    for cat in categories:
        rep_title = cat["label"].replace(" Jobs", "").strip()
        print(f"Fetching {cat['label']} ({cat['tag']}) ...")

        try:
            regional = fetch_regional_breakdown(cat["tag"])
        except requests.RequestException as e:
            print(f"  regional fetch failed: {e}")
            regional = []

        try:
            salary_trend = fetch_salary_trend(rep_title)
        except requests.RequestException as e:
            print(f"  salary fetch failed: {e}")
            salary_trend = {}

        snapshot["sectors"].append(
            {
                "label": cat["label"],
                "tag": cat["tag"],
                "representative_title": rep_title,
                "regional_vacancies": regional,
                "salary_trend": salary_trend,
            }
        )

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
                {"tag": s["tag"], "regional_vacancies": s["regional_vacancies"]}
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
