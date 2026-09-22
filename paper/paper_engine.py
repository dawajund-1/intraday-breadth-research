"""Research-only paper-trading engine.

This simulates the frozen RSI(2) rule on a single shared 100 SAR paper account.
It failed its holdout validation. Nothing here is evidence of an edge, and
nothing here may be traded with real money.

Hard rules enforced in code, not just documentation:
  * No broker, no order API, no real-money instruction. This module only ever
    reads price history and writes local files.
  * No notification of any kind. There is no alert path to disable because none
    is written.
  * Decisions come only from COMPLETED daily bars.
  * One decision cycle per completed bar, guarded durably in state.
  * Signals on bar D execute on the NEXT eligible completed bar. No look-ahead.
  * No leverage, no margin, no shorting. Cash can never go negative.
  * FX is locked at initialization and never re-read from a live rate.
"""
import csv
import json
import os
from datetime import datetime, timezone

RSI_PERIOD = 2
ENTRY_BELOW = 30.0
EXIT_ABOVE = 70.0
DUST_SAR = 1.0  # below this, an entry is skipped rather than creating dust


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
    """Drop a bar dated today that has not closed yet.

    A daily rule must never see a forming bar: intraday RSI wanders across the
    threshold and back, which would invent trades the study never measured.
    """
    if not bars:
        return bars
    today = now_utc.strftime("%Y-%m-%d")
    if bars[-1][0] != today:
        return bars                      # already a settled prior session
    hh, mm = (int(x) for x in market_close_utc.split(":"))
    if (now_utc.hour, now_utc.minute) >= (hh, mm):
        return bars                      # session is over, bar is final
    return bars[:-1]


def bar_id_of(bars):
    return bars[-1][0] if bars else None


# --------------------------------------------------------------------------
# account helpers - every figure below is SAR, the canonical account currency
# --------------------------------------------------------------------------

def to_sar(price_usd, cfg):
    """Convert at the rate LOCKED at initialization.

    The rate is read from config and never from a live feed, so a paper result
    can never move because the exchange rate moved.
    """
    return price_usd * cfg["account"]["fx"]["sar_per_usd"]


def equity_sar(state, prices_usd, cfg):
    """Cash plus positions marked at the given closes."""
    eq = state["cash_sar"]
    for p in state["positions"]:
        px = prices_usd.get(p["ticker"])
        if px is None:
            px = p["entry_px_usd"]       # stale mark rather than a fabricated one
        eq += p["shares"] * to_sar(px, cfg)
    return eq


def new_state(cfg):
    return {
        "schema": 1,
        "initialized_utc": utcnow(),
        "bar_guard": {"bar_id": None, "decided": False},
        "cash_sar": float(cfg["account"]["initial_capital"]),
        "positions": [],
        "pending_orders": [],
        "realized_pl_sar": 0.0,
        "purified_sar": 0.0,
        "closed_trades": 0,
        "last_run_utc": None,
        "last_decided_bar": None,
    }


def _order_id(action, ticker, signal_bar):
    """Deterministic, so a re-derived decision is recognised, never repeated."""
    return "%s:%s:%s" % (action, ticker, signal_bar)


# --------------------------------------------------------------------------
# the one decision cycle
# --------------------------------------------------------------------------

def run_cycle(state, cfg, bars_by_ticker, now_utc, ledger_rows, skipped_rows):
    """Process exactly one completed bar. Returns a dict describing what happened.

    Order matters and is deliberate:
      1. exits from orders queued on the previous bar
      2. entries from orders queued on the previous bar
      3. new signals observed on THIS bar, queued for the next one
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

    # ---- 1 & 2: execute orders queued on an earlier bar --------------------
    still_pending = []
    for o in sorted(state["pending_orders"], key=lambda x: (x["action"] != "SELL", x["ticker"])):
        if o["signal_bar"] >= bar_id:
            still_pending.append(o)          # queued on this very bar; not yet eligible
            continue
        px = prices.get(o["ticker"])
        if px is None:
            still_pending.append(o)          # no price this session; try next eligible bar
            continue
        px_sar = to_sar(px, cfg)

        if o["action"] == "SELL":
            pos = next((p for p in state["positions"] if p["ticker"] == o["ticker"]), None)
            if pos is None:
                continue                     # already closed; drop silently
            proceeds = pos["shares"] * px_sar
            cost = proceeds * cost_rate
            net_proceeds = proceeds - cost
            gross_pl = proceeds - pos["basis_sar"]
            net_pl = net_proceeds - pos["basis_sar"]
            pur = net_pl * pur_rate if net_pl > 0 else 0.0
            state["cash_sar"] += net_proceeds - pur
            state["realized_pl_sar"] += net_pl
            state["purified_sar"] += pur
            state["closed_trades"] += 1
            state["positions"] = [p for p in state["positions"] if p["ticker"] != o["ticker"]]
            ledger_rows.append({
                "timestamp_utc": utcnow(), "bar_id": bar_id, "action": "SELL",
                "ticker": o["ticker"], "signal_bar": o["signal_bar"],
                "price_usd": round(px, 6), "price_sar": round(px_sar, 6),
                "shares": round(pos["shares"], 8),
                "cost_sar": round(cost, 6),
                "gross_pl_sar": round(gross_pl, 6), "net_pl_sar": round(net_pl, 6),
                "purification_sar": round(pur, 6),
                "cash_after_sar": round(state["cash_sar"], 6),
                "order_id": o["id"],
            })
            filled += 1

        elif o["action"] == "BUY":
            if any(p["ticker"] == o["ticker"] for p in state["positions"]):
                continue                     # already held
            if len(state["positions"]) >= cfg["capital_model"]["max_concurrent_positions"]:
                skipped_rows.append({"timestamp_utc": utcnow(), "bar_id": bar_id,
                                     "ticker": o["ticker"], "rsi": "",
                                     "reason": "no free slot at execution time"})
                continue
            eq = equity_sar(state, prices, cfg)
            target = eq * cfg["capital_model"]["target_position_pct"]
            spend = min(target, state["cash_sar"])
            if spend < DUST_SAR:
                skipped_rows.append({"timestamp_utc": utcnow(), "bar_id": bar_id,
                                     "ticker": o["ticker"], "rsi": "",
                                     "reason": "insufficient cash at execution time "
                                               "(%.4f SAR available)" % state["cash_sar"]})
                continue
            cost = spend * cost_rate
            invested = spend - cost
            shares = invested / px_sar
            state["cash_sar"] -= spend
            state["positions"].append({
                "ticker": o["ticker"], "entry_bar": bar_id,
                "entry_px_usd": px, "shares": shares,
                "basis_sar": invested, "opened_utc": utcnow(),
            })
            ledger_rows.append({
                "timestamp_utc": utcnow(), "bar_id": bar_id, "action": "BUY",
                "ticker": o["ticker"], "signal_bar": o["signal_bar"],
                "price_usd": round(px, 6), "price_sar": round(px_sar, 6),
                "shares": round(shares, 8),
                "cost_sar": round(cost, 6),
                "gross_pl_sar": "", "net_pl_sar": "", "purification_sar": "",
                "cash_after_sar": round(state["cash_sar"], 6),
                "order_id": o["id"],
            })
            filled += 1

    state["pending_orders"] = still_pending

    # ---- 3: observe THIS bar, queue for the next eligible one --------------
    held = {p["ticker"] for p in state["positions"]}
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

    # exits first: they are unconditional, and they free slots for the next bar
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

    # entries: lowest RSI first, ties alphabetical - fixed in advance so the
    # selection cannot be nudged after seeing results
    candidates = sorted(
        [(r, t) for t, r in rsis.items()
         if r < ENTRY_BELOW and t not in held and t not in pending_buys],
        key=lambda rt: (rt[0], rt[1]))

    max_pos = cfg["capital_model"]["max_concurrent_positions"]
    # A queued SELL still occupies its slot until it actually fills.
    committed = len(state["positions"]) + len([o for o in state["pending_orders"]
                                               if o["action"] == "BUY"])
    free = max(0, max_pos - committed)

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
                          "ranked #%d by RSI)" % (committed, max_pos, rank + 1)})

    state["bar_guard"] = {"bar_id": bar_id, "decided": True}
    state["last_decided_bar"] = bar_id
    state["last_run_utc"] = utcnow()

    return {"status": "decided", "bar_id": bar_id, "filled": filled,
            "queued": queued, "skipped": len(skipped_rows),
            "equity_sar": equity_sar(state, prices, cfg)}


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

LEDGER_FIELDS = ["timestamp_utc", "bar_id", "action", "ticker", "signal_bar",
                 "price_usd", "price_sar", "shares", "cost_sar", "gross_pl_sar",
                 "net_pl_sar", "purification_sar", "cash_after_sar", "order_id"]
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
