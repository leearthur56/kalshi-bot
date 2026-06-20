#!/usr/bin/env python3
"""
Forward resolution logger -- builds an UNBIASED dataset over time.

The backtest (kalshi_backtest.py) can only use prices that already exist, and
the cleanest historical price (last_price) is biased toward the outcome. This
logger fixes that: it records the price NOW, before anyone knows the result,
then later records how each market settled. Joining the two gives you a clean
"priced at X -> resolved YES/NO" dataset to measure the real favorite edge.

Run it on a schedule (Windows Task Scheduler / cron):
  snapshot : every hour or two -- record current price of skewed open markets
  settle   : daily -- look up which logged markets have resolved
  report   : anytime -- realized win rate vs implied, by entry-price bucket

Usage
-----
  python kalshi_logger.py snapshot --threshold 0.90 --min-volume 100
  python kalshi_logger.py settle
  python kalshi_logger.py report

Data is appended as JSONL under ./data/ (snapshots.jsonl, resolutions.jsonl).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import kalshi_common as kc

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SNAP_PATH = os.path.join(DATA_DIR, "snapshots.jsonl")
RES_PATH = os.path.join(DATA_DIR, "resolutions.jsonl")

BUCKETS = [(0.50, 0.80), (0.80, 0.90), (0.90, 0.95), (0.95, 0.97),
           (0.97, 0.98), (0.98, 0.99), (0.99, 1.0001)]


def _append_jsonl(path: str, rows: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# --------------------------------------------------------------------------- #
def cmd_snapshot(args) -> int:
    now = int(time.time())
    rows = []
    scanned = 0
    for m in kc.paginate_markets("open", max_markets=args.max):
        scanned += 1
        yp = kc.yes_price(m)
        if yp <= 0:
            continue
        if kc.fnum(m, "volume_fp") < args.min_volume:
            continue
        fav_price = yp if yp >= 0.5 else 1 - yp
        if fav_price < args.threshold:
            continue
        rows.append({
            "ts": now,
            "ticker": m.get("ticker"),
            "yes_price": round(yp, 4),
            "fav_side": "yes" if yp >= 0.5 else "no",
            "fav_price": round(fav_price, 4),
            "volume": kc.fnum(m, "volume_fp"),
            "close_time": m.get("close_time"),
        })
    _append_jsonl(SNAP_PATH, rows)
    print(f"Snapshot: scanned {scanned} open markets, logged {len(rows)} "
          f"favorites >= {args.threshold:.2f} to {SNAP_PATH}")
    return 0


def cmd_settle(args) -> int:
    snaps = _read_jsonl(SNAP_PATH)
    if not snaps:
        print("No snapshots yet. Run 'snapshot' first.")
        return 0
    already = {r["ticker"] for r in _read_jsonl(RES_PATH)}
    want = {s["ticker"] for s in snaps if s["ticker"] not in already}
    if not want:
        print("All snapshotted markets already resolved/logged. Nothing to do.")
        return 0

    # Pull settled markets and match against what we're waiting on.
    found = []
    for m in kc.paginate_markets("settled", max_markets=args.max):
        t = m.get("ticker")
        if t in want and m.get("result") in ("yes", "no"):
            found.append({
                "ticker": t,
                "result": m.get("result"),
                "settle_price": kc.fnum(m, "last_price_dollars"),
                "settled_ts": int(time.time()),
            })
            want.discard(t)
            if not want:
                break
    _append_jsonl(RES_PATH, found)
    print(f"Settle: resolved {len(found)} markets; "
          f"{len(want)} still open/unfound. Logged to {RES_PATH}")
    return 0


def cmd_report(args) -> int:
    snaps = _read_jsonl(SNAP_PATH)
    res = {r["ticker"]: r for r in _read_jsonl(RES_PATH)}
    if not snaps:
        print("No snapshots yet.")
        return 0

    # Use the EARLIEST snapshot per ticker as the entry (most unbiased).
    entry: dict[str, dict] = {}
    for s in snaps:
        t = s["ticker"]
        if t not in entry or s["ts"] < entry[t]["ts"]:
            entry[t] = s

    stats = {b: [0, 0, 0.0, 0.0, 0.0] for b in BUCKETS}
    matched = 0
    for t, s in entry.items():
        if t not in res:
            continue
        matched += 1
        fav_price = s["fav_price"]
        fav_won = res[t]["result"] == s["fav_side"]
        bucket = next((b for b in BUCKETS if b[0] <= fav_price < b[1]), None)
        if bucket is None:
            continue
        fee = kc.fee_per_contract(fav_price)
        gross = (1 - fav_price) if fav_won else (-fav_price)
        st = stats[bucket]
        st[0] += 1
        st[1] += 1 if fav_won else 0
        st[2] += gross
        st[3] += gross - fee
        st[4] += fav_price

    print(f"\nLogged snapshots: {len(entry)} unique markets; "
          f"{matched} have resolved.\n")
    if matched == 0:
        print("No resolved markets yet -- run 'settle' after some markets close.")
        return 0

    header = (f"{'ENTRY PRICE':<13} {'N':>6} {'IMPLIED':>8} {'ACTUAL':>8} "
              f"{'EDGE':>7} {'NET EV':>8} {'TOTAL NET':>10}")
    print(header)
    print("-" * len(header))
    tot_n = tot_net = 0.0
    for b in BUCKETS:
        n, wins, gross, net, sump = stats[b]
        if n == 0:
            continue
        implied, actual = sump / n, wins / n
        print(f"{int(b[0]*100):>2d}-{int(b[1]*100):>3d}c     {n:>6d} "
              f"{implied*100:>7.2f}% {actual*100:>7.2f}% "
              f"{(actual-implied)*100:>+6.2f}% {net/n*100:>+7.2f}c {net*100:>+9.2f}c")
        tot_n += n
        tot_net += net
    print("-" * len(header))
    if tot_n:
        print(f"\nNet P&L across resolved logged favorites: ${tot_net:+.2f} "
              f"over {int(tot_n)} contracts.")
    print("\nThis dataset is unbiased: prices were recorded before the outcome\n"
          "was known. The more snapshots you accumulate, the more trustworthy it is.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snapshot", help="Record current price of skewed markets.")
    p_snap.add_argument("--threshold", type=float, default=0.90)
    p_snap.add_argument("--min-volume", type=float, default=100.0)
    p_snap.add_argument("--max", type=int, default=None)
    p_snap.set_defaults(func=cmd_snapshot)

    p_set = sub.add_parser("settle", help="Look up resolutions of logged markets.")
    p_set.add_argument("--max", type=int, default=20000)
    p_set.set_defaults(func=cmd_settle)

    p_rep = sub.add_parser("report", help="Realized win rate vs implied, by bucket.")
    p_rep.set_defaults(func=cmd_report)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
