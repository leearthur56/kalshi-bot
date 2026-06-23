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


def candles_1m(market: dict):
    t = market["ticker"]
    try:
        ot = int(dt.datetime.fromisoformat(market["open_time"].replace("Z", "+00:00")).timestamp())
        ct = int(dt.datetime.fromisoformat(market["close_time"].replace("Z", "+00:00")).timestamp())
    except (ValueError, KeyError):
        return None
    try:
        data = kc.get(f"/series/{kc.series_of(t)}/markets/{t}/candlesticks",
                      {"start_ts": ot - 60, "end_ts": ct + 60, "period_interval": 1})
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

    for m in kc.paginate_markets("settled", series_ticker="KXBTC15M", max_markets=args.max):
        result = m.get("result")
        if result not in ("yes", "no"):
            continue
        rows = candles_1m(m)
        if not rows or len(rows) < 3:
            continue

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

    print(f"\n=== KXBTC15M 'leg into a box' @ entry <= {entry*100:.0f}c ===")
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
    ap.add_argument("--entry", type=float, default=0.40,
                    help="Buy a side when offered at <= this price. Default 0.40.")
    ap.add_argument("--max", type=int, default=400, help="Settled markets to scan. Default 400.")
    ap.add_argument("--fee-coef", type=float, default=kc.DEFAULT_FEE_COEF)
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
