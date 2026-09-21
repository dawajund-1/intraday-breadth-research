"""Run the pre-registered study.

Usage:
  python research/run_study.py --splits train,validation   # pre-holdout pass
  python research/run_study.py --splits all                # adds the holdout

The holdout is run only after the train/validation results are committed, so the git
history shows it was genuinely untouched. Results accumulate into results/study.json.
"""
import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from engine import (COSTS, SPLITS, bucket, date_clustered_bootstrap, holm,
                    load_universe, matched_random_control, net, trades_for)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "results", "study.json")

# Measured on the live tracker, 2026-08-20..2026-09-21: 10 round trips / 23 sessions.
LIVE_TRIPS_PER_YEAR = 110


def trading_years(trades):
    if not trades:
        return 0.0
    ds = sorted({t["entry_date"] for t in trades})
    a = datetime.strptime(ds[0], "%Y-%m-%d")
    b = datetime.strptime(ds[-1], "%Y-%m-%d")
    return max((b - a).days / 365.25, 1e-9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--splits", default="train,validation")
    args = ap.parse_args()
    want = list(SPLITS) if args.splits == "all" else args.splits.split(",")

    uni, series = load_universe(ROOT)
    print(f"universe: {len(uni['tickers'])} tickers")

    all_trades = []
    for t in uni["tickers"]:
        dates, closes = series[t]
        all_trades.extend(trades_for(t, dates, closes))
    print(f"total trades generated (full history): {len(all_trades)}")

    prev = json.load(open(OUT)) if os.path.exists(OUT) else {"splits": {}}
    results = prev["splits"]

    for split in want:
        tr = bucket(all_trades, split)
        yrs = trading_years(tr)
        per_year = len(tr) / yrs if yrs else 0.0
        entry_dates = len({t["entry_date"] for t in tr})
        print(f"\n=== {split}: {len(tr)} trades over {yrs:.2f}y "
              f"({per_year:.0f}/yr, {entry_dates} distinct entry dates) ===")

        block = {
            "range": SPLITS[split],
            "trades": len(tr),
            "years": round(yrs, 2),
            "trades_per_year": round(per_year, 1),
            "distinct_entry_dates": entry_dates,
            "median_hold_sessions": int(np.median([t["hold"] for t in tr])) if tr else None,
            "tickers_traded": len({t["ticker"] for t in tr}),
            "costs": {},
        }
        for label, c in COSTS.items():
            r = net(tr, c)
            boot = date_clustered_bootstrap(tr, c)
            ctrl = matched_random_control(tr, series, split, c)
            block["costs"][label] = {
                "mean_net_per_trade": round(float(r.mean()), 6) if len(r) else None,
                "median_net_per_trade": round(float(np.median(r)), 6) if len(r) else None,
                "win_rate": round(float((r > 0).mean()), 4) if len(r) else None,
                "total_net_sum": round(float(r.sum()), 4) if len(r) else None,
                "bootstrap": {k: (round(v, 6) if isinstance(v, float) else v)
                              for k, v in boot.items()},
                "control": {k: (round(v, 6) if isinstance(v, float) else v)
                            for k, v in ctrl.items()},
            }
            print(f"  {label:>6}  mean/trade {r.mean()*100:+.4f}%  "
                  f"win {(r>0).mean()*100:.1f}%  "
                  f"boot95 [{boot['ci_lo']*100:+.4f}%, {boot['ci_hi']*100:+.4f}%]  "
                  f"vs random: {ctrl['pct']:.1f}th pct, p={ctrl['p']:.4f}")
        results[split] = block

    # Holm across every test computed so far (3 costs x however many splits exist).
    keys, praw = [], []
    for s in sorted(results):
        for label in COSTS:
            keys.append((s, label))
            praw.append(results[s]["costs"][label]["control"]["p"])
    for (s, label), p_adj in zip(keys, holm(praw)):
        results[s]["costs"][label]["control"]["p_holm"] = (
            round(p_adj, 6) if p_adj is not None else None)

    payload = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "universe_size": len(uni["tickers"]),
        "universe_criteria": uni["criteria"],
        "rule": {"indicator": "Wilder RSI(2) on daily closes",
                 "entry": "RSI < 30", "exit": "RSI > 70",
                 "execution": "next session close (signal on D, fill on D+1)",
                 "optimization": "none - all parameters frozen in advance"},
        "holm_tests": len([p for p in praw if p is not None]),
        "live_tracker_trips_per_year": LIVE_TRIPS_PER_YEAR,
        "splits": results,
        "holdout_examined": "holdout" in results,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump(payload, fh, indent=1)
    print(f"\nwrote {OUT}  (holdout examined: {payload['holdout_examined']})")


if __name__ == "__main__":
    main()
