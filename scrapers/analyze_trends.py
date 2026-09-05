#!/usr/bin/env python3
"""
UK Job Market Tracker — trend analysis

Reads data/latest.json (produced by adzuna_uk_tracker.py) and, for each
sector's 12-month salary_trend_uk series, fits a simple linear trend.

This is deliberately simple: 12 monthly points per sector is not enough
data to justify anything heavier (LSTM/Prophet/etc. would just be fitting
noise with false confidence). A least-squares line is honest about what
it is — a trend read, not a validated forecast — and the one-month-ahead
value is a naive linear extrapolation, labelled as such in the output.

Writes data/predictions.json with, per sector:
    - pct_change: % change, median-of-first-3-months vs median-of-last-3
      (median, not raw endpoints — a single odd month, e.g. a small-sample
      Adzuna data blip, shouldn't swing the whole read; caught this on a
      real sector in testing: Customer Services Jobs sat flat for 11
      months then spiked in the 12th, which a first-vs-last comparison
      misread as a 61% rise)
    - trend: "rising" / "falling" / "stable" (threshold-based on pct_change)
    - latest_month / latest_salary: most recent data point
    - naive_next_month_forecast: one-step Theil-Sen (median-of-pairwise-
      slopes) extrapolation — same outlier-resistance reasoning as above,
      instead of ordinary least squares

Revisit with a real time-series model once you have a couple of years of
your own accumulated weekly snapshots in data/history.json — until then,
this is the right amount of model for the amount of data.
"""

import json
import statistics
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path("data")
LATEST_FILE = DATA_DIR / "latest.json"
PREDICTIONS_FILE = DATA_DIR / "predictions.json"

# % change over the 12-month window beyond which a sector is called
# "rising"/"falling" rather than "stable". Adjust if this feels too
# sensitive or too dull once you've seen a few weeks of real output.
TREND_THRESHOLD_PCT = 5.0

MIN_MONTHS_FOR_TREND = 3  # below this, there's not enough to fit a line


def theil_sen(xs, ys):
    """Median of all pairwise slopes, plus a median-residual intercept.
    Robust to a single outlier point in a way ordinary least squares
    isn't — appropriate here since Adzuna's monthly figures for smaller
    categories can have one noisy month."""
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
    """Median of the first `window` months vs median of the last `window`
    months, instead of a raw first-vs-last comparison."""
    w = min(window, max(1, len(ys) // 2))
    early = statistics.median(ys[:w])
    late = statistics.median(ys[-w:])
    if early == 0:
        return 0.0
    return (late - early) / early * 100


def analyze_sector(trend_dict):
    months = sorted(trend_dict.keys())  # "YYYY-MM" sorts chronologically
    if len(months) < MIN_MONTHS_FOR_TREND:
        return {
            "months_available": len(months),
            "trend": "insufficient_data",
        }

    ys = [trend_dict[m] for m in months]
    xs = list(range(len(months)))

    slope, intercept = theil_sen(xs, ys)
    pct_change = robust_pct_change(ys)

    if pct_change >= TREND_THRESHOLD_PCT:
        trend = "rising"
    elif pct_change <= -TREND_THRESHOLD_PCT:
        trend = "falling"
    else:
        trend = "stable"

    naive_forecast = slope * len(months) + intercept  # one step past the last point

    return {
        "months_available": len(months),
        "pct_change_over_window": round(pct_change, 2),
        "trend": trend,
        "latest_month": months[-1],
        "latest_salary": round(ys[-1], 2),
        "naive_next_month_forecast": round(naive_forecast, 2),
    }


def main():
    if not LATEST_FILE.exists():
        raise SystemExit(f"{LATEST_FILE} not found — run adzuna_uk_tracker.py first.")

    latest = json.loads(LATEST_FILE.read_text())

    predictions = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "linear trend over available months, threshold-classified; "
                  "forecast is a naive one-step linear extrapolation, not a "
                  "validated model",
        "sectors": [],
    }

    for sector in latest.get("sectors", []):
        analysis = analyze_sector(sector.get("salary_trend_uk", {}))
        predictions["sectors"].append(
            {
                "label": sector["label"],
                "tag": sector["tag"],
                **analysis,
            }
        )

    DATA_DIR.mkdir(exist_ok=True)
    PREDICTIONS_FILE.write_text(json.dumps(predictions, indent=2))
    print(f"Wrote {PREDICTIONS_FILE}")

    rising = [s["label"] for s in predictions["sectors"] if s.get("trend") == "rising"]
    falling = [s["label"] for s in predictions["sectors"] if s.get("trend") == "falling"]
    print(f"Rising: {rising}")
    print(f"Falling: {falling}")


if __name__ == "__main__":
    main()
