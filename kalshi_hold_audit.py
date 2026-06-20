#!/usr/bin/env python3
"""
Hold-duration loss audit: only "buy the favorite" if it has SAT at >= threshold
for longer than --min-hold-hours. Tests the idea that brief 99c spikes are the
dangerous ones, and a favorite that *holds* 99c for hours is genuinely decided.

Per market we pull hourly, volume-backed candles and, for each side, find the
longest CONSECUTIVE run it held >= threshold (using the candle LOW for the yes
favorite / candle HIGH for the no favorite, so a single dip breaks the run --
i.e. it really stayed there). If that run spans more than min-hold-hours, the
bot would have entered that side; we then check whether it won or lost.

Usage
-----
  python kalshi_hold_audit.py --min-hold-hours 2            # weather, 0.99, >2h
  python kalshi_hold_audit.py --min-hold-hours 2 --threshold 0.97
  python kalshi_hold_audit.py --min-hold-hours 0            # sanity: ~matches loss audit
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import kalshi_common as kc

WEATHER = [
    # original-naming cities
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHDEN",
    "KXHIGHAUS", "KXHIGHPHIL",
    # newer KXHIGHT<city> daily high-temp series
    "KXHIGHTSEA", "KXHIGHTLV", "KXHIGHTNOLA", "KXHIGHTOKC", "KXHIGHTATL",
    "KXHIGHTDAL", "KXHIGHTBOS", "KXHIGHTDC", "KXHIGHTMIN", "KXHIGHTSFO",
    "KXHIGHTPHX", "KXHIGHTSATX", "KXHIGHTHOU",
]

HOUR = 3600


def _ts(s: str | None):
    if not s:
        return None
    try:
        return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def held_candles(market: dict, thr: float):
    """
    Return (yes_fav_hours, no_fav_hours): the longest consecutive span in HOURS
    that the yes favorite (low >= thr) / no favorite (high <= 1-thr) held, using
    only volume-backed hourly candles. 0.0 if it never sustained.
    """
    t = market["ticker"]
    start, end = _ts(market.get("open_time")), _ts(market.get("close_time"))
    if start is None or end is None:
        return None
    try:
        data = kc.get(f"/series/{kc.series_of(t)}/markets/{t}/candlesticks",
                      {"start_ts": start - HOUR, "end_ts": end + 60,
                       "period_interval": 60})
    except RuntimeError:
        return None

    yes_hi_thr = thr           # yes favorite: yes price >= thr
    no_hi_thr = 1.0 - thr      # no favorite: yes price <= 1-thr

    rows = []  # (ts, is_yes_fav, is_no_fav)
    for c in data.get("candlesticks", []):
        try:
            if float(c.get("volume_fp") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        p = c.get("price", {})
        try:
            lo = float(p.get("low_dollars"))
            hi = float(p.get("high_dollars"))
        except (TypeError, ValueError):
            continue
        ts = c.get("end_period_ts")
        if ts is None:
            continue
        yes_fav = 0 < lo and lo >= yes_hi_thr          # never dipped below thr
        no_fav = 0 < hi <= no_hi_thr                   # never rose above 1-thr
        rows.append((int(ts), yes_fav, no_fav))

    if not rows:
        return None
    rows.sort()

    def longest_span(idx: int) -> float:
        best = 0.0
        run_start = None
        prev_ts = None
        for ts, *flags in rows:
            on = flags[idx]
            if on:
                if run_start is None or (prev_ts is not None and ts - prev_ts > 1.5 * HOUR):
                    run_start = ts  # start / restart run after a gap
                best = max(best, (ts - run_start) / HOUR)
            prev_ts = ts if on else prev_ts
            if not on:
                run_start = None
        return best

    return longest_span(0), longest_span(1)


def run(args) -> int:
    series_list = [args.series] if args.series else WEATHER
    thr, min_hold = args.threshold, args.min_hold_hours
    audited = entered = wins = losses = 0
    loss_rows = []

    for s in series_list:
        s_enter = s_loss = 0
        for m in kc.paginate_markets("settled", series_ticker=s, max_markets=args.max):
            result = m.get("result")
            if result not in ("yes", "no"):
                continue
            held = held_candles(m, thr)
            if held is None:
                continue
            audited += 1
            yes_hours, no_hours = held

            # Bot enters a side only if that side HELD >= thr for > min_hold hours.
            for side, hours, won in (("YES", yes_hours, result == "yes"),
                                     ("NO", no_hours, result == "no")):
                if hours > min_hold:
                    entered += 1
                    s_enter += 1
                    if won:
                        wins += 1
                    else:
                        losses += 1
                        s_loss += 1
                        loss_rows.append((m["ticker"], side, hours, result))
        print(f"  {s:12} entered(>{min_hold:g}h) {s_enter:4d} | LOST: {s_loss}",
              file=sys.stderr)

    print(f"\n=== HOLD AUDIT: buy favorite only if it held >= {thr:.2f} "
          f"for > {min_hold:g}h ===")
    print(f"Series: {', '.join(series_list)}")
    print(f"Markets audited: {audited}")
    print(f"Qualifying entries (favorite held long enough): {entered}")
    print(f"  wins: {wins}   losses: {losses}")
    if entered:
        rate = losses / entered
        if losses:
            print(f"Upset rate among sustained 99c: {rate*100:.2f}%  "
                  f"(1 loss per ~{1/rate:.0f} bets)")
        else:
            print("Upset rate among sustained 99c: 0.00%  (zero losses)")
        # crude P&L: win pays (1-thr), loss costs ~thr (price paid near thr)
        pnl = wins * (1 - thr) - losses * thr
        print(f"Crude P&L, 1 contract each at ~{thr*100:.0f}c: "
              f"{wins} x +{(1-thr)*100:.0f}c  -  {losses} x ~{thr*100:.0f}c "
              f"= ${pnl:+.2f}")
    if loss_rows:
        print("\nSustained-99c favorites that STILL lost:")
        print(f"{'TICKER':<26} {'SIDE':<5} {'HELD(h)':>8} {'RESULT':>7}")
        for t, side, hours, res in loss_rows:
            print(f"{t:<26} {side:<5} {hours:>7.1f} {res:>7}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", type=str, default=None,
                    help="One series ticker. Default: all weather cities.")
    ap.add_argument("--threshold", type=float, default=0.99,
                    help="Favorite price the bot waits to see. Default 0.99.")
    ap.add_argument("--min-hold-hours", type=float, default=2.0,
                    help="Require the favorite to hold >= threshold longer than this. Default 2.")
    ap.add_argument("--max", type=int, default=None, help="Cap markets per series.")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
