#!/usr/bin/env python3
"""
air_quality.py — population-weighted PM2.5 per borough.

Source: Defra UK-AIR, Pollution Climate Mapping (PCM) model — "population-
weighted annual mean PM2.5 concentration by UK local authority" (OGL).
  https://uk-air.defra.gov.uk/data/pcm-data
Updated each autumn (~October) for the previous calendar year.

Why this measure: population-weighted means the concentration is averaged
where people actually live, so it directly reflects residents' exposure —
exactly the "air quality if I live here" question. PM2.5 is the pollutant
with the strongest health evidence. We use the TOTAL column (residents
breathe total PM2.5, not just the anthropogenic share).

File format (verified):
  row 1: title, row 2: note, row 3 header:
    LA code, PM2.5 YYYY (total), PM2.5 YYYY (non-anthropogenic),
    PM2.5 YYYY (anthropogenic), Local Authority
The LA code is an old numeric code (not ONS E09), so boroughs are matched by
NAME against the geography spine, like the crime scraper's BOCU matching.

URL pattern:
  https://uk-air.defra.gov.uk/datastore/pcm/popwmpm25{YEAR}byUKlocalauthority.csv
We probe recent years newest-first and take the first that exists.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

URL_TMPL = ("https://uk-air.defra.gov.uk/datastore/pcm/"
            "popwmpm25{year}byUKlocalauthority.csv")
HEADERS = {"User-Agent": "LONDON_OPEN_DATA pipeline (github.com/HammerThunderr)"}

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "air_quality.json"


def norm(name: str) -> str:
    return name.strip().lower().replace("&", "and")


def find_csv() -> tuple[str, int]:
    """Probe recent years, newest first; the new file lands each autumn."""
    this_year = date.today().year
    for year in range(this_year - 1, this_year - 6, -1):
        url = URL_TMPL.format(year=year)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=120)
        except requests.RequestException as e:
            print(f"  {year}: request failed ({e})")
            continue
        if resp.status_code == 200 and resp.content[:1] != b"<":  # not an error page
            return resp.content.decode("utf-8-sig", errors="replace"), year
        print(f"  {year}: status {resp.status_code}")
    raise SystemExit("No population-weighted PM2.5 file found for recent years.")


def build(csv_text: str, year: int, geography: dict) -> dict:
    rows = list(csv.reader(io.StringIO(csv_text)))

    # find the header row: the one starting with "LA code"
    header_i = next((i for i, r in enumerate(rows)
                     if r and r[0].strip().lower() == "la code"), None)
    if header_i is None:
        print(f"  first rows were: {rows[:4]}")
        raise SystemExit("Could not find the 'LA code' header row.")
    header = [c.strip() for c in rows[header_i]]

    def col(*words: str) -> int | None:
        for i, h in enumerate(header):
            hl = h.lower()
            if all(w in hl for w in words):
                return i
        return None

    total_col = col("total")
    name_col = col("local authority")
    if total_col is None or name_col is None:
        raise SystemExit(f"Unexpected header: {header}")

    name_to_code = {norm(b["name"]): (b["code"], b["name"])
                    for b in geography["boroughs"]}

    boroughs, seen = [], set()
    for r in rows[header_i + 1:]:
        if len(r) <= max(total_col, name_col):
            continue
        hit = name_to_code.get(norm(r[name_col]))
        if not hit:
            continue
        code, name = hit
        try:
            v = float(r[total_col])
        except ValueError:  # 'MISSING' or blank
            continue
        boroughs.append({"code": code, "name": name, "pm25_total": round(v, 2)})
        seen.add(code)
    boroughs.sort(key=lambda b: b["name"])

    return {
        "source": URL_TMPL.format(year=year),
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "year": year,
        "note": ("Defra PCM model: population-weighted annual mean PM2.5 "
                 "(total), µg/m³. Weighted to where residents live."),
        "boroughs": boroughs,
    }


def main() -> None:
    geography = json.loads((DATA_DIR / "geography.json").read_text(encoding="utf-8"))

    if len(sys.argv) > 1:  # offline test: python scrapers/air_quality.py file.csv [year]
        csv_text = Path(sys.argv[1]).read_text(encoding="utf-8-sig")
        year = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        print(f"Using local file {sys.argv[1]}")
    else:
        csv_text, year = find_csv()
        print(f"Using {URL_TMPL.format(year=year)}")

    result = build(csv_text, year, geography)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    vals = [b["pm25_total"] for b in result["boroughs"]]
    mean = sum(vals) / len(vals) if vals else 0
    print(f"Wrote air_quality.json: {len(vals)} boroughs, year {year}, "
          f"mean {mean:.1f} µg/m³ (range {min(vals):.1f}–{max(vals):.1f})")
    if len(vals) != 33:
        print(f"  WARNING: expected 33 boroughs, got {len(vals)} — "
              f"check name matching.")
    if vals and not 4 <= mean <= 25:
        print("  WARNING: mean outside plausible London PM2.5 range — "
              "check the column picked.")


if __name__ == "__main__":
    main()
