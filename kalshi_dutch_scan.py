#!/usr/bin/env python3
"""
Historical Dutch-book scanner for Kalshi multi-outcome events. Read-only.

An event like "NYC high temperature on Oct 26" is split into mutually exclusive
buckets -- exactly one resolves YES. So:

  LONG book  : buy YES on every bucket. Costs sum(yes_ask); pays exactly $1.
               Profitable if sum(yes_ask) + fees < $1.
  SHORT book : buy NO on every bucket (same as selling every YES). Costs
               sum(1 - yes_bid) = n - sum(yes_bid); pays exactly n - 1.
               Profitable if sum(yes_bid) - fees > $1.

For each settled event we pull 1-minute quotes for every leg, carry each leg's
last quote forward, and at every minute check both books. Both books cross the
spread (you pay asks / hit bids), so edge is reported gross and after TAKER fees
(coef 0.07, p(1-p) shape). Maker fees don't apply: a resting order fills at its
own price only when someone trades with it, which is market-making, not this.

Limitations (read before trusting a hit):
  * candle quotes are 1-minute snapshots, so sub-minute windows are invisible
    and a "hit" may have lasted far less than a minute;
  * candles carry no size -- the cheapest ask may have been 1 contract;
  * taking every leg at once is assumed; in reality legs fill one at a time.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import statistics
import sys
from collections import OrderedDict

import kalshi_common as kc
from kalshi_hold_audit import WEATHER as HIGH_TEMP

LOW_TEMP = ["KXLOWTATL", "KXLOWTAUS", "KXLOWTBOS", "KXLOWTCHI", "KXLOWTDAL", "KXLOWTDC",
            "KXLOWTDEN", "KXLOWTHOU", "KXLOWTLAX", "KXLOWTLV", "KXLOWTMIA", "KXLOWTMIN",
            "KXLOWTNOLA", "KXLOWTNYC", "KXLOWTOKC", "KXLOWTPHIL", "KXLOWTPHX", "KXLOWTSATX",
            "KXLOWTSEA", "KXLOWTSFO"]

TAKER = 0.07


def _ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def leg_quotes(ticker: str, start: int, end: int, cache_dir: str):
    """minute ts -> (yes_bid, yes_ask), cached on disk."""
    path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, f"{ticker}_{start}_{end}.json.gz")
        if os.path.exists(path):
            with gzip.open(path, "rt") as f:
                return {int(k): tuple(v) for k, v in json.load(f).items()}
    try:
        data = kc.get(f"/series/{kc.series_of(ticker)}/markets/{ticker}/candlesticks",
                      {"start_ts": start, "end_ts": end, "period_interval": 1})
    except RuntimeError:
        return None
    out = {}
    for c in data.get("candlesticks", []):
        try:
            yb = float(c["yes_bid"]["close_dollars"])
            ya = float(c["yes_ask"]["close_dollars"])
            out[int(c["end_period_ts"])] = (yb, ya)
        except (KeyError, TypeError, ValueError):
            continue
    if path:
        with gzip.open(path, "wt") as f:
            json.dump(out, f)
    return out


def fee(p: float, coef: float) -> float:
    return coef * p * (1 - p)


def scan_event(legs: list[dict], args):
    close_ts = min(_ts(m["close_time"]) for m in legs)
    start = close_ts - int(args.window_hours * 3600)
    end = close_ts - 60                      # ignore the settlement minute itself
    quotes = []
    for m in legs:
        q = leg_quotes(m["ticker"], start, end, args.cache_dir)
        if not q:
            return None
        quotes.append(q)

    stamps = sorted(set().union(*[q.keys() for q in quotes]))
    last = [None] * len(legs)
    minutes = []                             # per evaluated minute: dict of edges
    for t in stamps:
        for i, q in enumerate(quotes):
            if t in q:
                last[i] = q[t]
        if any(v is None for v in last):
            continue
        bids = [v[0] for v in last]
        asks = [v[1] for v in last]
        # an ask of 0 means no offer at all: that leg can't be bought
        long_ok = all(0 < a <= 1 for a in asks)
        cost = sum(asks)
        no_prices = [1 - b for b in bids]
        r = {"t": t, "cost": cost, "bids": sum(bids)}
        if long_ok:
            r["long_gross"] = 1 - cost
            r["long_taker"] = 1 - cost - sum(fee(a, TAKER) for a in asks)
        r["short_gross"] = sum(bids) - 1
        r["short_taker"] = sum(bids) - 1 - sum(fee(p, TAKER) for p in no_prices)
        minutes.append(r)
    return minutes


def run(args) -> int:
    families = {"high": HIGH_TEMP, "low": LOW_TEMP}
    series = [s for f in args.families.split(",") for s in families[f]]

    keys = ["long_gross", "long_taker", "short_gross", "short_taker"]
    hit_minutes = {k: 0 for k in keys}
    hit_events = {k: 0 for k in keys}
    episodes = {k: [] for k in keys}         # length (minutes) of each consecutive run of hits
    best = []                                # (edge, key, event, t, cost, bids)
    total_minutes = events_done = 0
    cost_samples, bid_samples = [], []

    for s in series:
        events: "OrderedDict[str, list]" = OrderedDict()
        for m in kc.paginate_markets("settled", series_ticker=s,
                                     max_markets=args.events_per_series * 8 + 20):
            events.setdefault(m.get("event_ticker"), []).append(m)
        done_here = 0
        for et, legs in events.items():
            if done_here >= args.events_per_series:
                break
            # a clean partition: at least 2 legs and exactly one resolved YES
            if len(legs) < 2 or sum(1 for m in legs if m.get("result") == "yes") != 1:
                continue
            mins = scan_event(legs, args)
            if not mins:
                continue
            done_here += 1
            events_done += 1
            total_minutes += len(mins)
            cost_samples += [r["cost"] for r in mins]
            bid_samples += [r["bids"] for r in mins]
            for k in keys:
                hits = [r for r in mins if r.get(k, -1) > 0]
                hit_minutes[k] += len(hits)
                hit_events[k] += 1 if hits else 0
                for r in hits:
                    best.append((r[k], k, et, r["t"], r["cost"], r["bids"]))
                run_len = 0
                for r in mins:                   # consecutive evaluated minutes = one episode
                    if r.get(k, -1) > 0:
                        run_len += 1
                    elif run_len:
                        episodes[k].append(run_len)
                        run_len = 0
                if run_len:
                    episodes[k].append(run_len)
            print(f"  {et:28} {len(legs)} legs, {len(mins):4} min  "
                  f"min cost ${min(r['cost'] for r in mins):.3f}  "
                  f"max bids ${max(r['bids'] for r in mins):.3f}", file=sys.stderr)

    print(f"\n=== Dutch-book scan: {events_done} settled events, {total_minutes:,} event-minutes "
          f"(final {args.window_hours:g}h of each) ===")
    if not events_done:
        return 0
    print(f"cost to buy every bucket : median ${statistics.median(cost_samples):.3f}, "
          f"min ${min(cost_samples):.3f}")
    print(f"proceeds selling every one: median ${statistics.median(bid_samples):.3f}, "
          f"max ${max(bid_samples):.3f}\n")
    print(f"{'book':<14}{'profitable min':>15}{'% of min':>10}{'events hit':>13}"
          f"{'episodes':>10}{'median len':>12}{'longest':>9}")
    print("-" * 83)
    labels = {"long_gross": "LONG gross", "long_taker": "LONG taker",
              "short_gross": "SHORT gross", "short_taker": "SHORT taker"}
    for k in keys:
        ep = episodes[k]
        med = f"{statistics.median(ep):.0f} min" if ep else "-"
        lng = f"{max(ep)} min" if ep else "-"
        print(f"{labels[k]:<14}{hit_minutes[k]:>15,}{hit_minutes[k]/total_minutes*100:>9.2f}%"
              f"{hit_events[k]:>7} / {events_done:<4}{len(ep):>10}{med:>12}{lng:>9}")

    best.sort(reverse=True)
    if best:
        print(f"\nTop opportunities (edge per $1 set):")
        seen = set()
        shown = 0
        for edge, k, et, t, cost, bids in best:
            if (et, k) in seen:
                continue                     # one line per event per book
            seen.add((et, k))
            when = dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%m-%d %H:%M")
            print(f"  {labels[k]:<12} {et:28} {when}Z  edge {edge*100:+.2f}c  "
                  f"(sum asks ${cost:.3f}, sum bids ${bids:.3f})")
            shown += 1
            if shown >= args.top:
                break
    print("\nCaveats: 1-minute snapshots (hits may be far shorter), no size data (the cheap "
          "ask may be 1\ncontract), and all legs assumed to fill at once.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--families", default="high,low",
                    help="Comma list of weather families: high, low. Default high,low.")
    ap.add_argument("--events-per-series", type=int, default=8,
                    help="Settled events to scan per city series. Default 8.")
    ap.add_argument("--window-hours", type=float, default=24,
                    help="Scan the final N hours before close. Default 24.")
    ap.add_argument("--top", type=int, default=15, help="Opportunities to list. Default 15.")
    ap.add_argument("--cache-dir", default=os.path.join("data", "dutch_cache"),
                    help="Cache leg quotes here (gzipped). '' disables.")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
