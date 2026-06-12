#!/usr/bin/env python3
"""
geography.py — the geography spine.

Produces data/geography.json: the canonical LSOA -> Ward -> Borough lookup that
EVERY other scraper joins against. Crime, PTAL, air quality and green space all
arrive at LSOA level and roll up to ward/borough via the lsoa_to_* maps below.
Rent and council tax arrive at borough level and key onto the borough codes.

Source: ONS Open Geography Portal
  "LSOA (2021) to Electoral Ward (YYYY) to LAD (YYYY) Best Fit Lookup in EW"

The ward/LAD vintage bumps every year and the item id changes with it. When it
does, find the latest on the portal and update ITEM_ID. The column names carry
the vintage (WD25CD, LAD25CD, ...) so we DETECT them by pattern rather than
hardcode the year — that is what stops this breaking each spring.
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

# 2025 vintage lookup (LSOA 2021 -> Ward 2025 -> LAD 2025), England & Wales.
ITEM_ID = "f29a49574cc84f6d8e6e59ce2d8efb18"
SOURCE = (
    f"https://open-geography-portalx-ons.hub.arcgis.com"
    f"/api/download/v1/items/{ITEM_ID}/csv?layers=0"
)

# London boroughs are the only LADs with an E09 code, so this one prefix is the
# entire "is this London?" test. 32 boroughs + City of London = 33.
LONDON_LAD_PREFIX = "E09"

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "geography.json"

COLS = {
    "lsoa_cd": re.compile(r"^LSOA\d+CD$"),
    "lsoa_nm": re.compile(r"^LSOA\d+NM$"),
    "ward_cd": re.compile(r"^WD\d+CD$"),
    "ward_nm": re.compile(r"^WD\d+NM$"),   # NM (English); deliberately not NMW
    "lad_cd": re.compile(r"^LAD\d+CD$"),
    "lad_nm": re.compile(r"^LAD\d+NM$"),
}


def resolve_columns(header: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, pattern in COLS.items():
        matches = [h for h in header if pattern.match(h)]
        if not matches:
            raise SystemExit(
                f"Could not find a column for '{key}' in header: {header}\n"
                f"The lookup layout may have changed — check the source."
            )
        found[key] = matches[0]
    return found


def detect_vintage(cols: dict[str, str]) -> dict[str, str]:
    def yr(name: str) -> str:
        m = re.search(r"\d+", name)
        return m.group(0) if m else "?"
    return {"lsoa": yr(cols["lsoa_cd"]), "ward": yr(cols["ward_cd"]), "lad": yr(cols["lad_cd"])}


def fetch_csv(url: str) -> str:
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    return resp.content.decode("utf-8-sig")


def build(csv_text: str) -> dict:
    reader = csv.DictReader(io.StringIO(csv_text))
    cols = resolve_columns(reader.fieldnames or [])

    boroughs: dict[str, dict] = {}
    wards: dict[str, dict] = {}
    lsoa_to_ward: dict[str, str] = {}
    lsoa_to_borough: dict[str, str] = {}

    for row in reader:
        lad_cd = (row[cols["lad_cd"]] or "").strip()
        if not lad_cd.startswith(LONDON_LAD_PREFIX):
            continue

        lad_nm = row[cols["lad_nm"]].strip()
        wd_cd = row[cols["ward_cd"]].strip()
        wd_nm = row[cols["ward_nm"]].strip()
        ls_cd = row[cols["lsoa_cd"]].strip()

        b = boroughs.setdefault(
            lad_cd, {"code": lad_cd, "name": lad_nm, "_wards": set(), "_lsoas": set()}
        )
        b["_wards"].add(wd_cd)
        b["_lsoas"].add(ls_cd)

        w = wards.setdefault(
            wd_cd,
            {"code": wd_cd, "name": wd_nm, "borough_code": lad_cd,
             "borough_name": lad_nm, "_lsoas": set()},
        )
        w["_lsoas"].add(ls_cd)

        lsoa_to_ward[ls_cd] = wd_cd
        lsoa_to_borough[ls_cd] = lad_cd

    borough_list = [
        {"code": b["code"], "name": b["name"],
         "ward_count": len(b["_wards"]), "lsoa_count": len(b["_lsoas"])}
        for b in sorted(boroughs.values(), key=lambda x: x["name"])
    ]
    ward_list = [
        {"code": w["code"], "name": w["name"],
         "borough_code": w["borough_code"], "borough_name": w["borough_name"],
         "lsoa_count": len(w["_lsoas"])}
        for w in sorted(wards.values(), key=lambda x: (x["borough_name"], x["name"]))
    ]

    # Flat envelope, same shape convention as the rest of the repo:
    # source / scraped_at / counts at the top, then the body.
    return {
        "source": SOURCE,
        "source_item_id": ITEM_ID,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vintage": detect_vintage(cols),
        "counts": {
            "boroughs": len(borough_list),
            "wards": len(ward_list),
            "lsoas": len(lsoa_to_ward),
        },
        "boroughs": borough_list,
        "wards": ward_list,
        "lsoa_to_ward": lsoa_to_ward,
        "lsoa_to_borough": lsoa_to_borough,
    }


def main() -> None:
    # Allow `python scrapers/geography.py path/to/local.csv` for offline testing.
    if len(sys.argv) > 1:
        csv_text = Path(sys.argv[1]).read_text(encoding="utf-8")
    else:
        print(f"Fetching lookup from {SOURCE}")
        csv_text = fetch_csv(SOURCE)

    result = build(csv_text)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    c, v = result["counts"], result["vintage"]
    print(
        f"Wrote {OUT_PATH.name}: {c['boroughs']} boroughs, {c['wards']} wards, "
        f"{c['lsoas']} LSOAs  (vintage LSOA{v['lsoa']} / WD{v['ward']} / LAD{v['lad']})"
    )
    if c["boroughs"] != 33:
        print(f"  WARNING: expected 33 London boroughs, got {c['boroughs']}.")


if __name__ == "__main__":
    main()
