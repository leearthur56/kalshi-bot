#!/usr/bin/env python3
"""
Loss audit: across all AVAILABLE history of a series, would "buy the favorite at
>= threshold" ever have lost?

Why this is different from the backtest
---------------------------------------
The backtest enters at one snapshot price. To ask "did it EVER lose", that's too
narrow -- and last_price hides losses (a favorite that lost has a last trade near
the loser). So per market we instead scan the FULL candlestick history and ask:
did the side that ultimately LOST ever trade at >= threshold? If yes, the bot
buying the favorite at that moment would have taken a -near-dollar loss.

Per market over its whole life (hourly candles):
  max_yes_ask  = highest price to BUY yes   -> YES favorite peak
  min_yes_bid  = lowest yes bid             -> NO favorite peak = 1 - min_yes_bid
A loss is recorded when:
  result == 'no'  and max_yes_ask        >= threshold   (YES favorite, lost)
  result == 'yes' and (1 - min_yes_bid)  >= threshold   (NO favorite, lost)

Usage
-----
  python kalshi_loss_audit.py                       # all weather cities, 0.99
  python kalshi_loss_audit.py --threshold 0.95
  python kalshi_loss_audit.py --series KXHIGHNY
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import kalshi_common as kc

WEATHER = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX",
           "KXHIGHDEN", "KXHIGHAUS", "KXHIGHPHIL"]


def life_extremes(market: dict):
    """
    Return (max_yes_traded, min_yes_traded) over the market's full life from
    actual TRADES (candles with volume), or None. We use traded prices, not
    bid/ask, because an empty order book reports sentinel ask=1.00 / bid=0.00
    that would otherwise look like fake 99c quotes.
    """
    t = market["ticker"]
    ot, ct = market.get("open_time"), market.get("close_time")
    if not ot or not ct:
        return None
    try:
        start = int(dt.datetime.fromisoformat(ot.replace("Z", "+00:00")).timestamp())
        end = int(dt.datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None
    try:
        data = kc.get(f"/series/{kc.series_of(t)}/markets/{t}/candlesticks",
                      {"start_ts": start - 3600, "end_ts": end + 60,
                       "period_interval": 60})
    except RuntimeError:
        return None
    max_hi, min_lo = None, None
    for c in data.get("candlesticks", []):
        try:
            if float(c.get("volume_fp") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        price = c.get("price", {})
        try:
            hi = float(price.get("high_dollars"))
            if 0 < hi <= 1 and (max_hi is None or hi > max_hi):
                max_hi = hi
        except (TypeError, ValueError):
            pass
        try:
            lo = float(price.get("low_dollars"))
            if 0 < lo <= 1 and (min_lo is None or lo < min_lo):
                min_lo = lo
        except (TypeError, ValueError):
            pass
    if max_hi is None and min_lo is None:
        return None
    return max_hi, min_lo


def run(args) -> int:
    series_list = [args.series] if args.series else WEATHER
    thr = args.threshold
    audited = reached = losses = 0
    loss_rows = []

    for s in series_list:
        s_audit = s_reached = s_loss = 0
        for m in kc.paginate_markets("settled", series_ticker=s, max_markets=args.max):
            result = m.get("result")
            if result not in ("yes", "no"):
                continue
            ext = life_extremes(m)
            if ext is None:
                continue
            audited += 1
            s_audit += 1
            max_ask, min_bid = ext
            yes_fav_peak = max_ask if max_ask is not None else 0.0
            no_fav_peak = (1 - min_bid) if min_bid is not None else 0.0

            # Did a favorite ever reach the threshold, and did it then lose?
            yes_fav_hit = yes_fav_peak >= thr
            no_fav_hit = no_fav_peak >= thr
            if yes_fav_hit or no_fav_hit:
                reached += 1
                s_reached += 1
            lost = (result == "no" and yes_fav_hit) or (result == "yes" and no_fav_hit)
            if lost:
                losses += 1
                s_loss += 1
                side = "YES" if (result == "no" and yes_fav_hit) else "NO"
                peak = yes_fav_peak if side == "YES" else no_fav_peak
                loss_rows.append((m["ticker"], side, peak, result))
        print(f"  {s:12} audited {s_audit:4d} | favorite hit >={thr:.2f}: "
              f"{s_reached:4d} | LOST: {s_loss}", file=sys.stderr)

    print(f"\n=== LOSS AUDIT: buy the favorite at >= {thr:.2f} ===")
    print(f"Series: {', '.join(series_list)}")
    print(f"Markets audited (full available history): {audited}")
    print(f"Markets where a favorite reached >= {thr:.2f}: {reached}")
    print(f"Of those, times the favorite LOST: {losses}")
    if reached:
        rate = losses / reached
        print(f"Empirical upset rate at >= {thr:.2f}: {rate*100:.2f}%  "
              f"(1 loss per ~{(1/rate):.0f} bets)" if losses else
              f"Empirical upset rate at >= {thr:.2f}: 0.00%  (zero losses in sample)")
    if loss_rows:
        print("\nThe losing bets the bot WOULD have taken:")
        print(f"{'TICKER':<26} {'BOUGHT':<6} {'PEAK PRICE':>10} {'RESULT':>7}")
        for t, side, peak, res in loss_rows:
            print(f"{t:<26} {side:<6} {peak*100:>9.1f}c {res:>7}")
        total_loss = sum(peak for _, _, peak, _ in loss_rows)  # ~ price paid per loss
        print(f"\nEach such loss costs ~ the price paid (near -{thr*100:.0f}c). "
              f"Buying 1 contract each, these {len(loss_rows)} upsets alone cost "
              f"~${total_loss:.2f}.")
    else:
        print("\nNo historical loss found in the available window. But note: this is\n"
              "~2 months of data, the favorite still resolved correctly only because\n"
              "no tail event landed yet. A true 99c contract must lose ~1/100 long-run.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", type=str, default=None,
                    help="One series ticker. Default: all weather cities.")
    ap.add_argument("--threshold", type=float, default=0.99,
                    help="Favorite price that triggers a (hypothetical) buy. Default 0.99.")
    ap.add_argument("--max", type=int, default=None, help="Cap markets per series.")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
