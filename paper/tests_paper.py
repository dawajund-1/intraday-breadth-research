"""Tests for the research-only paper engine. No network, no git, no files touched
outside a temp directory.

    python paper/tests_paper.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

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


def cfg_fixture(**over):
    c = {
        "account": {"initial_capital": 100, "currency": "SAR",
                    "fx": {"sar_per_usd": 3.75, "locked_utc": "2026-09-22T00:00:00Z"}},
        "capital_model": {"max_concurrent_positions": 10, "target_position_pct": 0.10,
                          "leverage": False, "margin": False, "shorting": False,
                          "fractional_shares": True},
        "costs": {"round_trip_pct": 0.0005, "per_side_pct": 0.00025},
        "purification": {"rate": 0.10},
    }
    for k, v in over.items():
        c[k].update(v)
    return c


def series(dates_prices):
    return [[d, p] for d, p in dates_prices]


def ramp(start_date, prices):
    """Build a bar series with sequential business-ish dates."""
    from datetime import date, timedelta
    d = date.fromisoformat(start_date)
    out = []
    for p in prices:
        out.append([d.isoformat(), float(p)])
        d += timedelta(days=1)
    return out


NOW = datetime(2026, 9, 22, 22, 0, tzinfo=timezone.utc)

print("\n=== FX locked at initialization ===")
c = cfg_fixture()
eq(pe.to_sar(100.0, c), 375.0, "100 USD converts at the locked 3.75")
c2 = cfg_fixture(account={"fx": {"sar_per_usd": 4.10, "locked_utc": "x"},
                          "initial_capital": 100, "currency": "SAR"})
eq(pe.to_sar(100.0, c2), 410.0, "conversion follows config, not a live feed")
ok(pe.to_sar(1.0, c) == c["account"]["fx"]["sar_per_usd"],
   "no hidden rate is applied anywhere")

print("\n=== Partial-bar removal ===")
bars = ramp("2026-09-20", [10, 11, 12])          # last bar dated 2026-09-22 = 'today'
before = pe.remove_partial_bar(bars, datetime(2026, 9, 22, 19, 0, tzinfo=timezone.utc))
after = pe.remove_partial_bar(bars, datetime(2026, 9, 22, 20, 30, tzinfo=timezone.utc))
eq(len(before), 2, "a forming bar is dropped before the close")
eq(pe.bar_id_of(before), "2026-09-21", "the prior completed bar is used instead")
eq(len(after), 3, "after the close the bar is final and kept")
settled = ramp("2026-09-18", [10, 11])
eq(len(pe.remove_partial_bar(settled, NOW)), 2, "a settled series is untouched")

print("\n=== Entry, costs, sizing ===")
c = cfg_fixture()
st = pe.new_state(c)
eq(st["cash_sar"], 100.0, "account starts at 100 SAR")
# Bar 1: RSI very low -> queues a BUY. Bar 2: the BUY fills.
down = ramp("2026-09-14", [100, 90, 80, 70])     # collapsing -> RSI(2) ~0
led, skp = [], []
r1 = pe.run_cycle(st, c, {"AAA": down}, NOW, led, skp)
eq(r1["queued"], 1, "a low-RSI name queues one BUY")
eq(len(st["positions"]), 0, "nothing fills on the signal bar itself (no look-ahead)")
down2 = down + [["2026-09-18", 60.0]]
r2 = pe.run_cycle(st, c, {"AAA": down2}, NOW, led, skp)
eq(r2["filled"], 1, "the BUY fills on the next completed bar")
pos = st["positions"][0]
eq(pos["entry_px_usd"], 60.0, "filled at the NEXT bar's close, not the signal bar's")
# 10% of 100 SAR = 10 SAR; cost 0.025% = 0.0025; invested 9.9975
eq(st["cash_sar"], 90.0, "10% of equity is committed")
eq(pos["basis_sar"], 10.0 - 10.0 * 0.00025, "entry cost is deducted from the basis")
buy_row = [r for r in led if r["action"] == "BUY"][0]
eq(float(buy_row["cost_sar"]), 0.0025, "the entry cost is recorded on the trade row")
eq(round(pos["shares"], 6), round((10.0 - 0.0025) / (60.0 * 3.75), 6),
   "fractional shares priced in SAR at the locked rate")

print("\n=== Exit, realized P/L, purification ===")
up = down2 + [["2026-09-19", 200.0]]             # spike -> RSI(2) ~100
r3 = pe.run_cycle(st, c, {"AAA": up}, NOW, led, skp)
eq(r3["queued"], 1, "a high-RSI holding queues one SELL")
up2 = up + [["2026-09-20", 210.0]]
r4 = pe.run_cycle(st, c, {"AAA": up2}, NOW, led, skp)
eq(r4["filled"], 1, "the SELL fills on the next completed bar")
eq(len(st["positions"]), 0, "the position is closed")
sell = [r for r in led if r["action"] == "SELL"][0]
proceeds = pos["shares"] * 210.0 * 3.75
expect_net = proceeds * (1 - 0.00025) - pos["basis_sar"]
eq(float(sell["net_pl_sar"]), round(expect_net, 6), "net P/L is after both-side costs")
eq(float(sell["purification_sar"]), round(expect_net * 0.10, 6),
   "purification is 10% of realized net profit")
eq(st["closed_trades"], 1, "closed-trade count increments once")
ok(float(sell["gross_pl_sar"]) > float(sell["net_pl_sar"]),
   "gross exceeds net, so costs are really being charged")

print("\n=== Purification never applies to a loss ===")
c = cfg_fixture()
st = pe.new_state(c)
led2, skp2 = [], []
d = ramp("2026-09-01", [100, 90, 80, 70])
pe.run_cycle(st, c, {"BBB": d}, NOW, led2, skp2)
d = d + [["2026-09-05", 60.0]]
pe.run_cycle(st, c, {"BBB": d}, NOW, led2, skp2)          # fills the BUY at 60
# force an exit at a loss by spiking RSI then collapsing price
d = d + [["2026-09-06", 90.0]]
pe.run_cycle(st, c, {"BBB": d}, NOW, led2, skp2)          # queues SELL
d = d + [["2026-09-07", 10.0]]
pe.run_cycle(st, c, {"BBB": d}, NOW, led2, skp2)          # fills SELL at 10 -> loss
loss = [r for r in led2 if r["action"] == "SELL"][0]
ok(float(loss["net_pl_sar"]) < 0, "the trade closed at a loss")
eq(float(loss["purification_sar"]), 0.0, "no purification is taken on a loss")
eq(st["purified_sar"], 0.0, "purified total stays at zero")

print("\n=== Capital allocation: 10 slots, lowest RSI first, ties alphabetical ===")
c = cfg_fixture()
st = pe.new_state(c)
led3, skp3 = [], []
bars = {}
# 14 names all signalling; deeper collapse -> lower RSI
for i in range(14):
    name = "T%02d" % i
    last = 50 - i                                  # T13 falls furthest => lowest RSI
    bars[name] = ramp("2026-09-14", [100, 80, 60, last])
pe.run_cycle(st, c, bars, NOW, led3, skp3)
queued = [o["ticker"] for o in st["pending_orders"] if o["action"] == "BUY"]
eq(len(queued), 10, "exactly 10 entries are queued for 10 slots")
eq(len(skp3), 4, "the other 4 valid signals are recorded as skipped")
ok(all("no free slot" in s["reason"] for s in skp3),
   "each skip records why it was skipped")
rsis = {o["ticker"]: o["rsi"] for o in st["pending_orders"]}
ok(max(rsis.values()) <= min(float(s["rsi"]) for s in skp3) + 1e-9,
   "every taken signal has an RSI at or below every skipped one")

# tie-break
c = cfg_fixture(capital_model={"max_concurrent_positions": 1,
                               "target_position_pct": 0.10,
                               "leverage": False, "margin": False,
                               "shorting": False, "fractional_shares": True})
st = pe.new_state(c)
led4, skp4 = [], []
tie = ramp("2026-09-14", [100, 80, 60, 40])
pe.run_cycle(st, c, {"ZZZ": list(tie), "AAA": list(tie), "MMM": list(tie)}, NOW, led4, skp4)
taken = [o["ticker"] for o in st["pending_orders"] if o["action"] == "BUY"]
eq(taken, ["AAA"], "identical RSI breaks alphabetically")
eq(sorted(s["ticker"] for s in skp4), ["MMM", "ZZZ"], "the rest are skipped")

print("\n=== No leverage / no shorting / cash never negative ===")
c = cfg_fixture()
st = pe.new_state(c)
led5, skp5 = [], []
bars = {("N%02d" % i): ramp("2026-09-14", [100, 80, 60, 50 - i]) for i in range(10)}
pe.run_cycle(st, c, bars, NOW, led5, skp5)
bars2 = {k: v + [["2026-09-18", 40.0]] for k, v in bars.items()}
pe.run_cycle(st, c, bars2, NOW, led5, skp5)
ok(st["cash_sar"] >= -1e-9, "cash never goes negative")
eq(len(st["positions"]), 10, "10 positions opened")
ok(all(p["shares"] > 0 for p in st["positions"]), "no short (negative) position exists")
total = st["cash_sar"] + sum(p["basis_sar"] for p in st["positions"])
ok(total <= 100.0 + 1e-6, "total committed never exceeds the 100 SAR account")

print("\n=== Same-bar rerun and duplicate prevention ===")
c = cfg_fixture()
st = pe.new_state(c)
led6, skp6 = [], []
d = ramp("2026-09-14", [100, 90, 80, 70])
r = pe.run_cycle(st, c, {"CCC": d}, NOW, led6, skp6)
eq(r["status"], "decided", "the first run decides the bar")
n_orders = len(st["pending_orders"])
for _ in range(50):
    r = pe.run_cycle(st, c, {"CCC": d}, NOW, led6, skp6)
eq(r["status"], "already_decided", "every rerun on the same bar is a no-op")
eq(len(st["pending_orders"]), n_orders, "reruns queue nothing extra")
eq(len(led6), 0, "reruns write no ledger row")

print("\n=== Weekends and holidays ===")
# No new session means no new bar, so the bar id stays stale and the cycle is spent.
c = cfg_fixture()
st = pe.new_state(c)
led7, skp7 = [], []
fri = ramp("2026-09-14", [100, 90, 80, 70])       # last bar Fri 2026-09-17
pe.run_cycle(st, c, {"DDD": fri}, NOW, led7, skp7)
sat = pe.run_cycle(st, c, {"DDD": fri}, NOW, led7, skp7)
sun = pe.run_cycle(st, c, {"DDD": fri}, NOW, led7, skp7)
eq(sat["status"], "already_decided", "Saturday adds nothing")
eq(sun["status"], "already_decided", "Sunday adds nothing")
hol = pe.run_cycle(st, c, {"DDD": fri}, NOW, led7, skp7)
eq(hol["status"], "already_decided", "a market holiday adds nothing")
nxt = pe.run_cycle(st, c, {"DDD": fri + [["2026-09-21", 65.0]]}, NOW, led7, skp7)
eq(nxt["status"], "decided", "the next real session decides normally")

print("\n=== Failed commit / crash before persistence ===")
# The runner persists state, ledger and skipped rows together. If that write
# never happens, the in-memory result is discarded and the next run re-derives
# the identical decision exactly once.
c = cfg_fixture()
saved = pe.new_state(c)
d = ramp("2026-09-14", [100, 90, 80, 70])
attempt = json.loads(json.dumps(saved))           # what a crashed run would have had
led8, skp8 = [], []
pe.run_cycle(attempt, c, {"EEE": d}, NOW, led8, skp8)
ok(len(led8) == 0 and len(attempt["pending_orders"]) == 1,
   "the lost run had queued an order and written no ledger row")
led9, skp9 = [], []
redo = json.loads(json.dumps(saved))              # state as it survived on disk
pe.run_cycle(redo, c, {"EEE": d}, NOW, led9, skp9)
eq(len(redo["pending_orders"]), 1, "the retry re-derives exactly one order")
eq(redo["pending_orders"][0]["id"], attempt["pending_orders"][0]["id"],
   "the order id is deterministic, so it cannot become a second order")

print("\n=== Order ids are deterministic ===")
eq(pe._order_id("BUY", "AAPL", "2026-09-21"), "BUY:AAPL:2026-09-21", "id format is stable")
ok(pe._order_id("BUY", "AAPL", "2026-09-21") != pe._order_id("SELL", "AAPL", "2026-09-21"),
   "buy and sell on the same bar are distinct ids")

print("\n=== ISO weeks and rollups ===")
y, w, mon = iso_week(datetime(2026, 9, 22))
eq((y, w), (2026, 39), "2026-09-22 falls in ISO week 2026-W39")
eq(mon.isoformat(), "2026-09-21", "the ISO week starts on Monday")
y2, w2, _ = iso_week(datetime(2027, 1, 1))        # Friday -> belongs to 2026-W53
eq(y2, 2026, "1 Jan 2027 belongs to ISO year 2026")
ledger = [
    {"action": "BUY", "timestamp_utc": "2026-09-21T22:00:00Z", "net_pl_sar": "",
     "purification_sar": ""},
    {"action": "SELL", "timestamp_utc": "2026-09-22T22:00:00Z", "net_pl_sar": "2.0",
     "purification_sar": "0.2"},
    {"action": "SELL", "timestamp_utc": "2026-09-24T22:00:00Z", "net_pl_sar": "-1.0",
     "purification_sar": "0"},
    {"action": "SELL", "timestamp_utc": "2026-10-01T22:00:00Z", "net_pl_sar": "3.0",
     "purification_sar": "0.3"},
]
skipped = [{"timestamp_utc": "2026-09-22T22:00:00Z"}]
wk = rollup(ledger, skipped, "week")
mo = rollup(ledger, skipped, "month")
eq(len(wk), 2, "two ISO weeks appear")
eq(wk[0]["closed"], 2, "BUY rows are excluded from closed-trade counts")
eq(wk[0]["wins"], 1, "one win in the first week")
eq(wk[0]["losses"], 1, "one loss in the first week")
eq(wk[0]["net_pl_sar"], 1.0, "weekly net P/L sums correctly")
eq(wk[0]["purification_sar"], 0.2, "weekly purification sums correctly")
eq(wk[0]["skipped"], 1, "skipped signals are bucketed by their own timestamp")
eq(wk[-1]["cumulative_sar"], 4.0, "cumulative runs across periods")
eq(len(mo), 2, "two calendar months appear")
eq(mo[0]["net_pl_sar"], 1.0, "September nets +1.0")
eq(mo[1]["net_pl_sar"], 3.0, "October nets +3.0")
eq(rollup([], [], "week"), [], "an empty ledger yields no rows, not invented ones")

print("\n=== No real-money or notification surface ===")
src = ""
for fn in ("paper_engine.py", "run_paper.py", "rollups.py"):
    src += open(os.path.join(os.path.dirname(os.path.abspath(__file__)), fn),
                encoding="utf-8").read()
# Assembled from fragments on purpose: spelling these out literally would trip
# the repository's own isolation guard when it scans this file, and the fix for
# that must never be "stop scanning this file".
banned = ["nt" + "fy", "alp" + "aca", "ib" + "kr", "place_" + "order",
          "submit_" + "order", "api_" + "key", "broker_" + "api",
          "web" + "hook", "sm" + "tp", "twi" + "lio"]
hits = [b for b in banned if b in src.lower().replace("no alert path", "")]
ok(not hits, "no broker, order or outbound-notification surface exists %s" % (hits or ""))

print("\n" + "-" * 46)
print("  passed: %d   failed: %d" % (PASS, FAIL))
if FAIL:
    for f in FAILURES:
        print("    - %s" % f)
    sys.exit(1)
print("  all green")
sys.exit(0)
