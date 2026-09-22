"""Research-only paper-trading runner.

  python paper/run_paper.py --init       create config + state (no trading)
  python paper/run_paper.py --snapshot   refresh the dashboard only (no trading)
  python paper/run_paper.py --trade      one decision cycle on the completed bar
  python paper/run_paper.py --audit      print weekly/monthly rollups

There is no broker, no order API and no notification path anywhere in this file.
It reads public price history and writes local files. That is all it can do.
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_engine as pe          # noqa: E402
from rollups import rollup         # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CFG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
LEDGER_PATH = os.path.join(HERE, "ledger.csv")
SKIPPED_PATH = os.path.join(HERE, "skipped.csv")
SNAPSHOT_PATH = os.path.join(ROOT, "docs", "paper.json")

UA = "Mozilla/5.0 (research paper-trading; daily bars)"
CTX = ssl.create_default_context()

DEFAULT_CONFIG = {
    "_status": "RESEARCH ONLY. This strategy failed its holdout validation "
               "(see PREREGISTRATION.md and the NOT GENERALIZED verdict). "
               "The 100 SAR below is hypothetical and is not real, allocated, "
               "invested, validated or investable capital.",
    "account": {
        "initial_capital": 100,
        "currency": "SAR",
        "fx": {
            "sar_per_usd": 3.75,
            "locked_utc": None,
            "note": "Locked at initialization and never re-read from a live feed. "
                    "A paper result can therefore never move because the exchange "
                    "rate moved. Changing this value invalidates the experiment."
        }
    },
    "capital_model": {
        "max_concurrent_positions": 10,
        "target_position_pct": 0.10,
        "leverage": False,
        "margin": False,
        "shorting": False,
        "fractional_shares": True,
        "fractional_note": "Allowed solely because 100 SAR (~27 USD) cannot buy one "
                           "share of most US names. Not a claim that fractional "
                           "execution is available anywhere."
    },
    "costs": {
        "round_trip_pct": 0.0005,
        "per_side_pct": 0.00025,
        "note": "0.05% round trip, charged as 0.025% on entry and 0.025% on exit. "
                "This is the 'base' scenario pre-registered for the study."
    },
    "purification": {
        "rate": 0.10,
        "basis": "realized net paper profit only; never on a loss, never on "
                 "unrealized marks"
    },
    "strategy": {
        "frozen": True,
        "indicator": "Wilder RSI(2) on daily closes",
        "entry_below": 30,
        "exit_above": 70,
        "execution": "signal on completed bar D, fill at the next eligible "
                     "completed bar's close",
        "note": "Identical to the pre-registered rule. Not tuned, not re-fitted, "
                "and not to be varied."
    },
    "schedule": {
        "proposed_cron_utc": "0 22 * * 1-5",
        "note": "PROPOSED, NOT ENABLED. 22:00 UTC is after the US close in both "
                "EDT (18:00 ET) and EST (17:00 ET), and 30 minutes after the "
                "unrelated live tracker's own window, so the two never contend."
    },
    "safety": {
        "broker": None,
        "order_api": None,
        "notifications": "none - no alert path exists in this repository",
        "real_money": False
    }
}


def fetch_daily(ticker, days=120):
    end = int(datetime.now(timezone.utc).timestamp())
    start = end - days * 86400
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
           "?interval=1d&period1=%d&period2=%d" % (ticker, start, end))
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                res = json.load(r)["chart"]["result"][0]
            ts = res["timestamp"]
            q = res["indicators"]["quote"][0]
            adj = None
            if "adjclose" in res["indicators"]:
                adj = res["indicators"]["adjclose"][0]["adjclose"]
            closes = adj if adj else q["close"]
            out = []
            for i in range(len(ts)):
                c = closes[i] if i < len(closes) else None
                if c is None:
                    continue
                d = datetime.fromtimestamp(ts[i], tz=timezone.utc).strftime("%Y-%m-%d")
                out.append([d, float(c)])
            return out
        except Exception:                       # noqa: BLE001
            time.sleep(1.5 * (attempt + 1))
    return None


def load_universe():
    u = pe.load_json(os.path.join(ROOT, "universe", "universe.json"))
    return u["tickers"]


def gather(tickers, now):
    bars, failed = {}, []
    for i, t in enumerate(tickers, 1):
        b = fetch_daily(t)
        if not b:
            failed.append(t)
            continue
        bars[t] = pe.remove_partial_bar(b, now)
        time.sleep(0.12)
        if i % 30 == 0:
            print("  fetched %d/%d" % (i, len(tickers)))
    return bars, failed


def write_snapshot(state, cfg, bars, failed, trading_enabled, note=""):
    prices = {t: b[-1][1] for t, b in bars.items() if b}
    bar_ids = {pe.bar_id_of(b) for b in bars.values() if b}
    bar_ids.discard(None)
    bar_id = max(bar_ids) if bar_ids else None

    watch = []
    held = {p["ticker"]: p for p in state["positions"]}
    pending = {o["ticker"]: o for o in state["pending_orders"]}
    for t in sorted(bars):
        b = bars[t]
        if not b:
            continue
        r = pe.wilder_rsi_last([x[1] for x in b])
        px = b[-1][1]
        pos = held.get(t)
        row = {
            "ticker": t,
            "price_usd": round(px, 4),
            "price_sar": round(pe.to_sar(px, cfg), 4),
            "rsi": round(r, 2) if r is not None else None,
            "status": "HOLDING" if pos else "FLAT",
            "pending": pending[t]["action"] if t in pending else None,
            "signal": ("BUY" if (r is not None and r < pe.ENTRY_BELOW and not pos)
                       else "SELL" if (r is not None and r > pe.EXIT_ABOVE and pos)
                       else None),
        }
        if pos:
            mv = pos["shares"] * pe.to_sar(px, cfg)
            row["unrealized_pl_sar"] = round(mv - pos["basis_sar"], 4)
            row["shares"] = round(pos["shares"], 6)
            row["entry_bar"] = pos["entry_bar"]
        watch.append(row)

    eq = pe.equity_sar(state, prices, cfg)
    unreal = sum(w.get("unrealized_pl_sar", 0.0) for w in watch)

    ledger = pe.read_csv(LEDGER_PATH)
    skipped = pe.read_csv(SKIPPED_PATH)

    snap = {
        "generated_utc": pe.utcnow(),
        "status_banner": "Research-only paper-trading experiment. This breadth "
                         "strategy failed its holdout validation and is not "
                         "approved for real trading.",
        "trading_enabled": trading_enabled,
        "note": note,
        "currency": cfg["account"]["currency"],
        "fx_locked": cfg["account"]["fx"],
        "costs": cfg["costs"],
        "purification_rate": cfg["purification"]["rate"],
        "capital_model": cfg["capital_model"],
        "schedule": cfg["schedule"],
        "last_decided_bar": state.get("last_decided_bar"),
        "last_run_utc": state.get("last_run_utc"),
        "account": {
            "initial_capital_sar": cfg["account"]["initial_capital"],
            "cash_sar": round(state["cash_sar"], 4),
            "equity_sar": round(eq, 4),
            "realized_pl_sar": round(state["realized_pl_sar"], 4),
            "unrealized_pl_sar": round(unreal, 4),
            "purified_sar": round(state["purified_sar"], 4),
            "open_positions": len(state["positions"]),
            "max_positions": cfg["capital_model"]["max_concurrent_positions"],
            "closed_trades": state["closed_trades"],
        },
        "positions": state["positions"],
        "pending_orders": state["pending_orders"],
        "watchlist": watch,
        "trades": ledger[-60:],
        "skipped_recent": skipped[-40:],
        "skipped_total": len(skipped),
        "weekly": rollup(ledger, skipped, "week"),
        "monthly": rollup(ledger, skipped, "month"),
        "universe_size": len(bars),
        "fetch_failures": failed,
        "bar_id": bar_id,
    }
    pe.save_json(SNAPSHOT_PATH, snap)
    return snap


def cmd_init():
    if os.path.exists(CFG_PATH):
        print("config already exists; refusing to re-initialize (FX rate is locked).")
        return 1
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg["account"]["fx"]["locked_utc"] = pe.utcnow()
    pe.save_json(CFG_PATH, cfg)
    pe.save_json(STATE_PATH, pe.new_state(cfg))
    print("initialized: %s %s, FX locked at %s SAR/USD on %s" % (
        cfg["account"]["initial_capital"], cfg["account"]["currency"],
        cfg["account"]["fx"]["sar_per_usd"], cfg["account"]["fx"]["locked_utc"]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--trade", action="store_true")
    ap.add_argument("--audit", action="store_true")
    a = ap.parse_args()

    if a.init:
        return cmd_init()

    cfg = pe.load_json(CFG_PATH)
    if cfg is None:
        print("no config; run --init first")
        return 1
    state = pe.load_json(STATE_PATH) or pe.new_state(cfg)

    if a.audit:
        led, skp = pe.read_csv(LEDGER_PATH), pe.read_csv(SKIPPED_PATH)
        for period in ("week", "month"):
            rows = rollup(led, skp, period)
            print("\n=== %sly (SAR) ===" % period.capitalize())
            if not rows:
                print("  no trades yet")
                continue
            print("%-28s %7s %9s %14s %12s %9s %14s" % (
                "Period", "Closed", "W / L", "Net P/L", "Purified", "Skipped", "Cumulative"))
            for b in rows:
                print("%-28s %7d %9s %14.4f %12.4f %9d %14.4f" % (
                    b["label"], b["closed"], "%d / %d" % (b["wins"], b["losses"]),
                    b["net_pl_sar"], b["purification_sar"], b["skipped"],
                    b["cumulative_sar"]))
        return 0

    now = datetime.now(timezone.utc)
    tickers = load_universe()
    print("fetching %d tickers..." % len(tickers))
    bars, failed = gather(tickers, now)
    print("got %d, failed %d" % (len(bars), len(failed)))

    if a.trade:
        led_rows, skp_rows = [], []
        res = pe.run_cycle(state, cfg, bars, now, led_rows, skp_rows)
        print("cycle: %s" % res)
        pe.append_csv(LEDGER_PATH, pe.LEDGER_FIELDS, led_rows)
        pe.append_csv(SKIPPED_PATH, pe.SKIPPED_FIELDS, skp_rows)
        pe.save_json(STATE_PATH, state)
        write_snapshot(state, cfg, bars, failed, True)
    else:
        write_snapshot(state, cfg, bars, failed, False,
                       note="Snapshot only - no decision cycle was run and no state "
                            "was changed.")
    print("snapshot written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
