#!/usr/bin/env python3
"""
crime.py — crime counts per borough, from the London Datastore.

Source: "MPS Recorded Crime: Geographic Breakdown" (Metropolitan Police via
London Datastore, OGL, updated monthly):
  https://data.london.gov.uk/dataset/mps-recorded-crime-geographic-breakdown-exy3m

We use the pre-aggregated "MPS Borough Level Crime (most recent 24 months)"
CSV. Each monthly refresh is uploaded as a NEW file with a NEW short URL, so we
parse the dataset page at runtime and take the FIRST matching link (the page
lists newest first) instead of hardcoding a URL.

CSV layout (verified Jun 2026):
  Group,SubGroup,BOCU,202406,202407,...,202605
BOCU is the borough NAME. We sum the most recent 12 month-columns per borough,
map names -> E09 codes via data/geography.json, and emit a per-1,000 rate.

Caveats baked in:
- MPS does not police the City of London, so E09000001 gets no value here.
  scores.py / the app handle missing metrics gracefully.
- Populations below are ONS mid-year estimates ROUNDED to the nearest 1,000,
  embedded as a stopgap. TODO: replace with a population scraper; rounding
  shifts rates by well under 1% and does not materially affect ranking.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://data.london.gov.uk"
DATASET_SLUG = "mps-recorded-crime-geographic-breakdown-exy3m"
DATASET_ID = "exy3m"
DATASET_PAGE = f"{BASE}/dataset/{DATASET_SLUG}"

# DataPress blocks scraping but provides a JSON API; also send a real UA.
HEADERS = {"User-Agent": "LONDON_OPEN_DATA pipeline (github.com/HammerThunderr)"}
WANTED = ("borough level crime", "24 months")

MONTH_COL_RE = re.compile(r"^\d{6}$")
MONTHS_TO_SUM = 12

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "crime.json"

# ONS mid-year population estimates, rounded to nearest 1,000. Stopgap — see
# module docstring. Keyed by borough name as it appears in geography.json.
POPULATIONS = {
    "Barking and Dagenham": 219000, "Barnet": 389000, "Bexley": 247000,
    "Brent": 340000, "Bromley": 330000, "Camden": 210000,
    "City of London": 9000, "Croydon": 390000, "Ealing": 367000,
    "Enfield": 330000, "Greenwich": 291000, "Hackney": 259000,
    "Hammersmith and Fulham": 183000, "Haringey": 264000, "Harrow": 261000,
    "Havering": 262000, "Hillingdon": 309000, "Hounslow": 288000,
    "Islington": 216000, "Kensington and Chelsea": 143000,
    "Kingston upon Thames": 168000, "Lambeth": 317000, "Lewisham": 300000,
    "Merton": 215000, "Newham": 358000, "Redbridge": 310000,
    "Richmond upon Thames": 195000, "Southwark": 311000, "Sutton": 209000,
    "Tower Hamlets": 325000, "Waltham Forest": 278000, "Wandsworth": 328000,
    "Westminster": 205000,
}


def norm(name: str) -> str:
    return name.strip().lower().replace("&", "and")


def _wanted(text: str) -> bool:
    t = (text or "").lower()
    return all(w in t for w in WANTED)


def pick_resource(payload: dict) -> str | None:
    """Pick the newest matching resource URL from a DataPress dataset JSON."""
    res = payload.get("resources") or {}
    items = list(res.values()) if isinstance(res, dict) else list(res)
    matches = [
        r for r in items
        if _wanted(r.get("title", "")) or _wanted(r.get("filename", ""))
    ]
    if not matches:
        return None
    matches.sort(key=lambda r: r.get("timestamp") or r.get("timeFrameTo") or "")
    url = matches[-1].get("url") or ""
    if url.startswith("/"):
        url = BASE + url
    return url or None


def discover_csv_url() -> str:
    # Primary: DataPress JSON API (scraping the HTML page gets blocked).
    for ident in (DATASET_ID, DATASET_SLUG):
        try:
            resp = requests.get(f"{BASE}/api/dataset/{ident}", headers=HEADERS, timeout=60)
            if resp.status_code == 200:
                url = pick_resource(resp.json())
                if url:
                    return url
        except (requests.RequestException, ValueError):
            pass

    # Fallback: tolerant scan of the dataset page HTML.
    from urllib.parse import unquote
    html = requests.get(DATASET_PAGE, headers=HEADERS, timeout=60).text
    candidates = re.findall(
        r'href="((?:https://data\.london\.gov\.uk)?/download/[^"]+\.csv)"', html
    )
    for href in candidates:  # page lists newest first
        if _wanted(unquote(href)):
            return href if href.startswith("http") else BASE + href
    raise SystemExit(
        "Could not locate the 'Borough Level Crime (most recent 24 months)' file "
        "via the DataPress API or the dataset page — check the dataset:\n"
        f"  {DATASET_PAGE}"
    )


def build(csv_text: str, geography: dict) -> dict:
    reader = csv.DictReader(io.StringIO(csv_text))
    header = reader.fieldnames or []
    month_cols = sorted(c for c in header if MONTH_COL_RE.match(c))
    if len(month_cols) < MONTHS_TO_SUM:
        raise SystemExit(f"Expected >= {MONTHS_TO_SUM} month columns, got: {month_cols}")
    recent = month_cols[-MONTHS_TO_SUM:]
    if "BOCU" not in header:
        raise SystemExit(f"Expected a BOCU column; header was: {header}")

    totals: dict[str, int] = {}
    for row in reader:
        bocu = (row["BOCU"] or "").strip()
        if not bocu:
            continue
        s = 0
        for c in recent:
            v = (row.get(c) or "").strip()
            if v.isdigit():
                s += int(v)
        totals[bocu] = totals.get(bocu, 0) + s

    name_to_code = {norm(b["name"]): (b["code"], b["name"]) for b in geography["boroughs"]}

    boroughs, unmatched = [], []
    for bocu, total in totals.items():
        hit = name_to_code.get(norm(bocu))
        if not hit:
            unmatched.append(bocu)
            continue
        code, name = hit
        pop = POPULATIONS.get(name)
        boroughs.append({
            "code": code,
            "name": name,
            "crimes_12mo": total,
            "population_used": pop,
            "rate_per_1000": round(total / pop * 1000, 1) if pop else None,
        })
    boroughs.sort(key=lambda b: b["name"])

    return {
        "source": DATASET_PAGE,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": {"from": recent[0], "to": recent[-1], "months": MONTHS_TO_SUM},
        "note": (
            "MPS data only: the City of London has its own police force and is "
            "not covered. Rates use rounded ONS mid-year population estimates."
        ),
        "unmatched_bocus": sorted(unmatched),
        "boroughs": boroughs,
    }


def main() -> None:
    geo_path = DATA_DIR / "geography.json"
    if not geo_path.exists():
        raise SystemExit(
            "data/geography.json not found — run scrapers/geography.py first "
            "(or trigger the geography workflow)."
        )
    geography = json.loads(geo_path.read_text(encoding="utf-8"))

    if len(sys.argv) > 1:  # offline test: python scrapers/crime.py local.csv
        csv_text = Path(sys.argv[1]).read_text(encoding="utf-8-sig")
        print(f"Using local file {sys.argv[1]}")
    else:
        url = discover_csv_url()
        print(f"Fetching {url}")
        resp = requests.get(url, headers=HEADERS, timeout=120)
        resp.raise_for_status()
        csv_text = resp.content.decode("utf-8-sig")

    result = build(csv_text, geography)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    n = len(result["boroughs"])
    print(f"Wrote crime.json: {n} boroughs, period {result['period']['from']}–{result['period']['to']}")
    if result["unmatched_bocus"]:
        print(f"  NOTE: unmatched BOCUs (skipped): {result['unmatched_bocus']}")
    if n < 32:
        print(f"  WARNING: expected 32 MPS boroughs, got {n}.")


if __name__ == "__main__":
    main()
