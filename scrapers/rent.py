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


def _xlsx_from(payload: dict, base_uri: str = "") -> str | None:
    """Build the download URL. ONS 'file' entries come in three shapes:
    absolute URLs, absolute /paths, or BARE FILENAMES that must be joined
    onto the page's own uri."""
    for d in payload.get("downloads") or []:
        f = (d.get("file") or "").strip()
        if not f.lower().endswith((".xlsx", ".xls")):
            continue
        if f.startswith("http"):
            return f
        if not f.startswith("/"):
            f = f"{base_uri.rstrip('/')}/{f}"
        return f"https://www.ons.gov.uk/file?uri={f}"
    return None


def find_download_url() -> str:
    """ONS pages have a JSON twin at <page>/data. The dataset LANDING page
    carries no downloads itself — its 'datasets' list points at the version
    page that does. Check the landing page first, then follow the children."""
    resp = requests.get(f"{PAGE}/data", headers=HEADERS, timeout=60)
    resp.raise_for_status()
    payload = resp.json()

    url = _xlsx_from(payload, payload.get("uri") or "")
    if url:
        return url

    children = payload.get("datasets") or []
    print(f"  landing page has no downloads; following {len(children)} child link(s)")
    for child in children:
        uri = (child.get("uri") or "").strip()
        if not uri:
            continue
        child_url = f"https://www.ons.gov.uk{uri}/data"
        try:
            r = requests.get(child_url, headers=HEADERS, timeout=60)
            if r.status_code != 200:
                print(f"  {child_url} -> status {r.status_code}")
                continue
            child_payload = r.json()
            url = _xlsx_from(child_payload, child_payload.get("uri") or uri)
            if url:
                return url
            print(f"  {uri}: no xlsx in downloads "
                  f"({[d.get('file') for d in child_payload.get('downloads') or []]})")
        except (requests.RequestException, ValueError) as e:
            print(f"  {child_url} failed: {e}")

    print(f"  page JSON keys: {list(payload.keys())}")
    raise SystemExit("No xlsx download found via the ONS dataset page JSON.")


def _label_date(label: str):
    """Try to read a column label as a month (e.g. 'Feb 2026', '2026-02')."""
    try:
        ts = pd.to_datetime(str(label).strip(), errors="coerce")
        return None if pd.isna(ts) else ts
    except Exception:
        return None


GSS_ANY_RE = re.compile(r"^[EKWSNJ]\d{8}$")  # any ONS geography code


def _as_date(v):
    """Read a cell as a date (openpyxl datetimes or strings)."""
    if v is None:
        return None
    try:
        ts = pd.to_datetime(v, errors="coerce")
        return None if pd.isna(ts) else ts
    except Exception:
        return None


def parse_xlsx(content: bytes, geography: dict) -> tuple[dict[str, float], str]:
    """PIPR layout (verified Jun 2026): LONG format. Each row is one area for
    one month: [date, area code, area name, region, then repeating 4-column
    blocks of (index, monthly change, annual change, average rent) per
    category, all-properties block first]. Strategy: find the code and date
    columns, keep only the latest month's London rows, then take the leftmost
    column whose values look like monthly rents (£200–8,000), preferring one
    whose header mentions rent/all."""
    sheets = pd.read_excel(io.BytesIO(content), engine="openpyxl",
                           sheet_name=None, header=None)
    valid = {b["code"] for b in geography["boroughs"]}

    for sheet_name, df in sheets.items():
        df = df.astype(object).where(pd.notna(df), None)

        # column holding E09 codes
        code_col = None
        for c in df.columns:
            n = df[c].map(lambda v: isinstance(v, str) and bool(E09_RE.match(v.strip()))).sum()
            if n >= 25:
                code_col = c
                break
        if code_col is None:
            continue

        e09_rows = [i for i in df.index
                    if isinstance(df.at[i, code_col], str)
                    and E09_RE.match(df.at[i, code_col].strip())]

        # date column: parses as a date on most E09 rows
        date_col = None
        for c in df.columns:
            if c == code_col:
                continue
            hits = sum(1 for i in e09_rows[:60] if _as_date(df.at[i, c]) is not None)
            if hits >= min(40, len(e09_rows[:60])):
                date_col = c
                break

        # keep only the latest month's rows (each area repeats per month)
        if date_col is not None:
            dates = {i: _as_date(df.at[i, date_col]) for i in e09_rows}
            latest = max(d for d in dates.values() if d is not None)
            e09_rows = [i for i, d in dates.items() if d == latest]
            period = latest.strftime("%b %Y")
        else:
            period = "latest"

        # real header sits above the first row holding ANY geography code
        def is_any(v) -> bool:
            return isinstance(v, str) and bool(GSS_ANY_RE.match(v.strip()))
        first_data_row = min(i for i in df.index if is_any(df.at[i, code_col]))
        labels: dict[int, str] = {}
        for c in df.columns:
            parts = [str(df.at[r, c]).strip()
                     for r in range(max(0, first_data_row - 6), first_data_row)
                     if df.at[r, c] is not None and str(df.at[r, c]).strip()]
            labels[c] = " / ".join(parts)

        # candidate value columns: look like monthly £ on the kept rows
        def rentish_fraction(c) -> float:
            ok = total = 0
            for i in e09_rows:
                v = df.at[i, c]
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                total += 1
                if 200 <= f <= 8000:
                    ok += 1
            return ok / total if total else 0.0

        candidates = [c for c in df.columns
                      if c not in (code_col, date_col) and rentish_fraction(c) >= 0.8]
        if not candidates:
            print(f"  sheet '{sheet_name}': code_col={code_col}, date_col={date_col}, "
                  f"period={period}, London rows after date filter={len(e09_rows)}")
            # Dump one London row in full so we can see exactly where the £ rent sits.
            sample = next((i for i in e09_rows
                           if df.at[i, code_col].strip() in valid), None)
            if sample is None and date_col is not None:
                # date filter may have emptied it — show a pre-filter London row
                allrows = [i for i in df.index
                           if isinstance(df.at[i, code_col], str)
                           and df.at[i, code_col].strip() in valid]
                sample = allrows[0] if allrows else None
                print(f"  (date filter left 0 London rows; showing an unfiltered one)")
            if sample is not None:
                print(f"  full values for London row {sample} "
                      f"(code={df.at[sample, code_col]}):")
                for c in df.columns:
                    v = df.at[sample, c]
                    if v is not None and str(v).strip():
                        print(f"    col {c}: {v!r}   header={labels.get(c) or ''!r}")
            continue

        def pref(c) -> tuple:
            l = labels.get(c, "").lower()
            return ("rent" in l and "all" in l, "rent" in l)

        labelled = [c for c in candidates if any(pref(c))]
        col = (sorted(labelled, key=lambda c: (pref(c), -c), reverse=True)[0]
               if labelled else min(candidates))  # leftmost = all-properties block
        print(f"  sheet '{sheet_name}': period {period}; value column {col} "
              f"({labels.get(col) or 'unlabelled — leftmost rent-like block'}); "
              f"{len(candidates)} candidate column(s)")

        out: dict[str, float] = {}
        for i in e09_rows:
            code = df.at[i, code_col].strip()
            if code not in valid:
                continue
            try:
                v = float(df.at[i, col])
            except (TypeError, ValueError):
                continue
            if 200 <= v <= 8000:
                out[code] = round(v, 0)
        if len(out) >= 25:
            return out, f"{labels.get(col) or f'column {col}'} ({period})"
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
