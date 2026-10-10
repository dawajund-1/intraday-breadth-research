"""Tests for the research-only paper engine (schema 2, independent slots).

No network, no git, no real topic. The publisher and sender seams used by the
live tracker do not exist here - this project has no notification path at all.

    python paper/tests_paper.py
"""
import json
import os
import sys
from datetime import date, timedelta, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_engine as pe
from rollups import iso_week, rollup

PASS = FAIL = 0
FAILURES = []


def ok(cond, name):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        FAILURES.append(name)
        print("  FAIL  %s" % name)


def eq(a, b, name, tol=1e-6):
    good = (abs(a - b) <= tol) if isinstance(a, float) or isinstance(b, float) else a == b
    ok(good, "%s (expected %s, got %s)" % (name, b, a))


def cfg_fixture(slots=25, per_slot=100.0, compound=False):
    return {
        "account": {"initial_capital": per_slot, "currency": "SAR",
                    "fx": {"sar_per_usd": 3.75, "locked_utc": "2026-10-10T00:00:00Z"}},
        "capital_model": {"mode": "independent_slots", "slots": slots,
                          "capital_per_slot_sar": per_slot,
                          "compound_per_slot": compound,
                          "leverage": False, "margin": False, "shorting": False,
                          "fractional_shares": True},
        "costs": {"round_trip_pct": 0.0005, "per_side_pct": 0.00025},
        "purification": {"rate": 0.10},
    }


def ramp(start_date, prices):
    d = date.fromisoformat(start_date)
    out = []
    for p in prices:
        out.append([d.isoformat(), float(p)])
        d += timedelta(days=1)
    return out


NOW = datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc)
COLLAPSE = [100, 90, 80, 70]     # RSI(2) -> ~0
SPIKE = 400.0                    # RSI(2) -> ~100


def cycle(state, cfg, bars, led=None, skp=None):
    led = [] if led is None else led
    skp = [] if skp is None else skp
    return pe.run_cycle(state, cfg, bars, NOW, led, skp), led, skp


print("\n=== FX locked at initialization ===")
c = cfg_fixture()
eq(pe.to_sar(100.0, c), 375.0, "100 USD converts at the locked 3.75")
c2 = cfg_fixture(); c2["account"]["fx"]["sar_per_usd"] = 4.10
eq(pe.to_sar(100.0, c2), 410.0, "conversion follows config, not a live feed")

print("\n=== Partial-bar removal ===")
bars = ramp("2026-10-08", [10, 11, 12])      # last bar dated 2026-10-10 = today
before = pe.remove_partial_bar(bars, datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc))
after = pe.remove_partial_bar(bars, datetime(2026, 10, 10, 20, 30, tzinfo=timezone.utc))
eq(len(before), 2, "a forming bar is dropped before the close")
eq(pe.bar_id_of(before), "2026-10-09", "the prior completed bar is used instead")
eq(len(after), 3, "after the close the bar is final and kept")

print("\n=== The account is 25 separate 100 SAR accounts ===")
c = cfg_fixture()
st = pe.new_state(c)
eq(len(st["slots"]), 25, "25 slots exist")
eq(pe.equity_sar(st, {}, c), 2500.0, "total notional is 25 x 100 = 2500 SAR")
ok(all(s["position"] is None for s in st["slots"]), "every slot starts flat")
ok(all(s["realized_pl_sar"] == 0 for s in st["slots"]), "every slot starts at zero P/L")
eq(pe.slot_stake(st["slots"][0], c), 100.0, "each slot stakes 100 SAR")

print("\n=== Entry: stake, cost, fractional shares ===")
d = ramp("2026-10-01", COLLAPSE)
r, led, skp = cycle(st, c, {"AAA": d})
eq(r["queued"], 1, "a low-RSI name queues one BUY")
eq(r["filled"], 0, "nothing fills on the signal bar (no look-ahead)")
d2 = d + [["2026-10-05", 60.0]]
r, led, skp = cycle(st, c, {"AAA": d2}, led, skp)
eq(r["filled"], 1, "the BUY fills on the next completed bar")
slot = next(s for s in st["slots"] if s.get("position"))
pos = slot["position"]
eq(pos["stake_sar"], 100.0, "the trade commits exactly 100 SAR")
eq(pos["basis_sar"], 100.0 - 100.0 * 0.00025, "entry cost comes out of the stake")
eq(round(pos["shares"], 8), round((100.0 - 0.025) / (60.0 * 3.75), 8),
   "fractional shares priced in SAR at the locked rate")
buy = [x for x in led if x["action"] == "BUY"][0]
eq(float(buy["cost_sar"]), 0.025, "the entry cost is on the trade row")
eq(buy["slot"], slot["id"], "the trade row records which slot took it")

print("\n=== Exit: P/L and purification land in that slot only ===")
d3 = d2 + [["2026-10-06", SPIKE]]
r, led, skp = cycle(st, c, {"AAA": d3}, led, skp)
eq(r["queued"], 1, "a high-RSI holding queues one SELL")
d4 = d3 + [["2026-10-07", SPIKE + 10]]
r, led, skp = cycle(st, c, {"AAA": d4}, led, skp)
eq(r["filled"], 1, "the SELL fills on the next completed bar")
winner = next(s for s in st["slots"] if s["closed_trades"] == 1)
ok(winner["realized_pl_sar"] > 0, "the winning slot booked a profit")
others = [s for s in st["slots"] if s["id"] != winner["id"]]
ok(all(s["realized_pl_sar"] == 0 for s in others), "NO other slot received any of it")
ok(all(s["purified_sar"] == 0 for s in others), "no other slot was purified")
ok(all(s["closed_trades"] == 0 for s in others), "no other slot's trade count moved")
sell = [x for x in led if x["action"] == "SELL"][0]
eq(float(sell["purification_sar"]), round(winner["realized_pl_sar"] * 0.10, 6),
   "purification is 10% of that slot's realized net profit")

print("\n=== Profit does NOT change the next stake (fixed sizing) ===")
eq(pe.slot_stake(winner, c), 100.0,
   "after a win the slot still stakes exactly 100 SAR")
ok(winner["realized_pl_sar"] > 0, "...even though it is up on the trade")
eq(pe.slot_value(winner, c),
   100.0 + winner["realized_pl_sar"] - winner["purified_sar"],
   "the slot's VALUE reflects its profit, kept beside the working capital")

print("\n=== ...but compounding works when switched on ===")
cc = cfg_fixture(compound=True)
sc = pe.new_state(cc)
sc["slots"][0]["realized_pl_sar"] = 20.0
sc["slots"][0]["purified_sar"] = 2.0
eq(pe.slot_stake(sc["slots"][0], cc), 118.0, "a compounding slot stakes 100 + 20 - 2")
eq(pe.slot_stake(sc["slots"][1], cc), 100.0, "its neighbour is unaffected")

print("\n=== A losing slot cannot drag the others down ===")
c = cfg_fixture()
st = pe.new_state(c)
led = []
skp = []
dl = ramp("2026-10-01", COLLAPSE)
_, led, skp = cycle(st, c, {"BBB": dl}, led, skp)
dl = dl + [["2026-10-05", 60.0]]
_, led, skp = cycle(st, c, {"BBB": dl}, led, skp)      # fills at 60
dl = dl + [["2026-10-06", SPIKE]]
_, led, skp = cycle(st, c, {"BBB": dl}, led, skp)      # queues SELL
dl = dl + [["2026-10-07", 1.0]]
_, led, skp = cycle(st, c, {"BBB": dl}, led, skp)      # fills SELL at 1.0 -> big loss
loser = next(s for s in st["slots"] if s["closed_trades"] == 1)
ok(loser["realized_pl_sar"] < 0, "the slot took a heavy loss")
eq(loser["purified_sar"], 0.0, "no purification is taken on a loss")
eq(pe.slot_stake(loser, c), 100.0, "the damaged slot STILL stakes a full 100 SAR")
ok(all(s["realized_pl_sar"] == 0 for s in st["slots"] if s["id"] != loser["id"]),
   "every other slot is untouched by the loss")
eq(pe.equity_sar(st, {}, c), 2500.0 + loser["realized_pl_sar"],
   "total equity falls by exactly that one slot's loss, no more")

print("\n=== Allocation: 25 slots, lowest RSI first, ties alphabetical ===")
c = cfg_fixture()
st = pe.new_state(c)
bars = {}
for i in range(30):
    bars["T%02d" % i] = ramp("2026-10-01", [100, 80, 60, 50 - i])
r, led, skp = cycle(st, c, bars)
queued = [o["ticker"] for o in st["pending_orders"] if o["action"] == "BUY"]
eq(len(queued), 25, "exactly 25 entries are queued for 25 slots")
eq(len(skp), 5, "the other 5 valid signals are recorded as skipped")
ok(all("no free slot" in s["reason"] for s in skp), "each skip records why")
taken_rsi = max(o["rsi"] for o in st["pending_orders"])
ok(taken_rsi <= min(float(s["rsi"]) for s in skp) + 1e-9,
   "every taken signal has an RSI at or below every skipped one")

c1 = cfg_fixture(slots=1)
st1 = pe.new_state(c1)
tie = ramp("2026-10-01", [100, 80, 60, 40])
_, _, skp1 = cycle(st1, c1, {"ZZZ": list(tie), "AAA": list(tie), "MMM": list(tie)})
eq([o["ticker"] for o in st1["pending_orders"]], ["AAA"],
   "identical RSI breaks alphabetically")
eq(sorted(s["ticker"] for s in skp1), ["MMM", "ZZZ"], "the rest are skipped")

print("\n=== No leverage, no shorting, one ticker per account ===")
c = cfg_fixture()
st = pe.new_state(c)
bars = {("N%02d" % i): ramp("2026-10-01", [100, 80, 60, 50 - i]) for i in range(25)}
_, led, skp = cycle(st, c, bars)
bars2 = {k: v + [["2026-10-05", 40.0]] for k, v in bars.items()}
_, led, skp = cycle(st, c, bars2, led, skp)
eq(sum(1 for s in st["slots"] if s.get("position")), 25, "all 25 slots filled")
ok(all(s["position"]["shares"] > 0 for s in st["slots"] if s.get("position")),
   "no short (negative) position exists")
ok(all(s["position"]["stake_sar"] == 100.0 for s in st["slots"] if s.get("position")),
   "every position staked exactly its own 100 SAR, never another slot's")
tickers = [s["position"]["ticker"] for s in st["slots"] if s.get("position")]
eq(len(set(tickers)), 25, "no ticker is held in two slots at once")

print("\n=== Same-bar rerun and duplicate prevention ===")
c = cfg_fixture()
st = pe.new_state(c)
d = ramp("2026-10-01", COLLAPSE)
r, led, skp = cycle(st, c, {"CCC": d})
eq(r["status"], "decided", "the first run decides the bar")
n_orders = len(st["pending_orders"])
for _ in range(50):
    r, led, skp = cycle(st, c, {"CCC": d}, led, skp)
eq(r["status"], "already_decided", "every rerun on the same bar is a no-op")
eq(len(st["pending_orders"]), n_orders, "reruns queue nothing extra")
eq(len(led), 0, "reruns write no ledger row")

print("\n=== Weekends and holidays ===")
c = cfg_fixture()
st = pe.new_state(c)
fri = ramp("2026-10-01", COLLAPSE)
r1, led, skp = cycle(st, c, {"DDD": fri})
r2, led, skp = cycle(st, c, {"DDD": fri}, led, skp)
r3, led, skp = cycle(st, c, {"DDD": fri}, led, skp)
eq(r2["status"], "already_decided", "Saturday adds nothing")
eq(r3["status"], "already_decided", "a market holiday adds nothing")
r4, led, skp = cycle(st, c, {"DDD": fri + [["2026-10-06", 65.0]]}, led, skp)
eq(r4["status"], "decided", "the next real session decides normally")

print("\n=== Crash before persistence ===")
c = cfg_fixture()
saved = pe.new_state(c)
d = ramp("2026-10-01", COLLAPSE)
attempt = json.loads(json.dumps(saved))
_, led8, _ = cycle(attempt, c, {"EEE": d})
ok(len(led8) == 0 and len(attempt["pending_orders"]) == 1,
   "the lost run queued an order and wrote no ledger row")
redo = json.loads(json.dumps(saved))
_, led9, _ = cycle(redo, c, {"EEE": d})
eq(len(redo["pending_orders"]), 1, "the retry re-derives exactly one order")
eq(redo["pending_orders"][0]["id"], attempt["pending_orders"][0]["id"],
   "the order id is deterministic, so it cannot become a second order")

print("\n=== ISO weeks and rollups ===")
y, w, mon = iso_week(datetime(2026, 10, 10))
eq((y, w), (2026, 41), "2026-10-10 falls in ISO week 2026-W41")
eq(mon.isoformat(), "2026-10-05", "the ISO week starts on Monday")
y2, _, _ = iso_week(datetime(2027, 1, 1))
eq(y2, 2026, "1 Jan 2027 belongs to ISO year 2026")
ledger = [
    {"action": "BUY", "timestamp_utc": "2026-10-05T22:00:00Z", "net_pl_sar": "",
     "purification_sar": ""},
    {"action": "SELL", "timestamp_utc": "2026-10-06T22:00:00Z", "net_pl_sar": "2.0",
     "purification_sar": "0.2"},
    {"action": "SELL", "timestamp_utc": "2026-10-08T22:00:00Z", "net_pl_sar": "-1.0",
     "purification_sar": "0"},
    {"action": "SELL", "timestamp_utc": "2026-11-02T22:00:00Z", "net_pl_sar": "3.0",
     "purification_sar": "0.3"},
]
wk = rollup(ledger, [{"timestamp_utc": "2026-10-06T22:00:00Z"}], "week")
mo = rollup(ledger, [], "month")
eq(len(wk), 2, "two ISO weeks appear")
eq(wk[0]["closed"], 2, "BUY rows are excluded from closed-trade counts")
eq(wk[0]["wins"], 1, "one win in the first week")
eq(wk[0]["losses"], 1, "one loss in the first week")
eq(wk[0]["net_pl_sar"], 1.0, "weekly net P/L sums correctly")
eq(wk[0]["skipped"], 1, "skipped signals are bucketed by their own timestamp")
eq(wk[-1]["cumulative_sar"], 4.0, "cumulative runs across periods")
eq(len(mo), 2, "two calendar months appear")
eq(rollup([], [], "week"), [], "an empty ledger yields no rows, not invented ones")

print("\n=== Shipped config is internally consistent ===")
_cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "config.json"), encoding="utf-8"))
_cm = _cfg["capital_model"]
eq(_cm["mode"], "independent_slots", "shipped model is independent slots")
eq(_cm["slots"], 25, "25 slots")
eq(_cm["capital_per_slot_sar"], 100, "100 SAR per slot")
eq(_cm["total_notional_sar"], _cm["slots"] * _cm["capital_per_slot_sar"],
   "declared notional matches slots x per-slot")
ok(_cm["shared_capital"] is False and _cm["shared_profit"] is False,
   "the shipped config declares no shared capital and no shared profit")
ok(_cm["compound_per_slot"] is False, "shipped sizing is fixed, not compounding")
ok(_cm["leverage"] is False and _cm["margin"] is False and _cm["shorting"] is False,
   "leverage, margin and shorting are all off")
eq(_cfg["account"]["currency"], "SAR", "shipped currency is SAR")
ok(_cfg["account"]["fx"]["locked_utc"] is not None, "shipped FX rate is locked")
eq(_cfg["strategy"]["entry_below"], 30, "entry threshold is still 30")
eq(_cfg["strategy"]["exit_above"], 70, "exit threshold is still 70")
ok(_cfg["strategy"]["frozen"] is True, "strategy is still marked frozen")

print("\n=== No real-money or notification surface ===")
src = ""
for fn in ("paper_engine.py", "run_paper.py", "rollups.py"):
    src += open(os.path.join(os.path.dirname(os.path.abspath(__file__)), fn),
                encoding="utf-8").read()
banned = ["nt" + "fy", "alp" + "aca", "ib" + "kr", "place_" + "order",
          "submit_" + "order", "api_" + "key", "broker_" + "api",
          "web" + "hook", "sm" + "tp", "twi" + "lio"]
hits = [b for b in banned if b in src.lower()]
ok(not hits, "no broker, order or outbound-notification surface exists %s" % (hits or ""))

print("\n" + "-" * 46)
print("  passed: %d   failed: %d" % (PASS, FAIL))
if FAIL:
    for f in FAILURES:
        print("    - %s" % f)
    sys.exit(1)
print("  all green")
sys.exit(0)
