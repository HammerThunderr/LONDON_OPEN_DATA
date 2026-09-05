#!/usr/bin/env python3
"""
UK Job Market Tracker — trend analysis

Reads data/latest.json and data/history.json, and writes
data/predictions.json.

Two separate signals per sector, per area (uk / london):

  SALARY TREND — from the 12-month series Adzuna provides directly.
  Available immediately.

  DEMAND TREND — from vacancy counts accumulated across our own runs in
  history.json. Adzuna gives a current count but no vacancy history, so
  this only becomes meaningful after several months of collection. Until
  then it reports "accumulating".

Method is deliberately simple: 12 monthly points is not enough to justify
anything heavier (an LSTM/Prophet would fit noise with false confidence).
Median-based comparisons and a Theil-Sen slope, both chosen because they
resist a single odd month — a real case from this dataset: Customer
Services sat flat near GBP 30k for 11 months then printed GBP 49k in the
12th, which a naive first-vs-last read misclassified as a 61% rise.

The forecast is a one-step linear extrapolation, not a validated model,
and is labelled as such in the output. Revisit with a proper time-series
model once history.json holds a couple of years.
"""

import json
import statistics
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path("data")
LATEST_FILE = DATA_DIR / "latest.json"
HISTORY_FILE = DATA_DIR / "history.json"
PREDICTIONS_FILE = DATA_DIR / "predictions.json"

# % change beyond which a sector is called rising/falling rather than
# stable. This is an untuned starting guess — watch a few months of real
# output and adjust. In a high-inflation year almost everything will read
# "rising" in nominal terms, which is a reason to raise it (or to deflate
# the series by CPI, see the note at the bottom of this file).
SALARY_THRESHOLD_PCT = 5.0
DEMAND_THRESHOLD_PCT = 10.0  # vacancy counts are noisier than salaries

MIN_MONTHS_FOR_TREND = 3
MIN_SNAPSHOTS_FOR_DEMAND = 4


def theil_sen(xs, ys):
    """Median of pairwise slopes — robust to a single outlier in a way
    ordinary least squares is not."""
    slopes = [
        (ys[j] - ys[i]) / (xs[j] - xs[i])
        for i in range(len(xs))
        for j in range(i + 1, len(xs))
        if xs[j] != xs[i]
    ]
    slope = statistics.median(slopes) if slopes else 0.0
    intercept = statistics.median(y - slope * x for x, y in zip(xs, ys))
    return slope, intercept


def robust_pct_change(ys, window=3):
    """Median of the first `window` points vs median of the last
    `window`, rather than a raw first-vs-last comparison."""
    w = min(window, max(1, len(ys) // 2))
    early = statistics.median(ys[:w])
    late = statistics.median(ys[-w:])
    if early == 0:
        return 0.0
    return (late - early) / early * 100


def classify(pct, threshold):
    if pct >= threshold:
        return "rising"
    if pct <= -threshold:
        return "falling"
    return "stable"


def analyze_salary(trend_dict):
    months = sorted(trend_dict.keys())  # "YYYY-MM" sorts chronologically
    if len(months) < MIN_MONTHS_FOR_TREND:
        return {"months_available": len(months), "trend": "insufficient_data"}

    ys = [trend_dict[m] for m in months]
    xs = list(range(len(months)))
    slope, intercept = theil_sen(xs, ys)
    pct = robust_pct_change(ys)

    return {
        "months_available": len(months),
        "pct_change": round(pct, 2),
        "trend": classify(pct, SALARY_THRESHOLD_PCT),
        "latest_month": months[-1],
        "latest_salary": round(ys[-1], 2),
        "naive_next_month_forecast": round(slope * len(months) + intercept, 2),
    }


def analyze_demand(counts):
    """counts: chronological list of vacancy counts for one sector/area."""
    counts = [c for c in counts if c is not None]
    if len(counts) < MIN_SNAPSHOTS_FOR_DEMAND:
        return {
            "snapshots_available": len(counts),
            "trend": "accumulating",
            "note": f"needs {MIN_SNAPSHOTS_FOR_DEMAND} snapshots before a "
                    f"demand trend means anything",
            "latest_count": counts[-1] if counts else None,
        }

    pct = robust_pct_change(counts)
    return {
        "snapshots_available": len(counts),
        "pct_change": round(pct, 2),
        "trend": classify(pct, DEMAND_THRESHOLD_PCT),
        "latest_count": counts[-1],
    }


def demand_series_from_history(history):
    """Build {tag: {area: [counts in time order]}} from history.json."""
    series = {}
    for snap in sorted(history, key=lambda s: s.get("collected_at", "")):
        for sector in snap.get("sectors", []):
            tag = sector.get("tag")
            if not tag:
                continue
            counts = sector.get("vacancy_counts", {})
            for area, value in counts.items():
                series.setdefault(tag, {}).setdefault(area, []).append(value)
    return series


def main():
    if not LATEST_FILE.exists():
        raise SystemExit(f"{LATEST_FILE} not found — run adzuna_uk_tracker.py first.")

    latest = json.loads(LATEST_FILE.read_text())
    history = json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else []
    demand_series = demand_series_from_history(history)

    predictions = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "median-based trend classification with Theil-Sen slope; "
                  "forecast is a naive one-step extrapolation, not a "
                  "validated model. Salary figures are nominal — not "
                  "adjusted for inflation.",
        "sectors": [],
    }

    for sector in latest.get("sectors", []):
        tag = sector["tag"]
        entry = {"label": sector["label"], "tag": tag, "areas": {}}

        for area_name, area_data in sector.get("areas", {}).items():
            entry["areas"][area_name] = {
                "salary": analyze_salary(area_data.get("salary_trend", {})),
                "demand": analyze_demand(
                    demand_series.get(tag, {}).get(area_name, [])
                ),
            }

        predictions["sectors"].append(entry)

    DATA_DIR.mkdir(exist_ok=True)
    PREDICTIONS_FILE.write_text(json.dumps(predictions, indent=2))
    print(f"Wrote {PREDICTIONS_FILE}")

    for area in ("london", "uk"):
        rising = [
            s["label"]
            for s in predictions["sectors"]
            if s["areas"].get(area, {}).get("salary", {}).get("trend") == "rising"
        ]
        print(f"[{area}] salary rising: {rising}")


if __name__ == "__main__":
    main()


# Possible next step: deflate the salary series by ONS CPI before
# classifying, so "rising" means a real-terms gain rather than nominal
# drift. ONS publishes CPI as open data.
