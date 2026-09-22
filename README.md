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


## Research-only paper-trading experiment

The dashboard at `docs/index.html` runs a declared paper simulation of the same frozen
rule on a hypothetical **100 SAR** account. It exists to watch an unvalidated rule
behave over time, not to establish anything.

**The strategy failed its holdout test.** Nothing produced by the simulation is
evidence of an edge, and a profitable stretch would be one unvalidated sample path.

| | |
|---|---|
| Account currency | SAR (canonical; no silent conversion) |
| FX | locked at initialization in `paper/config.json`, never re-read |
| Positions | max 10 concurrent, target 10% of equity each |
| Leverage / margin / shorting | none |
| Costs | 0.05% round trip (0.025% per side), charged on every fill |
| Purification | 10% of realized net paper profit only |
| Execution | signal on completed bar D, fill at the next eligible completed bar |
| Alerts | none - no notification path exists in this repository |

`paper/run_paper.py --audit` reprints the weekly and monthly rollups independently of
the dashboard.

## Layout

| Path | Purpose |
|---|---|
| `PREREGISTRATION.md` | The frozen study design |
| `universe/` | Selection criteria and the frozen ticker list |
| `research/` | Data fetch, backtest engine, study runner, isolation check |
| `results/` | Raw study output |
| `docs/` | The research dashboard (GitHub Pages) |
