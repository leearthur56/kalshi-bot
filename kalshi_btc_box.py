#!/usr/bin/env python3
"""
Backtest the "leg into a box" strategy on Kalshi's KXBTC15M 1-minute prices.

Strategy (your idea)
--------------------
Within a 15-min market: buy one side when it's offered at <= --entry (e.g. 40c).
Then if the OTHER side later drops to <= --entry too, buy it -> you now hold both
sides for < $1 and lock a guaranteed profit (a "box"). If the other side never
gets there, you're stuck holding one leg, which resolves win/lose.

Fair-market math: from 40c the second leg arrives ~66.7% of the time; the other
33.3% you lose the leg. EV nets to ~0 before fees. It only wins if prices
MEAN-REVERT (whipsaw) more than that -- which is exactly what this measures on
real data: the completion rate and the net P&L, after fees.

Data: KXBTC15M settled markets + their 1-minute candlesticks (public).
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import kalshi_common as kc


def candles_1m(market: dict, interval: int = 1, lookback_hours: float = 0):
    t = market["ticker"]
    try:
        ot = int(dt.datetime.fromisoformat(market["open_time"].replace("Z", "+00:00")).timestamp())
        ct = int(dt.datetime.fromisoformat(market["close_time"].replace("Z", "+00:00")).timestamp())
    except (ValueError, KeyError):
        return None
    # For long-lived markets (sports open days early) bound the 1-min fetch to the
    # final lookback_hours before close -- that's where the game actually plays.
    start = ot if lookback_hours <= 0 else max(ot, ct - int(lookback_hours * 3600))
    try:
        data = kc.get(f"/series/{kc.series_of(t)}/markets/{t}/candlesticks",
                      {"start_ts": start - 60, "end_ts": ct + 60, "period_interval": interval})
    except RuntimeError:
        return None
    out = []
    for c in data.get("candlesticks", []):
        try:
            yb = float(c.get("yes_bid", {}).get("close_dollars"))
            ya = float(c.get("yes_ask", {}).get("close_dollars"))
        except (TypeError, ValueError):
            continue
        if 0 < yb <= 1 and 0 < ya <= 1:
            out.append((yb, ya))
    return out


def run(args) -> int:
    entry, coef = args.entry, args.fee_coef
    fee = coef * entry * (1 - entry)        # ~per-contract fee in size at the entry price
    entries = completes = 0
    pnl = 0.0
    stuck_win = stuck_lose = 0
    scanned = even = 0

    for m in kc.paginate_markets("settled", series_ticker=args.series, max_markets=args.max):
        result = m.get("result")
        if result not in ("yes", "no"):
            continue
        rows = candles_1m(m, args.interval, args.candle_hours)
        if not rows or len(rows) < 3:
            continue
        scanned += 1

        # pregame evenness filter: use the EARLIEST price as the pregame line
        yb0, ya0 = rows[0]
        pregame = (yb0 + ya0) / 2
        if not (args.pregame_lo <= pregame <= args.pregame_hi):
            continue
        even += 1

        side = costA = None
        completed = False
        for yb, ya in rows:
            no_ask = 1 - yb                  # price to BUY the NO side
            if side is None:                 # look for first leg at <= entry
                if 0 < ya <= entry:
                    side, costA = "YES", ya
                elif 0 < no_ask <= entry:
                    side, costA = "NO", no_ask
            else:                            # look to complete with the other side
                other = no_ask if side == "YES" else ya
                if 0 < other <= entry:
                    completed = True
                    costB = other
                    break

        if side is None:
            continue                         # never reached entry price; no trade
        entries += 1
        if completed:
            completes += 1
            pnl += 1.0 - costA - costB - 2 * fee
        else:
            win = (result == "yes" and side == "YES") or (result == "no" and side == "NO")
            pnl += (1.0 if win else 0.0) - costA - fee
            stuck_win += 1 if win else 0
            stuck_lose += 0 if win else 1

    print(f"\n=== {args.series} 'leg into a box' @ entry <= {entry*100:.0f}c ===")
    if args.pregame_lo > 0 or args.pregame_hi < 1:
        print(f"pregame filter: {args.pregame_lo:.2f}-{args.pregame_hi:.2f}  "
              f"({even} of {scanned} markets were that even)")
    print(f"markets with a trade: {entries}")
    if entries == 0:
        print("no entries"); return 0
    crate = completes / entries
    breakeven = entry / (1 - entry)          # completion rate needed to break even (gross)
    print(f"completed the box: {completes} ({crate*100:.1f}%)")
    print(f"  break-even completion rate (gross): {breakeven*100:.1f}%  "
          f"-> {'ABOVE (edge!)' if crate>breakeven else 'below (no edge)'}")
    print(f"stuck single leg: {entries-completes}  (won {stuck_win}, lost {stuck_lose})")
    print(f"net P&L per trade (after fees): {pnl/entries*100:+.2f}c")
    print(f"total net P&L (1 contract/leg): {pnl*100:+.0f}c  on {entries} trades")
    print("\nCompletion rate ABOVE break-even AND positive per-trade P&L = the market\n"
          "whipsaws enough to beat the no-free-lunch baseline. Otherwise it doesn't.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", type=str, default="KXBTC15M",
                    help="Series ticker to backtest (default KXBTC15M). Try live single-game "
                         "sports series for whipsaw.")
    ap.add_argument("--entry", type=float, default=0.40,
                    help="Buy a side when offered at <= this price. Default 0.40.")
    ap.add_argument("--max", type=int, default=400, help="Settled markets to scan. Default 400.")
    ap.add_argument("--interval", type=int, default=1,
                    help="Candlestick minutes (1 for 15m markets; use 5+ for multi-hour games).")
    ap.add_argument("--candle-hours", type=float, default=0,
                    help="For long-lived markets, only fetch the final N hours before close "
                         "(e.g. 6 for sports games). 0 = whole life.")
    ap.add_argument("--pregame-lo", type=float, default=0.0,
                    help="Only trade markets whose earliest (pregame) price >= this. "
                         "Use 0.42 with --pregame-hi 0.58 for ~even matchups.")
    ap.add_argument("--pregame-hi", type=float, default=1.0,
                    help="Only trade markets whose earliest (pregame) price <= this.")
    ap.add_argument("--fee-coef", type=float, default=kc.DEFAULT_FEE_COEF)
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
