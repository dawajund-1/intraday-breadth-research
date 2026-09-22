"""Isolation guard. Fails if this project can reach the live tracker, reach real
money, or drift off its declared currency model.

Run locally with `python research/isolation_check.py`; CI runs the same file, so
the check cannot drift between the two.

The forbidden ntfy topics are matched by PATTERN, never stored literally - writing
a live topic into this repo to test for it would itself be the leak.

WHY THESE RULES CHANGED (2026-09-22)
------------------------------------
The first version of this guard banned any mention of a ledger, realized P/L,
purification or position sizing, because at the time this repository was pure
analysis and none of those had any business existing here.

This project now runs a declared, research-only paper-trading simulation, so
those words are legitimate. Leaving the old rules in place would have meant
either a permanently red build or - far worse - quietly deleting the rules to
make it green, which is how a guard becomes decorative.

The boundary was therefore re-drawn rather than relaxed. What the guard protects
now is the set of properties that actually still have to hold:

  * no path to the live tracker, its Pages site, its topics or its data
  * no path to real money: no broker, no order submission, no API credential
  * no notification path of any kind
  * SAR stays canonical: no USD-denominated account state
  * FX stays locked: no live exchange-rate lookup

A paper ledger denominated in SAR is expected. A USD balance, a broker call or
an alert is not.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SKIP_DIRS = {".git", "data_cache", "__pycache__", "node_modules"}
SELF = os.path.basename(__file__)

# --- isolation from the live tracker -------------------------------------
RULES = [
    ("live tracker repository name",
     re.compile(r"intraday[-_]sar[-_]tracker", re.I)),
    ("live tracker Pages site",
     re.compile(r"dawajund-1\.github\.io/intraday-sar", re.I)),
    ("old public US ntfy topic",
     re.compile(r"intraday-sar-[0-9a-f]{10}", re.I)),
    ("Tadawul ntfy topic",
     re.compile(r"intraday-sa-tadawul-[0-9a-f]{10}", re.I)),
    ("rotated US ntfy topic",
     re.compile(r"us-rsi-[0-9a-f]{32,}", re.I)),
    ("any ntfy endpoint",
     re.compile(r"ntfy\.sh", re.I)),
    ("live tracker's notification secret",
     re.compile(r"NTFY_TOPIC_US", re.I)),
    ("live tracker's data or script paths",
     re.compile(r"data-sa/|scripts-sa/|paper_trades\.csv|portfolio_history\.csv", re.I)),
]

# --- this project must never reach real money or a phone -----------------
EXECUTION_RULES = [
    ("broker or order-submission surface",
     re.compile(r"\balpaca\b|\bibkr\b|interactive_brokers|tradier|td_ameritrade|"
                r"place_order|submit_order|create_order|broker_api", re.I)),
    ("outbound notification surface",
     re.compile(r"\bwebhook\b|\bsmtp\b|\btwilio\b|sendgrid|pushover|telegram_bot", re.I)),
    ("stored API credential",
     re.compile(r"api_key\s*=|secret_key\s*=|access_token\s*=", re.I)),
    ("USD-denominated account state (SAR is canonical here)",
     re.compile(r"balance_usd|cash_usd|equity_usd|realized_pl_usd", re.I)),
    ("live exchange-rate lookup (the FX rate is locked at initialization)",
     re.compile(r"exchangerate\.|openexchangerates|fixer\.io|currencyapi|"
                r"fetch_fx|live_fx_rate", re.I)),
]


def files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn == SELF:
                continue  # the patterns themselves live here
            p = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(p) > 2_000_000:
                    continue
            except OSError:
                continue
            yield p


def main():
    violations = []
    scanned = 0
    for path in files():
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        scanned += 1
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        for label, rx in RULES + EXECUTION_RULES:
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                violations.append((rel, line, label))

    print("isolation check: scanned %d files under %s" % (scanned, ROOT))
    for label, _ in RULES + EXECUTION_RULES:
        print("  rule: %s" % label)

    if violations:
        print("\nFAILED - %d violation(s):" % len(violations))
        for rel, line, label in violations:
            print("  %s:%d  -> %s" % (rel, line, label))
        print("\nThis project must not reference the live tracker, reach real money, "
              "send notifications, hold USD account state, or read a live FX rate.")
        return 1

    print("\nPASS - isolated from the live tracker; no real-money, notification, "
          "USD-account or live-FX surface.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
