# intraday-breadth-research

**Research only — not approved for trading.**

A pre-registered test of one question: does the fixed RSI(2) daily mean-reversion rule
still work across a broad liquid US universe, and does that breadth deliver roughly 10x
the trade opportunities of the 8-name live tracker?

The rule is frozen. Nothing here is optimized. Read
[PREREGISTRATION.md](PREREGISTRATION.md) first — it was committed before any result
existed, and the git history is the proof.

## What this repository is not

It has no trading logic, no paper-trading ledger, no virtual balance, no alerts and no
notification topic. It computes statistics and renders them on a dashboard. It is fully
isolated from the live tracker: see `research/isolation_check.py`, which fails CI if any
reference to the live tracker's repository or notification topics appears here.

## Layout

| Path | Purpose |
|---|---|
| `PREREGISTRATION.md` | The frozen study design |
| `universe/` | Selection criteria and the frozen ticker list |
| `research/` | Data fetch, backtest engine, study runner, isolation check |
| `results/` | Raw study output |
| `docs/` | The research dashboard (GitHub Pages) |
