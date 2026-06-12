#!/usr/bin/env python3
"""
rent.py — average monthly private rent per borough.

Source: ONS "Price Index of Private Rents, UK: monthly price statistics"
(PIPR — the official series that replaced IPHRP/PRMS; the GLA's London Rents
Map runs on it). Published monthly at local-authority level.

Discovery: every ons.gov.uk page has a JSON twin at <page>/data; the dataset
page's JSON lists the current download file, so we never hardcode a versioned
URL.

Known gap baked in: ONS does not publish City of London rents (sample too
small), so this is a 32-borough metric; the app skips missing metrics.

The xlsx layout isn't version-stable, so parsing is defensive:
- borough rows are found by E09 codes,
- if a bedroom/property-category column exists, rows are filtered to the
  "all ..." category,
- the value column is the LATEST month among date-parseable headers, falling
  back to the rightmost header mentioning rent/price,
- and on failure every header seen is printed for a one-look fix.
"""

from __future__ import annotations

import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

PAGE = ("https://www.ons.gov.uk/economy/inflationandpriceindices/datasets/"
        "priceindexofprivaterentsukmonthlypricestatistics")
HEADERS = {"User-Agent": "LONDON_OPEN_DATA pipeline (github.com/HammerThunderr)"}

E09_RE = re.compile(r"^E09\d{6}$")
ALL_CATEGORY_RE = re.compile(r"^all\b", re.I)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "rent.json"


def find_download_url() -> str:
    resp = requests.get(f"{PAGE}/data", headers=HEADERS, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    downloads = payload.get("downloads") or []
    for d in downloads:
        f = d.get("file") or ""
        if f.lower().endswith((".xlsx", ".xls")):
            return f if f.startswith("http") else f"https://www.ons.gov.uk/file?uri={f}"
    print(f"  page JSON keys: {list(payload.keys())}")
    print(f"  downloads entries: {downloads}")
    raise SystemExit("No xlsx download found in the ONS dataset page JSON.")


def _label_date(label: str):
    """Try to read a column label as a month (e.g. 'Feb 2026', '2026-02')."""
    try:
        ts = pd.to_datetime(str(label).strip(), errors="coerce")
        return None if pd.isna(ts) else ts
    except Exception:
        return None


def parse_xlsx(content: bytes, geography: dict) -> tuple[dict[str, float], str]:
    """Returns ({borough_code: monthly_rent}, value_label)."""
    sheets = pd.read_excel(io.BytesIO(content), engine="openpyxl",
                           sheet_name=None, header=None)
    valid = {b["code"] for b in geography["boroughs"]}

    for sheet_name, df in sheets.items():
        df = df.astype(object).where(pd.notna(df), None)

        code_col = None
        for c in df.columns:
            col = df[c].map(lambda v: isinstance(v, str) and bool(E09_RE.match(v.strip())))
            if col.sum() >= 25:
                code_col = c
                break
        if code_col is None:
            continue

        rows = [i for i in df.index
                if isinstance(df.at[i, code_col], str)
                and E09_RE.match(df.at[i, code_col].strip())]
        first = min(rows)

        # flattened header labels from up to 6 rows above the data
        labels: dict[int, str] = {}
        for c in df.columns:
            parts = [str(df.at[r, c]).strip()
                     for r in range(max(0, first - 6), first)
                     if df.at[r, c] is not None and str(df.at[r, c]).strip()]
            labels[c] = " / ".join(parts)

        # optional category column (bedrooms / property type): keep "All ..."
        cat_col = None
        for c in df.columns:
            vals = {str(df.at[i, c]).strip().lower() for i in rows[:300]
                    if isinstance(df.at[i, c], str)}
            if any(v.startswith("all") for v in vals) and len(vals) > 1:
                cat_col = c
                break
        if cat_col is not None:
            rows = [i for i in rows
                    if isinstance(df.at[i, cat_col], str)
                    and ALL_CATEGORY_RE.match(df.at[i, cat_col].strip())]
            print(f"  sheet '{sheet_name}': filtering category column "
                  f"{labels.get(cat_col) or cat_col!r} to 'All ...' "
                  f"({len(rows)} rows kept)")

        # value column: latest date-labelled column, else rightmost rent/price
        dated = [(d, c) for c, l in labels.items()
                 if (d := _label_date(l)) is not None]
        if dated:
            dated.sort()
            val_col, val_label = dated[-1][1], labels[dated[-1][1]]
        else:
            rentish = [c for c, l in labels.items()
                       if any(w in l.lower() for w in ("rent", "price"))]
            if not rentish:
                print(f"  sheet '{sheet_name}': E09 rows but no usable value "
                      f"column. Headers seen:")
                for c, l in labels.items():
                    if l:
                        print(f"    col {c}: {l}")
                continue
            val_col, val_label = rentish[-1], labels[rentish[-1]]

        out: dict[str, float] = {}
        for i in rows:
            code = df.at[i, code_col].strip()
            if code not in valid:
                continue
            try:
                v = float(df.at[i, val_col])
            except (TypeError, ValueError):
                continue
            if 200 <= v <= 8000:  # sanity: monthly £ not an index value
                out[code] = round(v, 0)
        if len(out) >= 25:
            return out, val_label
        if out:
            print(f"  sheet '{sheet_name}': only {len(out)} boroughs parsed; "
                  f"trying next sheet.")

    raise SystemExit(
        "Could not extract borough rents from any sheet — see the header "
        "listings above and adjust parse_xlsx() in this script."
    )


def main() -> None:
    geography = json.loads((DATA_DIR / "geography.json").read_text(encoding="utf-8"))
    name_by_code = {b["code"]: b["name"] for b in geography["boroughs"]}

    if len(sys.argv) > 1:  # offline test: python scrapers/rent.py file.xlsx
        content = Path(sys.argv[1]).read_bytes()
        src = sys.argv[1]
        print(f"Using local file {src}")
    else:
        url = find_download_url()
        print(f"Fetching {url}")
        resp = requests.get(url, headers=HEADERS, timeout=300)
        resp.raise_for_status()
        content = resp.content
        src = url

    values, label = parse_xlsx(content, geography)

    boroughs = [
        {"code": code, "name": name_by_code[code], "monthly_rent": v}
        for code, v in sorted(values.items(), key=lambda kv: name_by_code[kv[0]])
    ]
    mean = sum(values.values()) / len(values)

    OUT_PATH.write_text(json.dumps({
        "source": PAGE,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "value_label": label,
        "note": ("ONS Price Index of Private Rents (PIPR): average monthly "
                 "private rent, all categories. City of London not published "
                 "by ONS (low sample)."),
        "boroughs": boroughs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote rent.json: {len(boroughs)} boroughs, London mean £{mean:.0f} "
          f"(column: {label!r})")
    if len(boroughs) != 32:
        print(f"  NOTE: expected 32 boroughs (City of London unpublished), "
              f"got {len(boroughs)}.")
    if not 1200 <= mean <= 3500:
        print(f"  WARNING: London mean £{mean:.0f} looks implausible — "
              f"check the column picked.")


if __name__ == "__main__":
    main()
