# Capital model change — shared pool to independent slots

**2026-10-10. Nothing was deleted; the previous run is archived in full.**

## What changed

| | Before (schema 1) | After (schema 2) |
|---|---|---|
| Structure | one shared 100 SAR pool | **25 independent 100 SAR accounts** |
| Notional | 100 SAR | **2,500 SAR** |
| Stake per trade | 4% of current pooled equity (~4 SAR) | **fixed 100 SAR** |
| Profit | pooled — every slot drew on the same cash | **never shared between slots** |
| Loss | shrank the pool, so it shrank everyone's next stake | **confined to the slot that took it** |

## Why

This is a fidelity fix, not a preference.

`PREREGISTRATION.md` section 3 measured the edge with "every ticker evaluated
independently… no shared capital pool, because this study measures the *edge*,
not a capital model." The shared pool was therefore a divergence between what was
measured and what was being simulated: pooled capital couples every trade to every
other trade's outcome, which is precisely the coupling the study excluded.

Independent slots remove that coupling. The simulation now matches the measurement.

## Why the stake is fixed rather than compounding

Every trade commits exactly 100 SAR regardless of what that slot has made or lost.
Realized P/L accrues *beside* the working capital instead of being added to it.

For a study whose purpose is estimating a per-trade edge, this is the right choice:
compounding makes the final number depend on the order trades happened to arrive in,
which says nothing about whether the rule works. Fixed sizing keeps the mean per-trade
return an unbiased estimate of the thing being measured.

Set `capital_model.compound_per_slot` to `true` in `config.json` to compound instead.
The engine supports it and it is covered by tests.

## What happened to the old run

The shared-pool run is preserved unchanged:

- `paper/ledger-v1-shared-pool.csv` — 88 fills, 36 closed trades
- `paper/skipped-v1-shared-pool.csv`
- `paper/state-v1-shared-pool.json` — final state, 16 positions open

**Its per-trade percentage returns remain valid evidence** — position sizing does not
affect a percentage return, and the rule, signals and execution model were identical.
What does not carry across is the SAR accounting: cash, equity, purification and the
equity curve are all on a different basis, so mixing them would be meaningless.

The v1 result, for the record: **36 closed trades, −0.94% per trade, 50.0% win rate,
−1.37 SAR realized**, of which a single day (2026-10-09, −1.43 SAR across 9 trades)
accounted for more than the entire loss. With a 95% CI of −2.34% to +0.46% per trade,
it neither confirmed nor refuted the research estimate of +0.32%.

Schema 2 starts flat. The 16 open positions were not carried over; re-sizing them from
~4 SAR to 100 SAR would have invented entry prices the rule never traded at. The rule
re-signals naturally on the next cycle.

## What did not change

The strategy, thresholds (RSI 30/70), universe (150 tickers), next-session execution,
cost model (0.05% round trip), purification rate (10% of realized net profit only),
the once-per-bar guard, and the **NOT GENERALIZED** holdout verdict. No parameter was
tuned and no trade is selected by profitability.
