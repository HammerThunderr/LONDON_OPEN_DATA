#!/usr/bin/env python3
"""
scores.py — assembles data/boroughs.json, the file the Flutter app reads.

Run this LAST, after any metric scraper. It loads data/geography.json for the
canonical 33 boroughs, then every metric file that exists in data/, computes a
0-100 percentile per metric per borough (HIGHER = ALWAYS BETTER — direction
inversion happens here, never in the app), and writes the combined file in the
app's contract:

  { "source", "scraped_at", "metrics": [...],
    "boroughs": [ {"code","name","metrics":{key:{raw,unit,percentile}}} ] }

Metrics whose file doesn't exist yet are simply omitted — the app skips them
and renormalises weights, so the pipeline can grow one dataset at a time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "boroughs.json"

# key -> (filename, field holding the raw value, display unit, higher_is_better)
METRICS = {
    "crime": ("crime.json", "rate_per_1000", "per 1,000", False),
    "rent": ("rent.json", "monthly_rent", "£/month", False),
    "council_tax": ("council_tax.json", "band_d", "£ Band D", False),
    "transport": ("ptal.json", "ptal_mean", "access score", True),
    "air_quality": ("air_quality.json", "pm25_total", "µg/m³ PM2.5", False),
    "green_space": ("green_space.json", "green_access_pct", "% near green space", True),
    "schools": ("schools.json", "attainment8", "Attainment 8", True),
}


def percentiles(values: dict[str, float], higher_is_better: bool) -> dict[str, float]:
    """Average-rank percentiles, 0-100, ties shared, direction-corrected."""
    if len(values) < 2:
        return {code: 50.0 for code in values}
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    # average rank for ties
    ranks: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        avg_rank = (i + j) / 2
        for k in range(i, j + 1):
            ranks[items[k][0]] = avg_rank
        i = j + 1
    out = {}
    for code, rank in ranks.items():
        p = rank / (n - 1) * 100  # low raw value -> low percentile
        out[code] = round(p if higher_is_better else 100 - p, 1)
    return out


def main() -> None:
    geography = json.loads((DATA_DIR / "geography.json").read_text(encoding="utf-8"))
    base = {b["code"]: {"code": b["code"], "name": b["name"], "metrics": {}}
            for b in geography["boroughs"]}

    included = []
    for key, (fname, field, unit, higher) in METRICS.items():
        path = DATA_DIR / fname
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_by_code = {
            row["code"]: row[field]
            for row in payload.get("boroughs", [])
            if row.get("code") in base and isinstance(row.get(field), (int, float))
        }
        if not raw_by_code:
            continue
        pct = percentiles(raw_by_code, higher)
        for code, raw in raw_by_code.items():
            base[code]["metrics"][key] = {
                "raw": raw, "unit": unit, "percentile": pct[code],
            }
        included.append(key)

    if not included:
        raise SystemExit("No metric files found in data/ — nothing to assemble.")

    out = {
        "source": "https://github.com/HammerThunderr/LONDON_OPEN_DATA",
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metrics": included,
        "boroughs": sorted(base.values(), key=lambda b: b["name"]),
    }
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote boroughs.json: {len(out['boroughs'])} boroughs, metrics: {', '.join(included)}")


if __name__ == "__main__":
    main()
