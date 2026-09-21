"""RSI(2) breadth engine — frozen rule, no tunable parameters.

Every number the dashboard shows comes from here. See PREREGISTRATION.md; nothing in
this file may be changed after the first results commit without re-registering.
"""
import json
import os
from collections import defaultdict

import numpy as np

RSI_PERIOD = 2
ENTRY_BELOW = 30.0
EXIT_ABOVE = 70.0

SPLITS = {
    "train":      ("2010-01-01", "2017-12-31"),
    "validation": ("2018-01-01", "2021-12-31"),
    "holdout":    ("2022-01-01", "2099-12-31"),
}
COSTS = {"0.01%": 0.0001, "0.05%": 0.0005, "0.10%": 0.0010}


def wilder_rsi(closes, period=RSI_PERIOD):
    """Wilder's RSI. Identical maths to the live tracker's Get-RsiLast."""
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    d = np.diff(closes)
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag = gain[:period].mean()
    al = loss[:period].mean()
    out[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(period, n - 1):
        ag = (ag * (period - 1) + gain[i]) / period
        al = (al * (period - 1) + loss[i]) / period
        out[i + 1] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def trades_for(ticker, dates, closes):
    """Run the frozen state machine over the FULL history once.

    Running it per split would invent trades at each boundary. Trades are bucketed
    into splits afterwards, by entry-fill date.

    Next-session execution: a signal on the close of bar i is filled at the close of
    bar i+1.
    """
    rsi = wilder_rsi(closes)
    out = []
    pos = None  # (entry_idx, entry_px)
    n = len(closes)
    for i in range(n - 1):  # need i+1 to exist for the fill
        r = rsi[i]
        if np.isnan(r):
            continue
        if pos is None:
            if r < ENTRY_BELOW:
                pos = (i + 1, closes[i + 1])
        else:
            if r > EXIT_ABOVE:
                ei, epx = pos
                xi, xpx = i + 1, closes[i + 1]
                if xi > ei:
                    out.append({
                        "ticker": ticker,
                        "entry_date": dates[ei], "exit_date": dates[xi],
                        "entry_px": epx, "exit_px": xpx,
                        "hold": xi - ei,
                        "gross": (xpx / epx) - 1.0,
                    })
                pos = None
    return out


def load_universe(root):
    uni = json.load(open(os.path.join(root, "universe", "universe.json")))
    series = {}
    for t in uni["tickers"]:
        rows = json.load(open(os.path.join(root, "data_cache", f"{t}.json")))
        series[t] = ([r[0] for r in rows], np.array([r[1] for r in rows], dtype=float))
    return uni, series


def bucket(trades, split):
    lo, hi = SPLITS[split]
    return [t for t in trades if lo <= t["entry_date"] <= hi]


def net(trades, cost):
    return np.array([t["gross"] - cost for t in trades], dtype=float)


def date_clustered_bootstrap(trades, cost, reps=1000, seed=7):
    """Resample whole ENTRY DATES with replacement.

    Trades opened on the same day are largely one market bet; resampling individual
    trades would treat them as independent and produce a confidence interval that is
    far too narrow.
    """
    if not trades:
        return {"mean": None, "ci_lo": None, "ci_hi": None, "p_gt_zero": None}
    rng = np.random.default_rng(seed)
    groups = defaultdict(list)
    for t in trades:
        groups[t["entry_date"]].append(t["gross"] - cost)
    keys = list(groups)
    arrs = [np.array(groups[k]) for k in keys]
    real = float(np.mean(np.concatenate(arrs)))

    means = np.empty(reps)
    k = len(keys)
    for b in range(reps):
        pick = rng.integers(0, k, k)
        means[b] = np.concatenate([arrs[i] for i in pick]).mean()
    return {
        "mean": real,
        "ci_lo": float(np.percentile(means, 2.5)),
        "ci_hi": float(np.percentile(means, 97.5)),
        # one-sided: how often a resampled world shows no edge at all
        "p_gt_zero": float((means <= 0).mean()),
    }


def matched_random_control(trades, series, split, cost, reps=1000, seed=11):
    """Matched random-entry null, preserving date clustering.

    For each real trade the control holds the SAME ticker for the SAME number of
    sessions, entered at a random date inside the same split. To keep the null as
    correlated as the real strategy, one random shift is drawn per real entry DATE and
    applied to every trade opened that date - otherwise the null would be a portfolio of
    independent bets and would look artificially stable.
    """
    if not trades:
        return {"real": None, "pct": None, "p": None, "null_mean": None}
    rng = np.random.default_rng(seed)
    lo, hi = SPLITS[split]
    real = float(net(trades, cost).mean())

    by_date = defaultdict(list)
    for t in trades:
        by_date[t["entry_date"]].append(t)

    # Per ticker: indices whose date falls inside the split.
    idx_cache = {}
    for t in trades:
        if t["ticker"] in idx_cache:
            continue
        dates, closes = series[t["ticker"]]
        ok = [i for i, d in enumerate(dates) if lo <= d <= hi]
        idx_cache[t["ticker"]] = (ok, closes)

    null = np.empty(reps)
    for b in range(reps):
        vals = []
        for _date, group in by_date.items():
            u = rng.random()  # one shared draw per entry date
            for t in group:
                ok, closes = idx_cache[t["ticker"]]
                span = len(ok) - t["hold"] - 1
                if span <= 0:
                    continue
                i = ok[int(u * span)]
                j = i + t["hold"]
                if j >= len(closes):
                    continue
                vals.append((closes[j] / closes[i]) - 1.0 - cost)
        null[b] = np.mean(vals) if vals else 0.0

    return {
        "real": real,
        "null_mean": float(null.mean()),
        "pct": float((null < real).mean() * 100.0),
        "p": float((null >= real).mean()),  # one-sided empirical p
    }


def holm(pvals):
    """Holm-Bonferroni. Returns adjusted p-values in the original order."""
    items = sorted(enumerate(pvals), key=lambda kv: (kv[1] is None, kv[1]))
    m = sum(1 for _, p in items if p is not None)
    adj = [None] * len(pvals)
    running = 0.0
    rank = 0
    for i, p in items:
        if p is None:
            continue
        rank += 1
        val = min(1.0, (m - rank + 1) * p)
        running = max(running, val)  # enforce monotonicity
        adj[i] = running
    return adj
