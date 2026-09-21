"""Fetch daily bars for the candidate pool and cache them locally.

Split/dividend-adjusted closes are used, because a 16-year study spans many splits and
unadjusted closes would inject fake gaps straight into RSI. This differs from the live
tracker, which uses raw closes over a 1-year window; the divergence is noted in the
dashboard.

Writes:
  data_cache/<TICKER>.json   (gitignored - too large to commit)
  universe/data_manifest.json (committed - provenance and checksums)
"""
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, "data_cache")
START = int(datetime(2009, 6, 1, tzinfo=timezone.utc).timestamp())  # warm-up before 2010
END = int(datetime.now(timezone.utc).timestamp())

UA = "Mozilla/5.0 (research; daily bars; low volume)"
CTX = ssl.create_default_context()


def candidates():
    out = []
    with open(os.path.join(ROOT, "universe", "candidates.txt")) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.extend(line.split())
    # de-duplicate, preserve order
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def fetch(ticker, attempts=3):
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?interval=1d&period1={START}&period2={END}&events=div%2Csplit"
    )
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                payload = json.load(r)
            res = payload["chart"]["result"][0]
            ts = res["timestamp"]
            quote = res["indicators"]["quote"][0]
            # Prefer adjusted closes; fall back to raw if Yahoo omits them.
            adj = None
            if "adjclose" in res["indicators"]:
                adj = res["indicators"]["adjclose"][0]["adjclose"]
            closes = adj if adj else quote["close"]
            vols = quote.get("volume") or [None] * len(ts)

            rows = []
            for j in range(len(ts)):
                c = closes[j] if j < len(closes) else None
                v = vols[j] if j < len(vols) else None
                if c is None:
                    continue
                d = datetime.fromtimestamp(ts[j], tz=timezone.utc).strftime("%Y-%m-%d")
                rows.append([d, round(float(c), 6), int(v or 0)])
            return rows
        except Exception as e:  # noqa: BLE001 - transient network/shape issues
            last = e
            time.sleep(1.5 * (i + 1))
    print(f"    ! {ticker}: {type(last).__name__}: {last}")
    return None


def main():
    os.makedirs(CACHE, exist_ok=True)
    tickers = candidates()
    print(f"candidate pool: {len(tickers)} tickers")
    manifest, failed = {}, []

    for n, t in enumerate(tickers, 1):
        path = os.path.join(CACHE, f"{t}.json")
        if os.path.exists(path):
            rows = json.load(open(path))
        else:
            rows = fetch(t)
            if not rows:
                failed.append(t)
                continue
            with open(path, "w") as fh:
                json.dump(rows, fh)
            time.sleep(0.25)  # be polite to a free endpoint
        blob = json.dumps(rows, sort_keys=True).encode()
        manifest[t] = {
            "bars": len(rows),
            "first": rows[0][0],
            "last": rows[-1][0],
            "sha256": hashlib.sha256(blob).hexdigest()[:16],
        }
        if n % 25 == 0:
            print(f"  {n}/{len(tickers)} ...")

    out = {
        "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "Yahoo Finance chart API, interval=1d, split/dividend-adjusted close",
        "candidates": len(tickers),
        "retrieved": len(manifest),
        "failed": failed,
        "tickers": manifest,
    }
    with open(os.path.join(ROOT, "universe", "data_manifest.json"), "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
    print(f"retrieved {len(manifest)}/{len(tickers)}; failed: {failed}")


if __name__ == "__main__":
    sys.exit(main())
