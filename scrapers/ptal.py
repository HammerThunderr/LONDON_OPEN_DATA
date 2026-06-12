#!/usr/bin/env python3
"""
ptal.py — public transport accessibility (PTAL) per borough.

Source: TfL GIS Open Data Hub (ArcGIS), "LSOA aggregated PTAL stats <year>"
  https://gis-tfl.opendata.arcgis.com

Discovery: the hub's DCAT catalogue feed lists every dataset with download
URLs, so we match titles containing "LSOA" + "PTAL", take the latest year,
and pick the CSV distribution. No hardcoded item ids to rot.

PTAL grades run 0, 1a, 1b, 2, 3, 4, 5, 6a, 6b (best). We map them to the
conventional numeric reading (1a=1, 1b=1.5, ... 6a=6, 6b=6.5), average the
LSOA mean values per borough via the geography spine, and emit `ptal_mean`.
LSOAs hold roughly equal population (~1,700 people), so an unweighted mean
across LSOAs approximates a population-weighted borough score.

The CSV layout isn't verified in advance, so parsing is defensive and noisy:
the LSOA column is found by E01-code pattern, the value column by 'mean' in
its header, and on failure every header seen is printed.
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

HUB = "https://gis-tfl.opendata.arcgis.com"
DCAT_FEEDS = [f"{HUB}/api/feed/dcat-us/1.1.json", f"{HUB}/data.json"]
HEADERS = {"User-Agent": "LONDON_OPEN_DATA pipeline (github.com/HammerThunderr)"}

TITLE_WORDS = ("lsoa", "ptal")
E01_RE = re.compile(r"^E01\d{6}$")
YEAR_RE = re.compile(r"(20\d{2})")

GRADE_VALUES = {
    "0": 0.0, "1A": 1.0, "1B": 1.5, "2": 2.0, "3": 3.0,
    "4": 4.0, "5": 5.0, "6A": 6.0, "6B": 6.5,
}

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "ptal.json"


def find_csv_url() -> tuple[str, str]:
    """Return (csv_url, dataset_title) for the newest LSOA PTAL dataset."""
    last_titles: list[str] = []
    for feed in DCAT_FEEDS:
        try:
            resp = requests.get(feed, headers=HEADERS, timeout=120)
            if resp.status_code != 200:
                print(f"  feed {feed} -> status {resp.status_code}")
                continue
            catalogue = resp.json().get("dataset") or []
        except (requests.RequestException, ValueError) as e:
            print(f"  feed {feed} failed: {e}")
            continue

        matches = []
        for d in catalogue:
            title = d.get("title") or ""
            t = title.lower()
            last_titles.append(title)
            if all(w in t for w in TITLE_WORDS):
                m = YEAR_RE.search(title)
                matches.append((int(m.group(1)) if m else 0, title, d))
        if not matches:
            continue
        matches.sort(key=lambda m: (m[0], m[1]))
        year, title, d = matches[-1]

        csv_url = geojson_url = None
        for dist in d.get("distribution") or []:
            url = dist.get("downloadURL") or dist.get("accessURL") or ""
            fmt = (dist.get("mediaType") or dist.get("format") or "").lower()
            if "csv" in fmt or url.lower().endswith("csv"):
                csv_url = url
            elif "geojson" in fmt or "geojson" in url.lower():
                geojson_url = url
        url = csv_url or geojson_url
        if url:
            return url, title
        print(f"  matched '{title}' but found no CSV/GeoJSON distribution.")

    ptalish = [t for t in last_titles if "ptal" in t.lower()]
    print(f"  PTAL-ish dataset titles seen in catalogue: {ptalish[:15]}")
    raise SystemExit("Could not locate an LSOA PTAL dataset in the TfL hub catalogue.")


def to_numeric(value) -> float | None:
    """Accept either numeric means or PTAL grade strings."""
    if value is None:
        return None
    s = str(value).strip().upper()
    if not s:
        return None
    if s in GRADE_VALUES:
        return GRADE_VALUES[s]
    try:
        return float(s)
    except ValueError:
        return None


def parse_rows(rows: list[dict], geography: dict) -> dict[str, list[float]]:
    """rows: list of flat dicts (CSV rows or GeoJSON properties).
    Returns borough_code -> list of LSOA values."""
    if not rows:
        raise SystemExit("Dataset was empty.")
    header = list(rows[0].keys())

    lsoa_col = None
    for col in header:
        hits = sum(1 for r in rows[:200]
                   if isinstance(r.get(col), str) and E01_RE.match(r[col].strip()))
        if hits >= 100:
            lsoa_col = col
            break
    if lsoa_col is None:
        raise SystemExit(f"No LSOA-code column found. Headers: {header}")

    mean_cols = [c for c in header if "mean" in c.lower() or c.lower() in ("avptal", "av_ptal")]
    if not mean_cols:
        print(f"  no 'mean' column found. Headers: {header}")
        raise SystemExit("Adjust the value-column match in this script.")
    if len(mean_cols) > 1:
        print(f"  multiple mean-ish columns {mean_cols}; using {mean_cols[0]!r}")
    val_col = mean_cols[0]
    print(f"  using LSOA column {lsoa_col!r}, value column {val_col!r}")

    lsoa_to_borough = geography["lsoa_to_borough"]
    per_borough: dict[str, list[float]] = {}
    skipped = 0
    for r in rows:
        code = (r.get(lsoa_col) or "").strip()
        v = to_numeric(r.get(val_col))
        b = lsoa_to_borough.get(code)
        if not b or v is None:
            skipped += 1
            continue
        per_borough.setdefault(b, []).append(v)
    if skipped:
        print(f"  skipped {skipped} rows (non-London LSOA or unparseable value)")
    return per_borough


def fetch_rows(url: str) -> list[dict]:
    resp = requests.get(url, headers=HEADERS, timeout=300)
    resp.raise_for_status()
    if "json" in (resp.headers.get("content-type") or "") or url.lower().endswith(("geojson", "json")):
        feats = resp.json().get("features") or []
        return [f.get("properties") or {} for f in feats]
    return list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))


def main() -> None:
    geography = json.loads((DATA_DIR / "geography.json").read_text(encoding="utf-8"))
    name_by_code = {b["code"]: b["name"] for b in geography["boroughs"]}

    if len(sys.argv) > 1:  # offline test: python scrapers/ptal.py file.csv
        rows = list(csv.DictReader(io.StringIO(
            Path(sys.argv[1]).read_text(encoding="utf-8-sig"))))
        src = title = sys.argv[1]
        print(f"Using local file {src}")
    else:
        url, title = find_csv_url()
        print(f"Fetching '{title}'\n  {url}")
        rows = fetch_rows(url)
        src = url

    per_borough = parse_rows(rows, geography)

    boroughs = [
        {"code": code, "name": name_by_code[code],
         "ptal_mean": round(sum(vals) / len(vals), 2), "lsoas": len(vals)}
        for code, vals in sorted(per_borough.items(),
                                 key=lambda kv: name_by_code.get(kv[0], ""))
        if code in name_by_code
    ]

    OUT_PATH.write_text(json.dumps({
        "source": src,
        "dataset": title,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": ("Mean PTAL per borough: unweighted mean of LSOA mean values "
                 "(grades mapped 1a=1, 1b=1.5, ... 6a=6, 6b=6.5). TfL WebCAT."),
        "boroughs": boroughs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    vals = [b["ptal_mean"] for b in boroughs]
    print(f"Wrote ptal.json: {len(boroughs)} boroughs, "
          f"range {min(vals):.2f}–{max(vals):.2f}")
    if len(boroughs) != 33:
        print(f"  WARNING: expected 33 boroughs, got {len(boroughs)}.")
    if not (vals and min(vals) >= 0 and max(vals) <= 6.5):
        print("  WARNING: values outside the 0–6.5 PTAL scale — check the column.")


if __name__ == "__main__":
    main()
