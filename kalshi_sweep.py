#!/usr/bin/env python3
"""
Sweep the "buy the favorite" backtest across MANY series and rank them.

Pages through settled markets, groups them by series ticker, and reports per
series: how often the favorite won vs. what its price implied, net of fees.
Use it to find *which* series (if any) show a real favorite-longshot edge, then
deep-dive the winners with kalshi_backtest.py --lead-hours for an unbiased read.

Entry-price modes (same trade-off as the single backtest):
  default        : last_price -- fast, but BIASED toward the outcome (final
                   trade sits right before settlement). Good for ranking and
                   spotting liquid series; do NOT trust the absolute edge.
  --lead-hours H : candlestick mid H hours before close -- unbiased, but one
                   extra API call per market (slow across thousands).

Usage
-----
  python kalshi_sweep.py --max 20000 --min-price 0.95 --min-volume 500
  python kalshi_sweep.py --max 20000 --min-n 30 --sort net_ev
  python kalshi_sweep.py --max 1500 --lead-hours 6 --min-price 0.95   # honest, slow
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import kalshi_common as kc


def parse_close_ts(market: dict) -> int | None:
    s = market.get("close_time") or market.get("expiration_time")
    if not s:
        return None
    try:
        return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def entry_price(market: dict, lead_hours: int | None) -> float | None:
    if lead_hours is None:
        last = kc.fnum(market, "last_price_dollars")
        return last if last > 0 else None
    close_ts = parse_close_ts(market)
    if close_ts is None:
        return None
    return kc.candlestick_price_at(market["ticker"], close_ts - lead_hours * 3600)


def run(args) -> int:
    # series -> [n, wins, gross_pnl, net_pnl, sum_price, sum_volume]
    agg: dict[str, list] = {}
    scanned = qualified = 0

    for m in kc.paginate_markets("settled", max_markets=args.max):
        scanned += 1
        if scanned % 2000 == 0:
            print(f"  ...scanned {scanned} settled markets", file=sys.stderr)
        result = m.get("result")
        if result not in ("yes", "no"):
            continue
        vol = kc.fnum(m, "volume_fp")
        if vol < args.min_volume:
            continue
        yp = entry_price(m, args.lead_hours)
        if yp is None or not (0 < yp < 1):
            continue

        if yp >= 0.5:
            fav_price, fav_won = yp, (result == "yes")
        else:
            fav_price, fav_won = 1 - yp, (result == "no")
        if fav_price < args.min_price:
            continue
        qualified += 1

        fee = kc.fee_per_contract(fav_price, args.order_size, args.fee_coef)
        gross = (1 - fav_price) if fav_won else (-fav_price)
        s = kc.series_of(m["ticker"])
        a = agg.setdefault(s, [0, 0, 0.0, 0.0, 0.0, 0.0])
        a[0] += 1
        a[1] += 1 if fav_won else 0
        a[2] += gross
        a[3] += gross - fee
        a[4] += fav_price
        a[5] += vol

    rows = []
    for s, (n, wins, gross, net, sump, sumv) in agg.items():
        if n < args.min_n:
            continue
        implied, actual = sump / n, wins / n
        rows.append({
            "series": s, "n": n, "implied": implied, "actual": actual,
            "edge": actual - implied, "net_ev": net / n, "total_net": net,
            "avg_vol": sumv / n,
        })

    key = {"net_ev": lambda r: r["net_ev"],
           "total_net": lambda r: r["total_net"],
           "edge": lambda r: r["edge"],
           "n": lambda r: r["n"]}[args.sort]
    rows.sort(key=key, reverse=True)

    mode = ("last_price (BIASED, optimistic)" if args.lead_hours is None
            else f"candlestick mid {args.lead_hours}h before close (unbiased)")
    print(f"\nScanned {scanned} settled markets; {qualified} qualifying favorites "
          f"(>= {args.min_price:.2f}, vol >= {args.min_volume:g}).")
    print(f"Entry mode: {mode} | order size {args.order_size} | "
          f"sorted by {args.sort}, series with N >= {args.min_n}\n")

    header = (f"{'SERIES':<22} {'N':>5} {'IMPLIED':>8} {'ACTUAL':>8} {'EDGE':>7} "
              f"{'NET EV':>8} {'TOTAL NET':>10} {'AVG VOL':>9}")
    print(header)
    print("-" * len(header))
    tot_n = tot_net = 0.0
    for r in rows:
        print(f"{r['series'][:22]:<22} {r['n']:>5d} {r['implied']*100:>7.2f}% "
              f"{r['actual']*100:>7.2f}% {r['edge']*100:>+6.2f}% "
              f"{r['net_ev']*100:>+7.2f}c {r['total_net']*100:>+9.2f}c "
              f"{r['avg_vol']:>9.0f}")
        tot_n += r["n"]
        tot_net += r["total_net"]
    print("-" * len(header))
    if tot_n:
        print(f"{'ALL (shown)':<22} {int(tot_n):>5d} {'':>8} {'':>8} {'':>7} "
              f"{tot_net/tot_n*100:>+7.2f}c {tot_net*100:>+9.2f}c")

    print(
        "\nRead: EDGE = actual win% - implied. Positive = favorite beats its price.\n"
        "NET EV = per-contract profit after fees. TOTAL NET = full P&L for that series.\n"
    )
    if args.lead_hours is None:
        print("WARNING: last_price mode is biased toward the outcome, so ACTUAL/EDGE\n"
              "are inflated. Use this only to RANK series and find liquid ones, then\n"
              "re-run the top few with --lead-hours 6 for an honest edge before trusting it.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max", type=int, default=20000, help="Max settled markets to scan.")
    ap.add_argument("--min-price", type=float, default=0.95, help="Favorite price floor.")
    ap.add_argument("--min-volume", type=float, default=500.0, help="Skip thin markets.")
    ap.add_argument("--min-n", type=int, default=20, help="Hide series with fewer than N trades.")
    ap.add_argument("--order-size", type=int, default=100, help="Contracts per order for fees.")
    ap.add_argument("--lead-hours", type=int, default=None, help="Unbiased candlestick entry (slow).")
    ap.add_argument("--fee-coef", type=float, default=kc.DEFAULT_FEE_COEF, help="Fee coefficient.")
    ap.add_argument("--sort", choices=["net_ev", "total_net", "edge", "n"],
                    default="net_ev", help="Ranking key. Default net_ev.")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
