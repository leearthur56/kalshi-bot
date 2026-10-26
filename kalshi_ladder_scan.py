#!/usr/bin/env python3
"""
Historical strike-ladder contradiction scanner for Kalshi above/below markets.
Read-only.

Within one event, "above K_hi" can never be worth more than "above K_lo" when
K_hi > K_lo: the underlying can't finish above K_hi without also finishing above
K_lo. So if the higher strike's YES BID ever exceeds the lower strike's YES ASK:

  buy YES on K_lo at its ask, sell YES on K_hi at its bid (= buy NO at 1 - bid)
  cost   = ask_lo + (1 - bid_hi)
  payoff = $1 if the underlying ends above K_hi or below K_lo,
           $2 if it ends between them (both legs win)
  guaranteed edge = bid_hi - ask_lo   (the "between" case is a free bonus)

After taker fees on both legs (coef 0.07, p(1-p) shape) the edge has to clear
those too. Ladders have dozens to hundreds of strikes, most stuck at 0c/$1, so we
only fetch the --near strikes on each side of where the event actually settled.

Limitations: 1-minute snapshots, no size data, both legs assumed to fill at once.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import statistics
import sys
from collections import OrderedDict

import kalshi_common as kc
from kalshi_dutch_scan import leg_quotes, _ts

TAKER = 0.07
DEFAULT_SERIES = "KXBTCD,KXETHD,KXSOLD,KXINXU,KXNASDAQ100U,KXGOLDD"


def fee(p: float) -> float:
    return TAKER * p * (1 - p)


def near_strikes(legs: list[dict], near: int):
    """Sort by strike, check results are monotone (YES...YES NO...NO), keep the
    `near` strikes on either side of the YES->NO flip. None if not a clean ladder."""
    legs = [m for m in legs if m.get("floor_strike") is not None
            and m.get("result") in ("yes", "no")]
    legs.sort(key=lambda m: float(m["floor_strike"]))
    res = [m["result"] for m in legs]
    if len(legs) < 2 or "yes" not in res or "no" not in res:
        return None
    flip = res.index("no")
    if any(r != "yes" for r in res[:flip]) or any(r != "no" for r in res[flip:]):
        return None                      # not a monotone ladder -- skip
    return legs[max(0, flip - near): flip + near]


def scan_event(legs: list[dict], args):
    close_ts = min(_ts(m["close_time"]) for m in legs)
    open_ts = max(_ts(m["open_time"]) for m in legs)
    start = max(open_ts, close_ts - int(args.window_hours * 3600))
    end = close_ts - 60
    quotes = []
    for m in legs:
        q = leg_quotes(m["ticker"], start, end, args.cache_dir)
        if q is None:
            return None
        quotes.append(q)

    stamps = sorted(set().union(*[q.keys() for q in quotes]))
    last = [None] * len(legs)
    out = []                             # per minute: (best_gross, best_net, detail)
    for t in stamps:
        for i, q in enumerate(quotes):
            if t in q:
                last[i] = q[t]
        live = [(i, v) for i, v in enumerate(last) if v is not None]
        if len(live) < 2:
            continue
        best_g = best_n = -9.0
        detail = None
        for a in range(len(live)):
            i, (bid_i, ask_i) = live[a]
            if not (0 < ask_i <= 1):
                continue                 # no offer on the lower strike
            for b in range(a + 1, len(live)):
                j, (bid_j, ask_j) = live[b]
                if bid_j <= 0:
                    continue             # nobody bidding on the higher strike
                g = bid_j - ask_i
                n = g - fee(ask_i) - fee(bid_j)
                if g > best_g:
                    best_g = g
                if n > best_n:
                    best_n = n
                    detail = (legs[i]["floor_strike"], ask_i, legs[j]["floor_strike"], bid_j)
        out.append((t, best_g, best_n, detail))
    return out


def run(args) -> int:
    series_list = args.series.split(",")
    tot = {"min": 0, "gross": 0, "net": 0, "events": 0, "ev_gross": 0, "ev_net": 0}
    episodes = []
    best = []
    per_series = []

    for s in series_list:
        events: "OrderedDict[str, list]" = OrderedDict()
        for m in kc.paginate_markets("settled", series_ticker=s, max_markets=args.max_markets):
            events.setdefault(m.get("event_ticker"), []).append(m)
        st = {"series": s, "events": 0, "min": 0, "gross": 0, "net": 0}
        for et, legs in events.items():
            if st["events"] >= args.events_per_series:
                break
            sel = near_strikes(legs, args.near)
            if not sel:
                continue
            mins = scan_event(sel, args)
            if not mins:
                continue
            st["events"] += 1
            g = sum(1 for r in mins if r[1] > 0)
            n = sum(1 for r in mins if r[2] > 0)
            st["min"] += len(mins)
            st["gross"] += g
            st["net"] += n
            tot["ev_gross"] += 1 if g else 0
            tot["ev_net"] += 1 if n else 0
            run_len = 0
            for t, _, net, det in mins:
                if net > 0:
                    run_len += 1
                    best.append((net, et, t, det))
                elif run_len:
                    episodes.append(run_len)
                    run_len = 0
            if run_len:
                episodes.append(run_len)
            print(f"  {et:30} {len(sel):2} strikes {len(mins):5} min  "
                  f"gross hits {g:4}  after-fee hits {n:4}", file=sys.stderr)
        per_series.append(st)
        for k in ("events", "min", "gross", "net"):
            tot[k] += st[k]

    print(f"\n=== Strike-ladder scan: {tot['events']} settled events, {tot['min']:,} event-minutes "
          f"({args.near} strikes each side of settlement, final {args.window_hours:g}h max) ===\n")
    print(f"{'series':<14}{'events':>7}{'minutes':>10}{'gross hits':>12}{'after fees':>12}")
    print("-" * 55)
    for st in per_series:
        print(f"{st['series']:<14}{st['events']:>7}{st['min']:>10,}{st['gross']:>12,}{st['net']:>12,}")
    print("-" * 55)
    print(f"{'TOTAL':<14}{tot['events']:>7}{tot['min']:>10,}{tot['gross']:>12,}{tot['net']:>12,}")
    if tot["min"]:
        print(f"\nminutes with a contradiction: {tot['gross']/tot['min']*100:.2f}% gross, "
              f"{tot['net']/tot['min']*100:.3f}% after taker fees")
        print(f"events with any after-fee hit: {tot['ev_net']} / {tot['events']}")
    if episodes:
        print(f"after-fee episodes: {len(episodes)}, median {statistics.median(episodes):.0f} min, "
              f"longest {max(episodes)} min")
    if best:
        best.sort(reverse=True)
        print("\nLargest after-fee contradictions (one per event):")
        seen, shown = set(), 0
        for net, et, t, det in best:
            if et in seen:
                continue
            seen.add(et)
            when = dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%m-%d %H:%M")
            lo, ask_lo, hi, bid_hi = det
            print(f"  {et:30} {when}Z  buy >{lo} @ {ask_lo*100:.0f}c, sell >{hi} @ "
                  f"{bid_hi*100:.0f}c  -> {net*100:+.2f}c after fees")
            shown += 1
            if shown >= args.top:
                break
    print("\nCaveats: 1-minute snapshots (hits may be far shorter), no size data, both legs "
          "assumed to fill at once.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", default=DEFAULT_SERIES,
                    help=f"Comma list of above/below series. Default {DEFAULT_SERIES}.")
    ap.add_argument("--events-per-series", type=int, default=15, help="Default 15.")
    ap.add_argument("--max-markets", type=int, default=6000,
                    help="Settled markets to page through per series (ladders are big). Default 6000.")
    ap.add_argument("--near", type=int, default=10,
                    help="Strikes to fetch on each side of where the event settled. Default 10.")
    ap.add_argument("--window-hours", type=float, default=24,
                    help="Scan at most the final N hours before close. Default 24.")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--cache-dir", default=os.path.join("data", "ladder_cache"))
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
