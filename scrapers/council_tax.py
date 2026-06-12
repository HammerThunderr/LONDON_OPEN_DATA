#!/usr/bin/env python3
"""
council_tax.py — Band D council tax per London borough.

Source: MHCLG "Council Tax levels set by local authorities in England"
(annual accredited official statistics, published each March, OGL).

Discovery is via the gov.uk Content API — every page has a JSON twin at
  https://www.gov.uk/api/content/<path>
whose details.attachments list carries title + url. We try the current
financial year's slug first, then the previous year's (so the scraper works
in the Jan–Mar window before the new release lands).

We parse "Table 10: Local Authority Level Data" (ODS): rows are located by
their ONS codes (E09... = London boroughs) and the value column by a header
containing "band d". The figure used is the AREA Band D — what a resident in
the borough actually pays, including the GLA precept.

The ODS layout isn't version-stable, so the parser is deliberately noisy on
failure: it prints every candidate header it saw, making next year's fix a
one-look job.
"""

from __future__ import annotations

import io
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

API_BASE = "https://www.gov.uk/api/content/government/statistics"
SLUG_TMPL = "council-tax-levels-set-by-local-authorities-in-england-{y1}-to-{y2}"
HEADERS = {"User-Agent": "LONDON_OPEN_DATA pipeline (github.com/HammerThunderr)"}

E09_RE = re.compile(r"^E09\d{6}$")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "council_tax.json"


def fy_candidates(today: date) -> list[tuple[int, int]]:
    """Financial years to try, newest first. New release lands in March."""
    y = today.year
    if today.month >= 4:
        return [(y, y + 1), (y - 1, y)]
    return [(y, y + 1), (y - 1, y), (y - 2, y - 1)]


def find_release() -> tuple[str, str, str]:
    """Return (ods_url, attachment_title, fy_label) for the newest release."""
    for y1, y2 in fy_candidates(date.today()):
        slug = SLUG_TMPL.format(y1=y1, y2=y2)
        resp = requests.get(f"{API_BASE}/{slug}", headers=HEADERS, timeout=60)
        if resp.status_code != 200:
            print(f"  no release for {y1}-{y2} (status {resp.status_code})")
            continue
        attachments = (resp.json().get("details") or {}).get("attachments") or []
        # Prefer the per-authority data table; fall back to the main tables file.
        for want in ("table 10", "tables 1"):
            for a in attachments:
                title = (a.get("title") or "").lower()
                url = a.get("url") or ""
                if want in title and url.endswith(".ods"):
                    return url, a.get("title", ""), f"{y1}-{str(y2)[2:]}"
        print(f"  release {y1}-{y2} found but no matching ODS; attachments were:")
        for a in attachments:
            print(f"    - {a.get('title')}")
    raise SystemExit("No usable council tax release found via the gov.uk content API.")


def parse_ods(content: bytes, geography: dict) -> tuple[dict[str, float], str]:
    """Find London borough rows by E09 code and the area Band D column by
    header text. Returns ({code: band_d}, column_label_used)."""
    sheets = pd.read_excel(io.BytesIO(content), engine="odf",
                           sheet_name=None, header=None)
    valid = {b["code"] for b in geography["boroughs"]}

    for sheet_name, df in sheets.items():
        df = df.astype(object).where(pd.notna(df), None)
        # locate the column holding ONS codes
        code_col = None
        for c in df.columns:
            col = df[c].map(lambda v: isinstance(v, str) and bool(E09_RE.match(v.strip())))
            if col.sum() >= 25:  # most of the 33 boroughs present
                code_col = c
                break
        if code_col is None:
            continue

        e09_rows = [i for i in df.index
                    if isinstance(df.at[i, code_col], str)
                    and E09_RE.match(df.at[i, code_col].strip())]
        first_data_row = min(e09_rows)

        # build flattened header labels from the rows above the data
        header_rows = range(max(0, first_data_row - 6), first_data_row)
        labels: dict[int, str] = {}
        for c in df.columns:
            parts = [str(df.at[r, c]).strip() for r in header_rows
                     if df.at[r, c] is not None and str(df.at[r, c]).strip()]
            labels[c] = " / ".join(parts)

        def is_band_d(label: str) -> bool:
            l = label.lower()
            return "band d" in l and "per dwelling" not in l

        candidates = [c for c, l in labels.items() if is_band_d(l)]
        # Prefer an "area" Band D (includes GLA precept) if labelled as such.
        area = [c for c in candidates if "area" in labels[c].lower()]
        pick = (area or candidates)

        if not pick:
            print(f"  sheet '{sheet_name}': found E09 rows but no 'Band D' column. "
                  f"Headers seen:")
            for c, l in labels.items():
                if l:
                    print(f"    col {c}: {l}")
            continue
        if len(pick) > 1:
            # rightmost Band D column is conventionally the all-precepts total
            print(f"  sheet '{sheet_name}': {len(pick)} Band D columns "
                  f"({[labels[c] for c in pick]}); using the rightmost.")
        col = pick[-1]

        out: dict[str, float] = {}
        for i in e09_rows:
            code = df.at[i, code_col].strip()
            if code not in valid:
                continue
            v = df.at[i, col]
            try:
                out[code] = round(float(v), 2)
            except (TypeError, ValueError):
                pass
        if len(out) >= 25:
            return out, labels[col]

    raise SystemExit(
        "Could not extract Band D figures from any sheet — see the header "
        "listings above and adjust is_band_d() in this script."
    )


def main() -> None:
    geography = json.loads((DATA_DIR / "geography.json").read_text(encoding="utf-8"))
    name_by_code = {b["code"]: b["name"] for b in geography["boroughs"]}

    if len(sys.argv) > 1:  # offline test: python scrapers/council_tax.py file.ods
        content = Path(sys.argv[1]).read_bytes()
        src, title, fy = sys.argv[1], "local file", "test"
        print(f"Using local file {src}")
    else:
        url, title, fy = find_release()
        print(f"Fetching {title}\n  {url}")
        resp = requests.get(url, headers=HEADERS, timeout=120)
        resp.raise_for_status()
        content = resp.content
        src = url

    values, column_used = parse_ods(content, geography)

    mean = sum(values.values()) / len(values)
    boroughs = [
        {"code": code, "name": name_by_code[code], "band_d": v}
        for code, v in sorted(values.items(), key=lambda kv: name_by_code[kv[0]])
    ]
    OUT_PATH.write_text(json.dumps({
        "source": src,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "financial_year": fy,
        "column_used": column_used,
        "note": "Area Band D council tax (includes GLA precept). MHCLG, OGL.",
        "boroughs": boroughs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote council_tax.json: {len(boroughs)} boroughs, FY {fy}, "
          f"London mean £{mean:.0f} (column: {column_used!r})")
    if len(boroughs) != 33:
        print(f"  WARNING: expected 33 boroughs, got {len(boroughs)}.")
    if not 1200 <= mean <= 3200:
        print(f"  WARNING: London mean £{mean:.0f} looks implausible — "
              f"check the column picked. (2026-27 reference: ~£2,068)")


if __name__ == "__main__":
    main()
