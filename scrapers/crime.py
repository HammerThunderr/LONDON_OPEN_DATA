#!/usr/bin/env python3
"""
crime.py — recorded crime rate per 1,000 population, per local authority.

Source: ONS "Recorded crime data by Community Safety Partnership area"
(CSP areas equate to local authorities). Carries offence counts and a
PRE-CALCULATED rate per 1,000 population for the latest year, every police
force in England & Wales — so this one national file covers every city in the
app, replacing the old London-only MPS feed and giving one consistent measure.

Discovery: ONS page-JSON convention (landing -> version -> file), same as
rent.py. The data file is xlsx/ods; we locate borough rows by LAD (E0x) code
and the rate column by header ('rate per 1,000'). Defensive + noisy on
mismatch.

Joins on the geography spine (data/geography.json), so it automatically covers
exactly the local authorities the spine contains — London + GM + WM + WY.
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

PAGE = ("https://www.ons.gov.uk/peoplepopulationandcommunity/crimeandjustice/"
        "datasets/recordedcrimedataatcommunitysafetypartnershiplocalauthoritylevel")
HEADERS = {"User-Agent": "LONDON_OPEN_DATA pipeline (github.com/HammerThunderr)",
           "Accept": "application/json"}

LAD_RE = re.compile(r"^E0[6-9]\d{6}$")  # E06/E07/E08/E09 local authorities

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "crime.json"


def _xlsx_from(payload: dict, base_uri: str = "") -> str | None:
    for d in payload.get("downloads") or []:
        f = (d.get("file") or "").strip()
        if not f.lower().endswith((".xlsx", ".xls", ".csv")):
            continue
        if f.startswith("http"):
            return f
        if not f.startswith("/"):
            f = f"{base_uri.rstrip('/')}/{f}"
        return f"https://www.ons.gov.uk/file?uri={f}"
    return None


def find_download_url() -> str:
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
        try:
            r = requests.get(f"https://www.ons.gov.uk{uri}/data", headers=HEADERS, timeout=60)
            if r.status_code != 200:
                continue
            cp = r.json()
            url = _xlsx_from(cp, cp.get("uri") or uri)
            if url:
                return url
        except (requests.RequestException, ValueError):
            continue
    print(f"  page JSON keys: {list(payload.keys())}")
    raise SystemExit("No data file found via the ONS dataset page JSON.")


def parse(content: bytes, geography: dict) -> tuple[dict[str, float], str]:
    valid = {b["code"] for b in geography["boroughs"]}
    # try every sheet; find LAD-code col + a 'rate per 1,000' col
    sheets = pd.read_excel(io.BytesIO(content), engine="openpyxl",
                           sheet_name=None, header=None)
    for sheet_name, df in sheets.items():
        df = df.astype(object).where(pd.notna(df), None)
        ncol = df.shape[1]
        code_col = None
        for c in range(ncol):
            if sum(1 for v in df[c] if isinstance(v, str) and LAD_RE.match(str(v).strip())) >= 20:
                code_col = c
                break
        if code_col is None:
            continue
        rows = [i for i in df.index
                if isinstance(df.at[i, code_col], str) and LAD_RE.match(df.at[i, code_col].strip())]
        first = min(rows)
        labels = {}
        for c in range(ncol):
            parts = [str(df.at[r, c]).strip() for r in range(max(0, first - 8), first)
                     if df.at[r, c] is not None and str(df.at[r, c]).strip()]
            labels[c] = " / ".join(parts)

        def col_vals(c):
            out = {}
            for i in rows:
                code = df.at[i, code_col].strip()
                if code not in valid:
                    continue
                try:
                    v = float(df.at[i, c])
                except (TypeError, ValueError):
                    continue
                if 0 < v < 1000:  # plausible crimes per 1,000
                    out[code] = round(v, 1)
            return out

        rate_cols = [c for c, l in labels.items()
                     if "rate" in l.lower() and "1,000" in l.replace(" ", "").replace("000", ",000").lower()
                     or ("rate" in l.lower() and "1000" in l.replace(",", ""))]
        # fallback: any column literally mentioning 'rate per'
        if not rate_cols:
            rate_cols = [c for c, l in labels.items() if "rate per" in l.lower()]
        scored = []
        for c in (rate_cols or range(ncol)):
            vals = col_vals(c)
            if len(vals) >= 20:
                # 'total'/'all offences' rate preferred over a single category
                pref = any(w in labels[c].lower() for w in ("total", "all offence", "all recorded"))
                scored.append((pref, len(vals), c, vals))
        if scored:
            scored.sort(key=lambda t: (t[0], t[1]))
            _, _, c, vals = scored[-1]
            return vals, labels[c] or f"{sheet_name} col {c}"
        print(f"  sheet '{sheet_name}': LAD rows found but no rate column. Headers:")
        for c, l in labels.items():
            if l:
                print(f"    col {c}: {l}")
    raise SystemExit("Could not find a 'rate per 1,000' column — see headers above.")


def main() -> None:
    geography = json.loads((DATA_DIR / "geography.json").read_text(encoding="utf-8"))
    name_by_code = {b["code"]: b["name"] for b in geography["boroughs"]}

    if len(sys.argv) > 1:
        content = Path(sys.argv[1]).read_bytes()
        src = sys.argv[1]
        print(f"Using local file {src}")
    else:
        url = find_download_url()
        print(f"Fetching {url}")
        resp = requests.get(url, headers={k: v for k, v in HEADERS.items() if k != "Accept"}, timeout=180)
        resp.raise_for_status()
        content = resp.content
        src = url

    values, label = parse(content, geography)
    boroughs = [{"code": code, "name": name_by_code[code], "rate_per_1000": v}
                for code, v in sorted(values.items(), key=lambda kv: name_by_code[kv[0]])]
    OUT_PATH.write_text(json.dumps({
        "source": PAGE,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "value_label": label,
        "note": ("ONS recorded crime by Community Safety Partnership (= local "
                 "authority): total offences rate per 1,000 residents, latest year. "
                 "One national source across all cities."),
        "boroughs": boroughs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    vals = [b["rate_per_1000"] for b in boroughs]
    mean = sum(vals) / len(vals) if vals else 0
    print(f"Wrote crime.json: {len(boroughs)} LADs, mean {mean:.0f}/1,000 "
          f"(range {min(vals):.0f}–{max(vals):.0f}, column {label!r})")
    if not 40 <= mean <= 200:
        print(f"  WARNING: mean {mean:.0f}/1,000 outside usual band — check column.")


if __name__ == "__main__":
    main()
