"""Apply the pre-registered universe filters. Training-period data only.

Selection must not see validation or holdout data, or the universe itself becomes a
fitted parameter. Every threshold here is copied from PREREGISTRATION.md section 5 and
none was changed after results existed.
"""
import json
import os
from datetime import datetime, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TRAIN_LO, TRAIN_HI = "2010-01-01", "2017-12-31"
MIN_PRICE = 5.0
MIN_DOLLAR_VOL = 50_000_000.0
TOP_N = 150
# Require near-complete history: ~2013 US trading days in 2010-2017.
MIN_TRAIN_BARS = int(2013 * 0.95)


def main():
    manifest = json.load(open(os.path.join(ROOT, "universe", "data_manifest.json")))
    rows_out, rejected = [], {"history": 0, "price": 0, "liquidity": 0}

    for t in sorted(manifest["tickers"]):
        path = os.path.join(ROOT, "data_cache", f"{t}.json")
        if not os.path.exists(path):
            continue
        bars = json.load(open(path))
        tr = [b for b in bars if TRAIN_LO <= b[0] <= TRAIN_HI]
        if len(tr) < MIN_TRAIN_BARS or not tr or tr[0][0] > "2010-01-08":
            rejected["history"] += 1
            continue
        closes = np.array([b[1] for b in tr], dtype=float)
        vols = np.array([b[2] for b in tr], dtype=float)
        med_px = float(np.median(closes))
        med_dv = float(np.median(closes * vols))
        if med_px <= MIN_PRICE:
            rejected["price"] += 1
            continue
        if med_dv <= MIN_DOLLAR_VOL:
            rejected["liquidity"] += 1
            continue
        rows_out.append({
            "ticker": t,
            "train_bars": len(tr),
            "median_close": round(med_px, 2),
            "median_dollar_vol": round(med_dv, 0),
        })

    rows_out.sort(key=lambda r: -r["median_dollar_vol"])
    selected = rows_out[:TOP_N]

    out = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "criteria": {
            "listing": "US common stock, NYSE/NASDAQ, one share class per issuer",
            "history": f"continuous daily bars covering {TRAIN_LO}..{TRAIN_HI} "
                       f"(>= {MIN_TRAIN_BARS} bars, first bar on/before 2010-01-08)",
            "min_median_close_usd": MIN_PRICE,
            "min_median_dollar_volume_usd": MIN_DOLLAR_VOL,
            "ranking": "median training-period dollar volume, descending",
            "top_n": TOP_N,
            "measured_on": f"TRAINING PERIOD ONLY ({TRAIN_LO}..{TRAIN_HI})",
        },
        "candidates_considered": len(manifest["tickers"]),
        "passed_filters": len(rows_out),
        "rejected": rejected,
        "selected_count": len(selected),
        "tickers": [r["ticker"] for r in selected],
        "detail": selected,
    }
    with open(os.path.join(ROOT, "universe", "universe.json"), "w") as fh:
        json.dump(out, fh, indent=1)

    print(f"candidates with data : {len(manifest['tickers'])}")
    print(f"rejected             : {rejected}")
    print(f"passed all filters   : {len(rows_out)}")
    print(f"SELECTED             : {len(selected)}")
    if selected:
        print(f"  most liquid : {selected[0]['ticker']} "
              f"(${selected[0]['median_dollar_vol']/1e9:.2f}B/day median)")
        print(f"  least liquid: {selected[-1]['ticker']} "
              f"(${selected[-1]['median_dollar_vol']/1e6:.0f}M/day median)")


if __name__ == "__main__":
    main()
