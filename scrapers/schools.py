#!/usr/bin/env python3
"""
schools.py — secondary school attainment per borough.

Source: DfE "Key stage 4 performance", Local Authority data set, served by the
Explore Education Statistics (EES) open-data API (OGL).

Measure: average Attainment 8 (`avg_att8`) for ALL pupils, state-funded
schools, both genders. We use Attainment 8 rather than Progress 8 because
Progress 8 is NOT published in some years (2020, 2021, and 2024/25 — those
KS4 cohorts have no KS2 baseline), whereas Attainment 8 is published every
year. If a Progress 8 column is present we also record it for display.

Why not Ofsted: since Sept 2024 there is no single overall-effectiveness
grade, and it was removed from the schools register — so an objective
attainment score is both more available and more comparable.

Discovery: the EES API lists publications -> releases -> data sets. We find
the "Key stage 4 performance" publication, take its latest release, pick the
"local authority" data set, and pull its CSV. Column and filter names are
resolved from the CSV header / values defensively, and the script is noisy on
any mismatch so a format change is a one-look fix.

City of London has no state-funded secondary schools, so it has no value here
(the app skips missing metrics).
"""

from __future__ import annotations

import csv
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://api.education.gov.uk/statistics/v1"
EES = "https://explore-education-statistics.service.gov.uk"
HEADERS = {"User-Agent": "LONDON_OPEN_DATA pipeline (github.com/HammerThunderr)",
           "Accept": "application/json"}

# Fallback: a known KS4 'local authority' data-set id on the main EES site,
# used only if the API listing doesn't surface one (the API doesn't host every
# dataset). The /csv endpoint on the main service is stable.
FALLBACK_DATASET_IDS = [
    "ce0ff638-7b30-45d3-b264-34ecf5cddb9b",  # KS4 LA data (gender breakdown)
]

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "schools.json"


def _get(url: str, **kw):
    r = requests.get(url, headers=HEADERS, timeout=120, **kw)
    r.raise_for_status()
    return r


def _results(payload) -> list:
    if isinstance(payload, list):
        return payload
    return payload.get("results") or payload.get("content") or []


def _csv_url(dataset_id: str) -> str:
    # Documented CSV download for a data set (main EES service).
    return f"{EES}/data-catalogue/data-set/{dataset_id}/csv"


def find_csv_url() -> tuple[str, str]:
    """Resolve the latest KS4 'local authority' data set via the documented
    EES v1 API (https://api.education.gov.uk/statistics/v1). Falls back to a
    known data-set id if the API doesn't list one (not all EES datasets are
    on the API)."""
    try:
        # 1) find the KS4 performance publication (paginate a little)
        pub_id = None
        for page in range(1, 6):
            payload = _get(f"{API}/publications",
                           params={"page": page, "pageSize": 40}).json()
            results = _results(payload)
            for p in results:
                if "key stage 4" in (p.get("title") or "").lower():
                    pub_id = p.get("id")
                    break
            if pub_id or not results:
                break
            paging = payload.get("paging") or {}
            if page >= (paging.get("totalPages") or page):
                break

        if pub_id:
            # 2) list its data sets, choose 'local authority' (not AP)
            ds_payload = _get(f"{API}/publications/{pub_id}/data-sets",
                              params={"pageSize": 60}).json()
            for d in _results(ds_payload):
                t = (d.get("title") or "").lower()
                if "local authority" in t and "alternative provision" not in t:
                    return _csv_url(d["id"]), d.get("title", "")
            print("  API listed the publication but no LA data set; using fallback")
        else:
            print("  KS4 publication not found via API; using fallback id")
    except requests.RequestException as e:
        print(f"  API discovery failed ({e}); using fallback id")

    # 3) fallback: known KS4 LA data-set id(s)
    for ds_id in FALLBACK_DATASET_IDS:
        return _csv_url(ds_id), f"KS4 local authority data ({ds_id})"
    raise SystemExit("No KS4 local authority data set could be resolved.")


def build(csv_text: str, geography: dict, source: str, title: str) -> dict:
    reader = csv.DictReader(io.StringIO(csv_text))
    header = reader.fieldnames or []

    def find_col(*opts: str) -> str | None:
        for o in opts:
            for h in header:
                if h.lower() == o:
                    return h
        return None

    code_col = find_col("new_la_code", "la_code", "new_la_code_unrounded")
    att8_col = find_col("avg_att8")
    p8_col = find_col("p8mea", "avg_p8score", "p8_score")
    gender_col = find_col("gender", "sex")
    time_col = find_col("time_period")
    if not code_col or not att8_col:
        raise SystemExit(f"Missing expected columns. Header was: {header}")

    valid = {b["code"]: b["name"] for b in geography["boroughs"]}

    # If a gender filter exists, keep 'Total'/'All'; if a time column exists,
    # keep the latest period present.
    rows = list(reader)
    if time_col:
        periods = {r[time_col] for r in rows if r.get(time_col)}
        if periods:
            latest = max(periods)
            rows = [r for r in rows if r.get(time_col) == latest]
    else:
        latest = "latest"

    def is_total(v: str) -> bool:
        return v.strip().lower() in ("total", "all", "all pupils", "")

    boroughs = []
    for r in rows:
        code = (r.get(code_col) or "").strip()
        if code not in valid:
            continue
        if gender_col and not is_total(r.get(gender_col) or ""):
            continue
        try:
            att8 = float(r[att8_col])
        except (TypeError, ValueError):
            continue
        entry = {"code": code, "name": valid[code], "attainment8": round(att8, 1)}
        if p8_col:
            try:
                entry["progress8"] = round(float(r[p8_col]), 2)
            except (TypeError, ValueError):
                pass
        boroughs.append(entry)

    # de-dupe (a stray non-Total row could double a borough); keep first
    seen, deduped = set(), []
    for b in sorted(boroughs, key=lambda x: x["name"]):
        if b["code"] in seen:
            continue
        seen.add(b["code"])
        deduped.append(b)

    return {
        "source": source,
        "dataset": title,
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "period": latest,
        "note": ("DfE Key Stage 4: average Attainment 8 per pupil, state-funded "
                 "schools. City of London has no state secondary schools."),
        "boroughs": deduped,
    }


def main() -> None:
    geography = json.loads((DATA_DIR / "geography.json").read_text(encoding="utf-8"))

    if len(sys.argv) > 1:  # offline test: python scrapers/schools.py file.csv
        csv_text = Path(sys.argv[1]).read_text(encoding="utf-8-sig")
        src, title = sys.argv[1], "local file"
        print(f"Using local file {src}")
    else:
        url, title = find_csv_url()
        print(f"Fetching '{title}'\n  {url}")
        csv_text = _get(url).content.decode("utf-8-sig", errors="replace")
        src = url

    result = build(csv_text, geography, src, title)
    OUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    vals = [b["attainment8"] for b in result["boroughs"]]
    mean = sum(vals) / len(vals) if vals else 0
    print(f"Wrote schools.json: {len(vals)} boroughs, period {result['period']}, "
          f"mean Attainment 8 {mean:.1f} (range {min(vals):.1f}–{max(vals):.1f})")
    if not 30 <= mean <= 65:
        print(f"  WARNING: mean Attainment 8 {mean:.1f} outside the usual ~40–55 "
              f"band — check the column picked.")
    if len(vals) < 31:
        print(f"  NOTE: {len(vals)} boroughs (City of London expected absent; "
              f"32 is normal).")


if __name__ == "__main__":
    main()
