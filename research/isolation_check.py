"""Isolation guard. Fails if this project can reach the live tracker in any way.

Run locally with `python research/isolation_check.py`; CI runs the same file, so the
check cannot drift between the two.

The forbidden ntfy topics are matched by PATTERN, never stored literally - writing a
live topic into this repo to test for it would itself be the leak. The old US topic was
public; the new one is a GitHub Actions secret belonging to the other repository and
must never appear here.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SKIP_DIRS = {".git", "data_cache", "__pycache__", "node_modules"}
SELF = os.path.basename(__file__)

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
    ("cross-repo write via the other repo's data paths",
     re.compile(r"data-sa/|scripts-sa/", re.I)),
]

# This project must stay read-only toward the outside world.
EXECUTION_RULES = [
    ("paper-trading ledger", re.compile(r"paper_trades\.csv", re.I)),
    ("virtual balance / portfolio state",
     re.compile(r"balance_usd|portfolio_history\.csv|purification", re.I)),
    ("alert dispatch", re.compile(r"Add-OutboxEvent|Send-OutboxMessage", re.I)),
]


def files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn == SELF:
                continue  # the patterns themselves live here
            p = os.path.join(dirpath, fn)
            if os.path.getsize(p) > 2_000_000:
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

    print(f"isolation check: scanned {scanned} files under {ROOT}")
    for label, _ in RULES + EXECUTION_RULES:
        print(f"  rule: {label}")

    if violations:
        print(f"\nFAILED - {len(violations)} violation(s):")
        for rel, line, label in violations:
            print(f"  {rel}:{line}  -> {label}")
        print("\nThis project must not reference the live tracker, its notification "
              "topics, or any trade-execution surface.")
        return 1

    print("\nPASS - no reference to the live tracker, its topics, or any "
          "execution/ledger surface.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
