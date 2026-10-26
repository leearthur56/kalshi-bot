#!/usr/bin/env python3
"""
Favorite-longshot bias test on Kalshi settled markets. Read-only.

Claim being tested (research brief, citing a secondary summary of a 313,972-
contract Kalshi study): cheap contracts (<10c) are overpriced and lose most of
their stake; 88-95c contracts are underpriced by ~4c. If true, systematically
buying favorites in that band is +EV.

Method (no look-ahead):
  * for each settled market, take its quote at fixed points in its life
    (--fracs, e.g. 25% / 50% / 90% of the way from open to close);
  * at each point record BOTH trades you could make: buy YES at the yes ask,
    buy NO at the no ask (1 - yes bid). One is the cheap side, one the dear side;
  * bucket every trade by the price paid, and compare the price with how often
    it actually won.

edge  = win rate - average price paid          (cents per $1 contract)
net   = edge minus fees: taker (0.07 p(1-p)) and maker (0.0175 p(1-p))
The maker column assumes you got filled as a resting order at that price,
which ignores who chooses to trade with you -- treat it as an upper bound.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import math
import os
import sys
from collections import defaultdict

import kalshi_common as kc

TAKER, MAKER = 0.07, 0.0175

CATEGORIES = {
    "weather": ["KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHDEN",
                "KXLOWTNYC", "KXLOWTCHI", "KXLOWTMIA"],
    "sports": ["KXNBAGAME", "KXNFLGAME", "KXMLBGAME", "KXNHLGAME", "KXEPLGAME",
               "KXNCAAFGAME"],
    "crypto": ["KXBTC15M"],
}
ONE_PER_EVENT = {"sports"}          # mirror-image team markets: keep one per game

BUCKETS = [(0.01, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.50),
           (0.50, 0.65), (0.65, 0.80), (0.80, 0.88), (0.88, 0.95), (0.95, 0.995)]


def _ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def life_quotes(m: dict, cache_dir: str):
    """Sorted [(ts, yes_bid, yes_ask)] over the market's life. 1-min candles for
    markets that live <= 6h, hourly otherwise. Cached."""
    o, c = _ts(m["open_time"]), _ts(m["close_time"])
    period = 1 if c - o <= 6 * 3600 else 60
    path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, f"{m['ticker']}_{period}.json.gz")
        if os.path.exists(path):
            with gzip.open(path, "rt") as f:
                return [tuple(r) for r in json.load(f)]
    try:
        data = kc.get(f"/series/{kc.series_of(m['ticker'])}/markets/{m['ticker']}/candlesticks",
                      {"start_ts": o - 60, "end_ts": c + 60, "period_interval": period})
    except RuntimeError:
        return None
    rows = []
    for cd in data.get("candlesticks", []):
        try:
            rows.append((int(cd["end_period_ts"]), float(cd["yes_bid"]["close_dollars"]),
                         float(cd["yes_ask"]["close_dollars"])))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort()
    if path:
        with gzip.open(path, "wt") as f:
            json.dump(rows, f)
    return rows


def quote_at(rows, t):
    best = None
    for ts, yb, ya in rows:
        if ts > t:
            break
        if 0 < yb <= ya <= 1:
            best = (yb, ya)
    return best


def bucket_of(p):
    for lo, hi in BUCKETS:
        if lo <= p < hi:
            return (lo, hi)
    return None


def run(args) -> int:
    fracs = [float(x) for x in args.fracs.split(",")]
    cats = args.categories.split(",")
    # trades[frac][bucket] -> list of (price, won); also per category for the 88-95 band
    trades = {f: defaultdict(list) for f in fracs}
    band = {f: defaultdict(list) for f in fracs}
    n_markets = 0

    for cat in cats:
        for s in CATEGORIES[cat]:
            seen_ev, used = set(), 0
            for m in kc.paginate_markets("settled", series_ticker=s, max_markets=args.max * 3):
                if used >= args.max:
                    break
                if m.get("result") not in ("yes", "no"):
                    continue
                if cat in ONE_PER_EVENT:
                    ev = m.get("event_ticker")
                    if ev in seen_ev:
                        continue
                    seen_ev.add(ev)
                rows = life_quotes(m, args.cache_dir)
                if not rows:
                    continue
                used += 1
                n_markets += 1
                o, c = _ts(m["open_time"]), _ts(m["close_time"])
                yes_won = m["result"] == "yes"
                for f in fracs:
                    q = quote_at(rows, o + int(f * (c - o)))
                    if q is None:
                        continue
                    yb, ya = q
                    for price, won in ((ya, yes_won), (round(1 - yb, 4), not yes_won)):
                        b = bucket_of(price)
                        if b is None:
                            continue
                        trades[f][b].append((price, won))
                        if b == (0.88, 0.95):
                            band[f][cat].append((price, won))
            print(f"  {s:13} {used} markets", file=sys.stderr)

    print(f"\n=== Favorite-longshot test: {n_markets} settled markets "
          f"({', '.join(cats)}) ===")
    for f in fracs:
        print(f"\n--- priced at {f*100:.0f}% of each market's life ---")
        print(f"{'price paid':<12}{'trades':>8}{'avg price':>11}{'win rate':>10}"
              f"{'edge':>8}{'after taker':>13}{'after maker':>13}{'+/- 2se':>9}")
        print("-" * 84)
        for b in BUCKETS:
            t = trades[f][b]
            if not t:
                continue
            n = len(t)
            avg_p = sum(p for p, _ in t) / n
            win = sum(1 for _, w in t if w) / n
            edge = win - avg_p
            tk = edge - sum(TAKER * p * (1 - p) for p, _ in t) / n
            mk = edge - sum(MAKER * p * (1 - p) for p, _ in t) / n
            # noise you'd expect if contracts were fairly priced (win rate = price)
            se = 2 * math.sqrt(avg_p * (1 - avg_p) / n)
            print(f"{b[0]*100:>3.0f}-{b[1]*100:<3.0f}c    {n:>8}{avg_p*100:>10.1f}c{win*100:>9.1f}%"
                  f"{edge*100:>+7.1f}c{tk*100:>+12.1f}c{mk*100:>+12.1f}c{se*100:>8.1f}c")
        print("  88-95c band by category:", end="")
        for cat in cats:
            t = band[f][cat]
            if t:
                e = sum(1 for _, w in t if w) / len(t) - sum(p for p, _ in t) / len(t)
                print(f"  {cat} {e*100:+.1f}c (n={len(t)})", end="")
        print()
    print("\nedge = win rate - price paid. '+/- 2se' is the noise you'd expect on the win rate "
          "if the\nbucket were fairly priced: an edge smaller than that is indistinguishable from "
          "zero.\nMaker column ignores fill selection.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--categories", default="weather,sports,crypto",
                    help="Comma list from: weather, sports, crypto. Default all.")
    ap.add_argument("--max", type=int, default=120, help="Settled markets per series. Default 120.")
    ap.add_argument("--fracs", default="0.25,0.5,0.9",
                    help="Points in each market's life to price at. Default 0.25,0.5,0.9.")
    ap.add_argument("--cache-dir", default=os.path.join("data", "flb_cache"))
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
