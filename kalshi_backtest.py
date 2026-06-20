#!/usr/bin/env python3
"""
Backtest the "buy the heavy favorite" strategy on SETTLED Kalshi markets.

The question this answers
-------------------------
You suspect that when a market sits near 99c/1c, holders of the dying 1c side
don't bother to sell, so the favorite stays slightly *underpriced* and may win
MORE often than its price implies (favorite-longshot bias + the 1c tick floor).
This tool measures that directly: for real settled markets, it buckets trades by
entry price and reports, per bucket, how often the favorite actually won vs. what
the price implied -- net of Kalshi fees.

Two entry-price modes
----------------------
  default        : use the market's final `last_price`. Fast (no extra calls),
                   but BIASED -- the last trade is recorded right before close,
                   when the price has already drifted toward the true outcome.
                   Treat default results as an optimistic upper bound.
  --lead-hours H : reconstruct the mid price H hours BEFORE close from
                   candlesticks. Unbiased (no peeking at the result) but slow:
                   one extra API call per market.

Usage
-----
  python kalshi_backtest.py --max 3000 --min-volume 100
  python kalshi_backtest.py --series KXHIGHNY --min-price 0.90
  python kalshi_backtest.py --max 500 --lead-hours 6 --min-volume 500
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import kalshi_common as kc

BUCKETS = [(0.50, 0.80), (0.80, 0.90), (0.90, 0.95), (0.95, 0.97),
           (0.97, 0.98), (0.98, 0.99), (0.99, 1.0001)]


def parse_close_ts(market: dict) -> int | None:
    s = market.get("close_time") or market.get("expiration_time")
    if not s:
        return None
    try:
        return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def entry_price(market: dict, lead_hours: int | None) -> float | None:
    """Favorite-agnostic YES entry price (0..1)."""
    if lead_hours is None:
        last = kc.fnum(market, "last_price_dollars")
        return last if last > 0 else None
    close_ts = parse_close_ts(market)
    if close_ts is None:
        return None
    return kc.candlestick_price_at(market["ticker"], close_ts - lead_hours * 3600)


def run(args) -> int:
    fee_coef = args.fee_coef
    # bucket -> [n, wins, gross_pnl, net_pnl, sum_entry_price]
    stats = {b: [0, 0, 0.0, 0.0, 0.0] for b in BUCKETS}
    scanned = priced = 0

    for m in kc.paginate_markets("settled", series_ticker=args.series,
                                 max_markets=args.max):
        scanned += 1
        result = m.get("result")
        if result not in ("yes", "no"):
            continue
        if kc.fnum(m, "volume_fp") < args.min_volume:
            continue
        yp = entry_price(m, args.lead_hours)
        if yp is None or not (0 < yp < 1):
            continue

        # Bet on whichever side is the favorite at entry.
        if yp >= 0.5:
            fav_side, fav_price, fav_won = "yes", yp, (result == "yes")
        else:
            fav_side, fav_price, fav_won = "no", 1 - yp, (result == "no")
        if fav_price < args.min_price:
            continue
        priced += 1

        bucket = next((b for b in BUCKETS if b[0] <= fav_price < b[1]), None)
        if bucket is None:
            continue
        fee = kc.fee_per_contract(fav_price, args.order_size, fee_coef)
        gross = (1 - fav_price) if fav_won else (-fav_price)
        s = stats[bucket]
        s[0] += 1
        s[1] += 1 if fav_won else 0
        s[2] += gross
        s[3] += gross - fee
        s[4] += fav_price

    mode = ("last_price (BIASED, optimistic)" if args.lead_hours is None
            else f"candlestick mid {args.lead_hours}h before close (unbiased)")
    print(f"\nScanned {scanned} settled markets; {priced} qualified "
          f"(favorite >= {args.min_price:.2f}, vol >= {args.min_volume:g}).")
    print(f"Entry price mode: {mode}")
    print(f"Order size: {args.order_size} contracts "
          f"(fee rounded up once per order, then amortized per contract)\n")

    header = (f"{'ENTRY PRICE':<13} {'N':>6} {'IMPLIED':>8} {'ACTUAL':>8} "
              f"{'EDGE':>7} {'GROSS EV':>9} {'NET EV':>8} {'TOTAL NET':>10}")
    print(header)
    print("-" * len(header))
    tot_n = tot_net = 0.0
    for b in BUCKETS:
        n, wins, gross, net, sump = stats[b]
        if n == 0:
            continue
        implied = sump / n
        actual = wins / n
        edge = actual - implied
        print(f"{int(b[0]*100):>2d}-{int(b[1]*100):>3d}c     {n:>6d} "
              f"{implied*100:>7.2f}% {actual*100:>7.2f}% "
              f"{edge*100:>+6.2f}% {gross/n*100:>+8.2f}c {net/n*100:>+7.2f}c "
              f"{net*100:>+9.2f}c")
        tot_n += n
        tot_net += net

    print("-" * len(header))
    if tot_n:
        print(f"{'ALL':<13} {int(tot_n):>6d} {'':>8} {'':>8} {'':>7} "
              f"{'':>9} {tot_net/tot_n*100:>+7.2f}c {tot_net*100:>+9.2f}c")
        print(f"\nNet P&L buying every qualifying favorite (1 contract each): "
              f"{tot_net*100:+.0f} cents = ${tot_net:+.2f}")
    print(
        "\nHow to read this:\n"
        "  IMPLIED = avg entry price (the market's stated probability).\n"
        "  ACTUAL  = how often the favorite actually won.\n"
        "  EDGE    = ACTUAL - IMPLIED. Positive = favorite won MORE than priced\n"
        "            (your hypothesis). But NET EV is what you keep after fees.\n"
        "  A positive EDGE that turns into a negative NET EV means the fee ate it."
    )
    if args.lead_hours is None:
        print("\n  NOTE: default mode uses last_price, which is recorded just before\n"
              "  close and is biased toward the outcome. Re-run with --lead-hours 6\n"
              "  for an honest, non-peeking entry price before trusting any edge.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max", type=int, default=3000, help="Max settled markets to scan.")
    ap.add_argument("--series", type=str, default=None,
                    help="Restrict to one series ticker, e.g. KXHIGHNY.")
    ap.add_argument("--min-price", type=float, default=0.90,
                    help="Only count favorites priced >= this. Default 0.90.")
    ap.add_argument("--min-volume", type=float, default=100.0,
                    help="Skip thin markets below this volume. Default 100.")
    ap.add_argument("--lead-hours", type=int, default=None,
                    help="Use candlestick mid this many hours before close (unbiased, slow).")
    ap.add_argument("--order-size", type=int, default=100,
                    help="Contracts per order; fee is rounded up once per order, "
                         "so larger orders pay less per contract. Default 100.")
    ap.add_argument("--fee-coef", type=float, default=kc.DEFAULT_FEE_COEF,
                    help="Kalshi fee coefficient (0.07 standard, 0.035 for some).")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
