# Pre-registration — RSI(2) breadth study

**Registered: 2026-09-21 (UTC), before any result was computed.**

This file is committed *before* the data-fetching and analysis code is run. The git
history is the evidence: if this commit is not strictly earlier than the first results
commit, treat the whole study as exploratory and discard its p-values.

Nothing in this repository is approved for trading.

---

## 1. Question

The live US cloud tracker runs fixed RSI(2) mean reversion on daily bars over 8 mega-cap
names and produces roughly **110 round trips per year** (measured: 10 round trips in 23
trading days, 2026-08-20 to 2026-09-21), capital-constrained to one open position.

**Does the same fixed rule, unchanged, still work across a much broader liquid US
universe — and does breadth deliver about 10x the trade opportunities?**

This is deliberately a *generalization* test, not a discovery exercise. The rule is
frozen. Only the universe changes.

## 2. The rule (frozen — no optimization)

- Indicator: **Wilder RSI, period 2**, computed on daily closes.
- Entry: RSI(2) closes **below 30** while flat.
- Exit: RSI(2) closes **above 70** while long.
- Long only. No stops, no targets, no position sizing rules, no filters.
- **No parameter will be tuned.** 30/70/period-2 are inherited from the existing
  validated result and are not free parameters in this study. If any variant is ever
  tested, it must be added to the registry in section 9 and carried into the
  multiple-testing correction.

## 3. Execution model

- Signal is computed on the close of session **D**.
- The trade is filled at the close of session **D+1** (next-session execution).
- This is **stricter than the live tracker**, which fills at the close of bar D itself.
  The research therefore cannot flatter the live system by accident.
- Every ticker is evaluated independently. There is no shared capital pool and no
  one-position-at-a-time constraint, because this study measures the *edge*, not a
  capital model. The capital model is a separate decision (section 10).
- Returns are simple close-to-close per trade: `(exit_px / entry_px) - 1`, minus cost.

## 4. Costs

Round-trip cost is subtracted from every trade, at three pre-declared levels:

| Scenario | Round trip |
|---|---|
| Optimistic | 0.01% |
| Base | 0.05% |
| Pessimistic | 0.10% |

The prior validated result died above ~0.07% round-trip, so the 0.10% scenario is
expected to fail. It is included precisely so that the failure is visible rather than
quietly omitted.

## 5. Universe — objective criteria

Selected by rules, not by judgement, and computed **only from training-period data** so
that selection cannot peek at validation or holdout:

1. US-listed common stock (NYSE / NASDAQ), one share class per issuer.
2. Continuous daily history covering the entire training period.
3. Median training-period close **> $5**.
4. Median training-period dollar volume **> $50,000,000/day**.
5. Ranked by median training-period dollar volume; **top 150** retained.

The resulting list is frozen to `universe/universe.json` before any return is computed.

**Known limitation — survivorship bias.** The candidate list is drawn from issuers that
exist today, so companies that delisted or were acquired are absent. This biases results
**optimistically** and cannot be fixed with free data. It is a material limitation and
is reported alongside every result, not buried.

## 6. Periods

| Split | Range | Use |
|---|---|---|
| Train | 2010-01-01 → 2017-12-31 | Universe selection; sanity checks |
| Validation | 2018-01-01 → 2021-12-31 | Pre-holdout read |
| **Holdout** | 2022-01-01 → present | **Touched exactly once, at the end** |

The holdout is not examined, plotted, or summarized until train and validation are
complete and committed. Because no parameter is tuned, the split is a discipline
mechanism rather than an overfitting control, and is described as such.

## 7. Null model — matched random-entry controls

For each real trade the control draws a random entry date for the **same ticker** and
holds for the **same number of sessions**. This matches ticker, holding period, and the
overall period, so the only thing being tested is whether *RSI(2)'s timing* adds
anything beyond being long that name for that long.

- 1,000 control replications.
- Reported as the percentile of the real result within the control distribution, and as
  a one-sided empirical p-value.

## 8. Date-clustered resampling

Trades entered on the same calendar date are **not independent** — they are largely one
market bet. All significance testing therefore resamples **whole dates with
replacement**, keeping every trade on a date together, rather than resampling trades.

This is expected to widen confidence intervals substantially versus naive per-trade
resampling. That widening is the honest number.

## 9. Multiple-testing correction

Holm-Bonferroni across the **3 cost scenarios x 3 splits = 9** pre-declared tests.

The project's prior research registry (strategies already tested and rejected in the
`IntradayTracker` work: 5m RSI, Tadawul RSI, ORB_RVOL, VWAP_Trend, XSECT_RS, Regime)
is acknowledged as prior multiple testing. A new repository does not reset that counter.
This study's headline claim is therefore reported both uncorrected and corrected.

## 10. Pre-declared decision rule

The rule is judged to **generalize** only if, on the untouched holdout:

1. Mean net per-trade return at the **0.05% base cost** is positive, **and**
2. The date-clustered bootstrap p-value against the matched random-entry null is
   **< 0.05 after Holm correction**, **and**
3. The holdout produces at least **100 trades** (the threshold used elsewhere in this
   project), **and**
4. The sign of the effect is consistent across train, validation and holdout.

Anything less is reported as **not generalized**. A positive mean with a
non-significant p-value is a failure, not a "promising trend".

**Breadth claim:** judged met if the universe generates **>= 1,100 trade opportunities
per year**, i.e. ~10x the live tracker's ~110 round trips/year.

Note these are independent: breadth can succeed while the edge fails. That combination
means "many more chances to trade something that does not work", which is worse than
doing nothing.

## 11. What would falsify this

- Negative mean net return at 0.05% on the holdout.
- A percentile below ~95 against matched random entry.
- An effect that reverses sign between validation and holdout.
- An edge that exists only at 0.01% cost — that is a cost artifact, not an edge.

## 12. Out of scope for this repository

No trade alerts, no ntfy topic, no paper-trading ledger, no virtual balance, no
execution of any kind. This repo computes statistics and renders them. Converting it
into a paper-trading system requires explicit approval and a separate capital model.
