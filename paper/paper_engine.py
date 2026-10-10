"""Research-only paper-trading engine.

This simulates the frozen RSI(2) rule. It failed its holdout validation. Nothing
here is evidence of an edge, and nothing here may be traded with real money.

CAPITAL MODEL (schema 2, 2026-10-10) - INDEPENDENT SLOTS
--------------------------------------------------------
Each of the N slots is its own self-contained paper account holding a fixed
`capital_per_slot_sar`. Slots never share capital and never share profit: a win
in slot 3 cannot fund a position in slot 7, and a run of losses in one slot
cannot shrink any other slot's next trade.

This replaced a single shared pool, and it is a fidelity fix rather than a
preference. PREREGISTRATION.md section 3 measured the edge with "every ticker
evaluated independently... no shared capital pool", so the shared pool was a
divergence between what was measured and what is simulated. Independent slots
remove it.

Sizing is FIXED, not compounding: every trade commits exactly
`capital_per_slot_sar`, whatever that slot has made or lost so far. Realized P/L
accrues beside the working capital instead of being added to it. For a study
whose whole purpose is estimating a per-trade edge this is the right choice -
compounding makes the result depend on the order trades happened to arrive in,
which says nothing about whether the rule works. Set
`capital_model.compound_per_slot` to true to let each slot compound instead.

Hard rules enforced in code, not just documentation:
  * No broker, no order API, no real-money instruction. This module only ever
    reads price history and writes local files.
  * No notification of any kind. There is no alert path to disable because none
    is written.
  * Decisions come only from COMPLETED daily bars.
  * One decision cycle per completed bar, guarded durably in state.
  * Signals on bar D execute on the NEXT eligible completed bar. No look-ahead.
  * No leverage, no margin, no shorting.
  * FX is locked at initialization and never re-read from a live rate.
"""
import csv
import json
import os
from datetime import datetime, timezone

RSI_PERIOD = 2
ENTRY_BELOW = 30.0
EXIT_ABOVE = 70.0
SCHEMA = 2


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wilder_rsi_last(closes, period=RSI_PERIOD):
    """Wilder RSI of the final bar. Same maths as the research engine."""
    n = len(closes)
    if n < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    ag, al = gains / period, losses / period
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + (d if d > 0 else 0.0)) / period
        al = (al * (period - 1) + (-d if d < 0 else 0.0)) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + ag / al)


def remove_partial_bar(bars, now_utc, market_close_utc="20:00"):
    """Drop a bar dated today that has not closed yet."""
    if not bars:
        return bars
    today = now_utc.strftime("%Y-%m-%d")
    if bars[-1][0] != today:
        return bars
    hh, mm = (int(x) for x in market_close_utc.split(":"))
    if (now_utc.hour, now_utc.minute) >= (hh, mm):
        return bars
    return bars[:-1]


def bar_id_of(bars):
    return bars[-1][0] if bars else None


# --------------------------------------------------------------------------
# account helpers - every figure is SAR, the canonical account currency
# --------------------------------------------------------------------------

def to_sar(price_usd, cfg):
    """Convert at the rate LOCKED at initialization, never a live feed."""
    return price_usd * cfg["account"]["fx"]["sar_per_usd"]


def slot_stake(slot, cfg):
    """How much this slot commits to its next trade.

    Fixed by default: the slot's own history does not change its stake, which is
    what keeps the per-trade estimate free of path dependence.
    """
    base = float(cfg["capital_model"]["capital_per_slot_sar"])
    if cfg["capital_model"].get("compound_per_slot"):
        return max(0.0, base + slot["realized_pl_sar"] - slot["purified_sar"])
    return base


def slot_value(slot, cfg):
    """Working capital plus what this slot has kept, excluding open marks."""
    base = float(cfg["capital_model"]["capital_per_slot_sar"])
    if cfg["capital_model"].get("compound_per_slot"):
        return slot_stake(slot, cfg)
    return base + slot["realized_pl_sar"] - slot["purified_sar"]


def equity_sar(state, prices_usd, cfg):
    """Total across all slots, including open positions marked to the close."""
    total = 0.0
    for s in state["slots"]:
        total += slot_value(s, cfg)
        p = s.get("position")
        if p:
            px = prices_usd.get(p["ticker"], p["entry_px_usd"])
            total += p["shares"] * to_sar(px, cfg) - p["basis_sar"]
    return total


def new_state(cfg):
    n = int(cfg["capital_model"]["slots"])
    return {
        "schema": SCHEMA,
        "initialized_utc": utcnow(),
        "bar_guard": {"bar_id": None, "decided": False},
        "slots": [{
            "id": i + 1,
            "position": None,
            "realized_pl_sar": 0.0,
            "purified_sar": 0.0,
            "closed_trades": 0,
        } for i in range(n)],
        "pending_orders": [],
        "last_run_utc": None,
        "last_decided_bar": None,
    }


def held_tickers(state):
    return {s["position"]["ticker"] for s in state["slots"] if s.get("position")}


def free_slots(state):
    return [s for s in state["slots"] if not s.get("position")]


def totals(state):
    return {
        "realized_pl_sar": sum(s["realized_pl_sar"] for s in state["slots"]),
        "purified_sar": sum(s["purified_sar"] for s in state["slots"]),
        "closed_trades": sum(s["closed_trades"] for s in state["slots"]),
        "open_positions": sum(1 for s in state["slots"] if s.get("position")),
    }


def _order_id(action, ticker, signal_bar):
    """Deterministic, so a re-derived decision is recognised, never repeated."""
    return "%s:%s:%s" % (action, ticker, signal_bar)


# --------------------------------------------------------------------------
# the one decision cycle
# --------------------------------------------------------------------------

def run_cycle(state, cfg, bars_by_ticker, now_utc, ledger_rows, skipped_rows):
    """Process exactly one completed bar.

    Order: exits queued earlier, then entries queued earlier, then new signals
    observed on THIS bar queued for the next one.
    """
    prices = {t: b[-1][1] for t, b in bars_by_ticker.items() if b}
    bar_ids = {bar_id_of(b) for b in bars_by_ticker.values() if b}
    bar_ids.discard(None)
    if not bar_ids:
        return {"status": "no_bars", "bar_id": None}
    bar_id = max(bar_ids)

    guard = state.get("bar_guard") or {"bar_id": None, "decided": False}
    if guard.get("bar_id") == bar_id and guard.get("decided"):
        return {"status": "already_decided", "bar_id": bar_id,
                "filled": 0, "queued": 0, "skipped": 0}

    filled = queued = 0
    cost_rate = cfg["costs"]["per_side_pct"]
    pur_rate = cfg["purification"]["rate"]

    # ---- execute orders queued on an earlier bar ---------------------------
    still_pending = []
    for o in sorted(state["pending_orders"],
                    key=lambda x: (x["action"] != "SELL", x["ticker"])):
        if o["signal_bar"] >= bar_id:
            still_pending.append(o)          # queued on this very bar
            continue
        px = prices.get(o["ticker"])
        if px is None:
            still_pending.append(o)          # no price; try the next bar
            continue
        px_sar = to_sar(px, cfg)

        if o["action"] == "SELL":
            slot = next((s for s in state["slots"]
                         if s.get("position") and s["position"]["ticker"] == o["ticker"]), None)
            if slot is None:
                continue                     # already closed; drop silently
            pos = slot["position"]
            proceeds = pos["shares"] * px_sar
            cost = proceeds * cost_rate
            net_proceeds = proceeds - cost
            gross_pl = proceeds - pos["basis_sar"]
            net_pl = net_proceeds - pos["basis_sar"]
            pur = net_pl * pur_rate if net_pl > 0 else 0.0

            # Credited to THIS slot only. No other slot is touched.
            slot["realized_pl_sar"] += net_pl
            slot["purified_sar"] += pur
            slot["closed_trades"] += 1
            slot["position"] = None

            ledger_rows.append({
                "timestamp_utc": utcnow(), "bar_id": bar_id, "action": "SELL",
                "slot": slot["id"], "ticker": o["ticker"], "signal_bar": o["signal_bar"],
                "price_usd": round(px, 6), "price_sar": round(px_sar, 6),
                "shares": round(pos["shares"], 8), "stake_sar": round(pos["stake_sar"], 6),
                "cost_sar": round(cost, 6),
                "gross_pl_sar": round(gross_pl, 6), "net_pl_sar": round(net_pl, 6),
                "purification_sar": round(pur, 6),
                "slot_realized_after_sar": round(slot["realized_pl_sar"], 6),
                "order_id": o["id"],
            })
            filled += 1

        elif o["action"] == "BUY":
            if o["ticker"] in held_tickers(state):
                continue                     # already held in some slot
            free = free_slots(state)
            if not free:
                skipped_rows.append({"timestamp_utc": utcnow(), "bar_id": bar_id,
                                     "ticker": o["ticker"], "rsi": "",
                                     "reason": "no free slot at execution time"})
                continue
            slot = free[0]
            stake = slot_stake(slot, cfg)
            if stake <= 0:
                skipped_rows.append({"timestamp_utc": utcnow(), "bar_id": bar_id,
                                     "ticker": o["ticker"], "rsi": "",
                                     "reason": "slot %d has no capital left" % slot["id"]})
                continue
            cost = stake * cost_rate
            invested = stake - cost
            shares = invested / px_sar
            slot["position"] = {
                "ticker": o["ticker"], "entry_bar": bar_id,
                "entry_px_usd": px, "shares": shares,
                "basis_sar": invested, "stake_sar": stake,
                "opened_utc": utcnow(),
            }
            ledger_rows.append({
                "timestamp_utc": utcnow(), "bar_id": bar_id, "action": "BUY",
                "slot": slot["id"], "ticker": o["ticker"], "signal_bar": o["signal_bar"],
                "price_usd": round(px, 6), "price_sar": round(px_sar, 6),
                "shares": round(shares, 8), "stake_sar": round(stake, 6),
                "cost_sar": round(cost, 6),
                "gross_pl_sar": "", "net_pl_sar": "", "purification_sar": "",
                "slot_realized_after_sar": round(slot["realized_pl_sar"], 6),
                "order_id": o["id"],
            })
            filled += 1

    state["pending_orders"] = still_pending

    # ---- observe THIS bar, queue for the next eligible one -----------------
    held = held_tickers(state)
    pending_ids = {o["id"] for o in state["pending_orders"]}
    pending_buys = {o["ticker"] for o in state["pending_orders"] if o["action"] == "BUY"}
    pending_sells = {o["ticker"] for o in state["pending_orders"] if o["action"] == "SELL"}

    rsis = {}
    for t, b in bars_by_ticker.items():
        if not b or bar_id_of(b) != bar_id:
            continue
        r = wilder_rsi_last([x[1] for x in b])
        if r is not None:
            rsis[t] = r

    for t in sorted(held):
        r = rsis.get(t)
        if r is not None and r > EXIT_ABOVE and t not in pending_sells:
            oid = _order_id("SELL", t, bar_id)
            if oid not in pending_ids:
                state["pending_orders"].append({
                    "id": oid, "action": "SELL", "ticker": t,
                    "signal_bar": bar_id, "rsi": round(r, 4), "created_utc": utcnow()})
                pending_ids.add(oid)
                queued += 1

    # entries: lowest RSI first, ties alphabetical - fixed in advance
    candidates = sorted(
        [(r, t) for t, r in rsis.items()
         if r < ENTRY_BELOW and t not in held and t not in pending_buys],
        key=lambda rt: (rt[0], rt[1]))

    n_slots = int(cfg["capital_model"]["slots"])
    committed = len(held) + len(pending_buys)
    free = max(0, n_slots - committed)

    for rank, (r, t) in enumerate(candidates):
        if rank < free:
            oid = _order_id("BUY", t, bar_id)
            if oid in pending_ids:
                continue
            state["pending_orders"].append({
                "id": oid, "action": "BUY", "ticker": t,
                "signal_bar": bar_id, "rsi": round(r, 4), "created_utc": utcnow()})
            pending_ids.add(oid)
            queued += 1
        else:
            skipped_rows.append({
                "timestamp_utc": utcnow(), "bar_id": bar_id, "ticker": t,
                "rsi": round(r, 4),
                "reason": "valid entry signal but no free slot (%d/%d used, "
                          "ranked #%d by RSI)" % (committed, n_slots, rank + 1)})

    state["bar_guard"] = {"bar_id": bar_id, "decided": True}
    state["last_decided_bar"] = bar_id
    state["last_run_utc"] = utcnow()

    t = totals(state)
    return {"status": "decided", "bar_id": bar_id, "filled": filled,
            "queued": queued, "skipped": len(skipped_rows),
            "equity_sar": equity_sar(state, prices, cfg),
            "open_positions": t["open_positions"]}


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

LEDGER_FIELDS = ["timestamp_utc", "bar_id", "action", "slot", "ticker", "signal_bar",
                 "price_usd", "price_sar", "shares", "stake_sar", "cost_sar",
                 "gross_pl_sar", "net_pl_sar", "purification_sar",
                 "slot_realized_after_sar", "order_id"]
SKIPPED_FIELDS = ["timestamp_utc", "bar_id", "ticker", "rsi", "reason"]


def append_csv(path, fields, rows):
    if not rows:
        return
    exists = os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
