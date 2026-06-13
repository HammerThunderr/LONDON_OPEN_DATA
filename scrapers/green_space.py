#!/usr/bin/env python3
"""
green_space.py — access to green/blue space per borough.

Source: Defra "Access to green and blue space in England" (Official Statistics
in Development, first published March 2026, OGL). The headline measure is the
percentage of households with access to at least one green or blue space
within a 15-minute walk (the Environmental Improvement Plan "15-minute
commitment").
  https://www.gov.uk/government/statistics/access-to-green-and-blue-space-in-england

The main data table is a 44 MB UPRN/MSOA ODS — too big and too fine. We use the
small "figure data" ODS instead (tens of KB), which carries the aggregated
LAD-level figures behind the publication's charts. We locate the London
borough rows by E09 code and the access column by header text.

Discovery: the ODS lives at an assets.publishing.service.gov.uk media-hash URL
that changes each release, so we parse the publication page for the
'figure data' .ods link rather than hardcoding it.

Reuses the error-cell-tolerant odfpy reader pattern from council_tax.py:
gov.uk ODS files contain value-type 'error' cells that crash pandas.
"""

from __future__ import annotations

import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from odf.opendocument import load as odf_load
from odf.table import Table, TableCell, TableRow
from odf import teletype

PUBLICATION = ("https://www.gov.uk/government/statistics/"
               "access-to-green-and-blue-space-in-england")
HEADERS = {"User-Agent": "LONDON_OPEN_DATA pipeline (github.com/HammerThunderr)"}

E09_RE = re.compile(r"^E09\d{6}$")
# Prefer the combined green-OR-blue 15-minute access measure.
ACCESS_HINTS = ("green or blue", "green and blue", "15-minute", "15 minute",
                "access", "percentage", "households", "%")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "green_space.json"


def find_ods_url() -> str:
    """Find the small 'figure data' ODS link on the publication page."""
    html = requests.get(PUBLICATION, headers=HEADERS, timeout=60).text
    ods = re.findall(r'https://assets\.publishing\.service\.gov\.uk/[^"]+?\.ods', html)
    if not ods:
        raise SystemExit("No .ods links found on the publication page.")
    # Prefer a 'figure' file (small, aggregated); else the smallest-named table.
    figure = [u for u in ods if "figure" in u.lower()]
    chosen = (figure or ods)[0]
    print(f"  candidate ODS links: {len(ods)}; chose "
          f"{'figure-data' if figure else 'first'}: {chosen}")
    return chosen


def _ods_sheets(content: bytes) -> dict[str, list[list]]:
    """Read ODS via odfpy; error/unknown cells degrade to text/None, never
    raising (pandas' ODS reader crashes on value-type 'error')."""
    doc = odf_load(io.BytesIO(content))
    sheets: dict[str, list[list]] = {}
    for table in doc.spreadsheet.getElementsByType(Table):
        rows: list[list] = []
        for tr in table.getElementsByType(TableRow):
            row: list = []
            for tc in tr.getElementsByType(TableCell):
                try:
                    rep = int(tc.getAttribute("numbercolumnsrepeated") or 1)
                except (TypeError, ValueError):
                    rep = 1
                vtype = tc.getAttribute("valuetype")
                value = None
                if vtype in ("float", "currency", "percentage"):
                    try:
                        value = float(tc.getAttribute("value"))
                    except (TypeError, ValueError):
                        value = None
                if value is None:
                    text = teletype.extractText(tc).strip()
                    value = text or None
                row.extend([value] * min(rep, 1 if value is None else 64, 512))
                if len(row) > 512:
                    break
            rows.append(row)
        sheets[str(table.getAttribute("name"))] = rows
    return sheets


def parse(content: bytes, geography: dict) -> tuple[dict[str, float], str]:
    valid = {b["code"] for b in geography["boroughs"]}

    best: tuple[dict[str, float], str] | None = None
    for sheet_name, rows in _ods_sheets(content).items():
        if not rows:
            continue
        width = max(len(r) for r in rows)
        grid = [r + [None] * (width - len(r)) for r in rows]

        def cell(i, c):
            return grid[i][c]

        def is_code(v):
            return isinstance(v, str) and bool(E09_RE.match(v.strip()))

        code_col = None
        for c in range(width):
            if sum(1 for i in range(len(grid)) if is_code(cell(i, c))) >= 20:
                code_col = c
                break
        if code_col is None:
            continue

        e09_rows = [i for i in range(len(grid)) if is_code(cell(i, code_col))]
        first = min(e09_rows)
        labels = {}
        for c in range(width):
            parts = [str(cell(r, c)).strip() for r in range(max(0, first - 8), first)
                     if cell(r, c) is not None and str(cell(r, c)).strip()]
            labels[c] = " / ".join(parts)

        # candidate value columns: numeric for E09 rows, values in 0..100
        def col_vals(c):
            out = {}
            for i in e09_rows:
                code = cell(i, code_col).strip()
                if code not in valid:
                    continue
                v = cell(i, c)
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                if 0 <= f <= 100:
                    out[code] = round(f if f > 1.5 else f * 100, 1)  # accept fraction or %
            return out

        # rank columns by header relevance then coverage
        scored = []
        for c in range(width):
            if c == code_col:
                continue
            vals = col_vals(c)
            if len(vals) < 20:
                continue
            label = labels[c].lower()
            hint = sum(1 for h in ACCESS_HINTS if h in label)
            # strongly prefer the combined green-or-blue total, penalise sub-measures
            if "blue" in label and "green" not in label:
                hint -= 1
            if any(w in label for w in ("rural", "urban", "doorstep", "local only")):
                hint -= 1
            scored.append((hint, len(vals), c, vals))
        if not scored:
            continue
        scored.sort(key=lambda t: (t[0], t[1]))
        hint, n, c, vals = scored[-1]
        cand = (vals, labels[c] or f"{sheet_name} col {c}")
        if best is None or len(vals) > len(best[0]):
            best = cand

    if best is None:
        raise SystemExit(
            "Could not find a London borough access column in the figure data. "
            "The ODS layout may differ — inspect the file structure.")
    return best


def main() -> None:
    geography = json.loads((DATA_DIR / "geography.json").read_text(encoding="utf-8"))
    name_by_code = {b["code"]: b["name"] for b in geography["boroughs"]}

    if len(sys.argv) > 1:  # offline test: python scrapers/green_space.py file.ods
        content = Path(sys.argv[1]).read_bytes()
        src = sys.argv[1]
        print(f"Using local file {src}")
    else:
        url = find_ods_url()
        print(f"Fetching {url}")
        resp = requests.get(url, headers=HEADERS, timeout=180)
        resp.raise_for_status()
        content = resp.content
        src = url

    values, label = parse(content, geography)
    boroughs = [
        {"code": code, "name": name_by_code[code], "green_access_pct": v}
        for code, v in sorted(values.items(), key=lambda kv: name_by_code[kv[0]])
    ]
    mean = sum(values.values()) / len(values)

    OUT_PATH.write_text(json.dumps({
        "source": PUBLICATION,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "value_label": label,
        "note": ("Defra: % of households with access to green/blue space within "
                 "a 15-minute walk (Official Statistics in Development, 2025 data)."),
        "boroughs": boroughs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote green_space.json: {len(boroughs)} boroughs, "
          f"mean {mean:.0f}% (column: {label!r})")
    if len(boroughs) != 33:
        print(f"  NOTE: expected 33 boroughs, got {len(boroughs)}.")
    if not 30 <= mean <= 100:
        print(f"  WARNING: mean {mean:.0f}% implausible — check the column.")


if __name__ == "__main__":
    main()
