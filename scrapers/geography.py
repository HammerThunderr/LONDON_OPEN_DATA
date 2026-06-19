#!/usr/bin/env python3
"""
geography.py — the multi-city geography spine.

Produces data/geography.json: the canonical lookup every other scraper joins
to. Now multi-city: each local authority is tagged with the city it belongs to
(per scrapers/cities.py), and LSOA-level sources roll up to LAD via the maps
below.

Two ONS sources:
  1. LSOA(2021) -> Ward -> LAD best-fit lookup  (the LSOA rollup spine)
  2. LAD -> Combined Authority lookup           (assigns LADs to city regions)

London is matched by E09 prefix (no CAUTH); other cities by their CAUTH code.

Vintages bump yearly and column names carry the year (WD25CD, LAD25CD,
CAUTH24CD...), so columns are detected by PATTERN, not hardcoded.
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

from cities import CITIES

LSOA_ITEM_ID = "f29a49574cc84f6d8e6e59ce2d8efb18"
LSOA_URL = (f"https://open-geography-portalx-ons.hub.arcgis.com"
            f"/api/download/v1/items/{LSOA_ITEM_ID}/csv?layers=0")

# LAD -> Combined Authority lookup (England). Served from data.gov.uk CKAN,
# which gives permanent resource-UUID download URLs (far more stable than the
# ArcGIS service-name guessing). Newest first; columns auto-detect by pattern,
# so when ONS publishes a newer vintage just prepend its CSV resource UUID.
CKAN_RESOURCE = "https://ckan.publishing.service.gov.uk/dataset/{ds}/resource/{rid}/download"
CAUTH_SOURCES = [
    # May 2024 LAD->CAUTH, CSV resource (LAD24CD, LAD24NM, CAUTH24CD, CAUTH24NM)
    ("local-authority-district-to-combined-authority-may-2024-lookup-in-en",
     "b7b39ed9-e6b2-4036-87b5-d8c40b9ff057"),
]

HEADERS = {"User-Agent": "LONDON_OPEN_DATA pipeline (github.com/HammerThunderr)"}
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "geography.json"

LSOA_COLS = {
    "lsoa_cd": re.compile(r"^LSOA\d+CD$"),
    "ward_cd": re.compile(r"^WD\d+CD$"),
    "ward_nm": re.compile(r"^WD\d+NM$"),
    "lad_cd": re.compile(r"^LAD\d+CD$"),
    "lad_nm": re.compile(r"^LAD\d+NM$"),
}
CAUTH_COLS = {
    "lad_cd": re.compile(r"^LAD\d+CD$"),
    "cauth_cd": re.compile(r"^CAUTH\d+CD$"),
}


def _resolve(header, patterns):
    out = {}
    for key, pat in patterns.items():
        hit = [h for h in header if pat.match(h)]
        if not hit:
            raise SystemExit(f"Column '{key}' not found in header: {header}")
        out[key] = hit[0]
    return out


def _fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=120)
    r.raise_for_status()
    return r.content.decode("utf-8-sig")


def _read(path_or_none, url):
    if path_or_none:
        return Path(path_or_none).read_text(encoding="utf-8-sig")
    print(f"Fetching {url}")
    return _fetch(url)


def _fetch_cauth() -> str:
    """Fetch the LAD->CAUTH lookup from data.gov.uk CKAN (stable resource URLs)."""
    for ds, rid in CAUTH_SOURCES:
        url = CKAN_RESOURCE.format(ds=ds, rid=rid)
        try:
            r = requests.get(url, headers=HEADERS, timeout=120, allow_redirects=True)
            if r.status_code == 200 and "CAUTH" in r.text[:4000].upper():
                print(f"  CAUTH source: {ds}")
                return r.content.decode("utf-8-sig")
            print(f"  CAUTH {ds}: status {r.status_code}")
        except requests.RequestException as e:
            print(f"  CAUTH {ds}: {e}")
    raise SystemExit("Could not fetch the LAD->CAUTH lookup from CKAN.")


def lad_to_city(cauth_text: str):
    reader = csv.DictReader(io.StringIO(cauth_text))
    cols = _resolve(reader.fieldnames or [], CAUTH_COLS)
    cauth_to_city = {c["match"]["value"]: c["id"]
                     for c in CITIES if c["match"]["type"] == "cauth"}
    mapping = {}
    for row in reader:
        lad = (row[cols["lad_cd"]] or "").strip()
        cauth = (row[cols["cauth_cd"]] or "").strip()
        if cauth in cauth_to_city:
            mapping[lad] = cauth_to_city[cauth]
    return mapping


def build(lsoa_text: str, cauth_text: str) -> dict:
    lad_city = lad_to_city(cauth_text)
    prefix_cities = [(c["match"]["value"], c["id"])
                     for c in CITIES if c["match"]["type"] == "prefix"]

    reader = csv.DictReader(io.StringIO(lsoa_text))
    cols = _resolve(reader.fieldnames or [], LSOA_COLS)

    def city_of(lad_cd):
        if lad_cd in lad_city:
            return lad_city[lad_cd]
        for prefix, cid in prefix_cities:
            if lad_cd.startswith(prefix):
                return cid
        return None

    lads, wards = {}, {}
    lsoa_to_ward, lsoa_to_lad = {}, {}

    for row in reader:
        lad_cd = (row[cols["lad_cd"]] or "").strip()
        city = city_of(lad_cd)
        if not city:
            continue
        lad_nm = row[cols["lad_nm"]].strip()
        wd_cd = row[cols["ward_cd"]].strip()
        wd_nm = row[cols["ward_nm"]].strip()
        ls_cd = row[cols["lsoa_cd"]].strip()
        b = lads.setdefault(lad_cd, {"code": lad_cd, "name": lad_nm,
                                     "city": city, "_lsoas": set()})
        b["_lsoas"].add(ls_cd)
        wards.setdefault(wd_cd, {"code": wd_cd, "name": wd_nm,
                                 "lad_code": lad_cd, "city": city})
        lsoa_to_ward[ls_cd] = wd_cd
        lsoa_to_lad[ls_cd] = lad_cd

    by_city = {}
    borough_list = []
    for b in sorted(lads.values(), key=lambda x: (x["city"], x["name"])):
        by_city[b["city"]] = by_city.get(b["city"], 0) + 1
        borough_list.append({"code": b["code"], "name": b["name"],
                             "city": b["city"], "lsoa_count": len(b["_lsoas"])})

    return {
        "source": {"lsoa": LSOA_URL, "cauth": CKAN_RESOURCE.format(
            ds=CAUTH_SOURCES[0][0], rid=CAUTH_SOURCES[0][1])},
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cities": [{"id": c["id"], "name": c["name"], "has_ptal": c["has_ptal"],
                    "lad_count": by_city.get(c["id"], 0)} for c in CITIES],
        "boroughs": borough_list,
        "wards": [{"code": w["code"], "name": w["name"],
                   "lad_code": w["lad_code"], "city": w["city"]}
                  for w in sorted(wards.values(), key=lambda x: (x["city"], x["name"]))],
        "lsoa_to_ward": lsoa_to_ward,
        "lsoa_to_borough": lsoa_to_lad,
        "lsoa_to_lad": lsoa_to_lad,
    }


def main() -> None:
    lsoa_arg = sys.argv[1] if len(sys.argv) > 1 else None
    cauth_arg = sys.argv[2] if len(sys.argv) > 2 else None
    lsoa_text = _read(lsoa_arg, LSOA_URL)
    cauth_text = Path(cauth_arg).read_text(encoding="utf-8-sig") if cauth_arg else _fetch_cauth()
    result = build(lsoa_text, cauth_text)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote geography.json: {len(result['boroughs'])} LADs across "
          f"{len(result['cities'])} cities, {len(result['lsoa_to_lad'])} LSOAs")
    for c in result["cities"]:
        flag = "" if c["lad_count"] else "  <-- WARNING: 0 LADs matched"
        print(f"  {c['name']}: {c['lad_count']} LADs{flag}")


if __name__ == "__main__":
    main()
