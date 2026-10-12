"""Weekly and monthly rollups over the paper ledger.

Kept separate from the dashboard's own JavaScript implementation on purpose: two
independent implementations of the same documented rules are a cross-check. If
they ever disagree, one of them is wrong and the tests will say so.

Rules (identical on both sides):
  * Only SELL rows are realized. BUY rows carry no P/L.
  * A trade belongs to the period containing its SESSION BAR, not the moment the
    job happened to run. These differ routinely: GitHub delays the cron 3-4
    hours, so a Friday session is usually written on Saturday. Bucketing by run
    time would therefore push a month-end session into the following month.
    bar_id is the market session the decision was priced on, which is the thing
    a weekly or monthly total is actually about.
  * Weeks are ISO-8601: Monday-Sunday, ISO year taken from the Thursday.
  * Months are calendar months.
  * Realized P/L   = sum of net_pl_sar
  * Purification   = sum of purification_sar
  * win = net_pl_sar > 0, loss = net_pl_sar < 0 (exactly zero is neither)
  * Skipped signals are counted by their own timestamp, in the same buckets.
"""
from collections import OrderedDict
from datetime import datetime, timedelta


def _parse(ts):
    return datetime.strptime(ts.replace("Z", ""), "%Y-%m-%dT%H:%M:%S")


def _period_date(row):
    """The date a row belongs to: its session bar, falling back to run time.

    bar_id is a plain YYYY-MM-DD session date. Rows written before bar_id
    existed fall back to the run timestamp so old data still buckets somewhere
    rather than vanishing.
    """
    bar = (row.get("bar_id") or "").strip()
    if bar:
        return datetime.strptime(bar[:10], "%Y-%m-%d")
    return _parse(row["timestamp_utc"])


def iso_week(d):
    """(iso_year, iso_week, monday) for a date, per ISO-8601."""
    day = d.date() if isinstance(d, datetime) else d
    dow = day.weekday()                       # Mon = 0
    thursday = day + timedelta(days=3 - dow)
    iso_year = thursday.year
    jan4 = datetime(iso_year, 1, 4).date()
    week1_mon = jan4 - timedelta(days=jan4.weekday())
    week = ((thursday - week1_mon).days // 7) + 1
    return iso_year, week, day - timedelta(days=dow)


def _blank(key, label):
    return {"key": key, "label": label, "closed": 0, "wins": 0, "losses": 0,
            "net_pl_sar": 0.0, "purification_sar": 0.0, "skipped": 0}


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def rollup(ledger_rows, skipped_rows, period):
    """period is 'week' or 'month'. Returns rows oldest-first with a cumulative."""
    buckets = OrderedDict()

    def key_label(d):
        if period == "week":
            y, w, mon = iso_week(d)
            return ("%04d-W%02d" % (y, w),
                    "%04d-W%02d (%s - %s)" % (y, w, mon.strftime("%b %d"),
                                              (mon + timedelta(days=6)).strftime("%b %d")))
        return (d.strftime("%Y-%m"), d.strftime("%B %Y"))

    for r in ledger_rows:
        if r.get("action") != "SELL":
            continue
        k, lab = key_label(_period_date(r))
        b = buckets.setdefault(k, _blank(k, lab))
        b["closed"] += 1
        pl = _fnum(r.get("net_pl_sar"))
        pur = _fnum(r.get("purification_sar"))
        if pl is not None:
            b["net_pl_sar"] += pl
            if pl > 0:
                b["wins"] += 1
            elif pl < 0:
                b["losses"] += 1
        if pur is not None:
            b["purification_sar"] += pur

    for r in skipped_rows:
        if not (r.get("bar_id") or r.get("timestamp_utc")):
            continue
        k, lab = key_label(_period_date(r))
        buckets.setdefault(k, _blank(k, lab))["skipped"] += 1

    rows = sorted(buckets.values(), key=lambda b: b["key"])
    cum = 0.0
    for b in rows:
        cum += b["net_pl_sar"]
        b["cumulative_sar"] = round(cum, 6)
        b["net_pl_sar"] = round(b["net_pl_sar"], 6)
        b["purification_sar"] = round(b["purification_sar"], 6)
    return rows
