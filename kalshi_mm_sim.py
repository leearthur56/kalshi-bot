#!/usr/bin/env python3
"""
Paper market-making simulator on Kalshi's real trade history. FAKE MONEY.

Each minute we post a bid and an ask (sized --size contracts each) based on the
PREVIOUS minute's quote, so there is no look-ahead. Then we replay the market's
actual trades during that minute:

  * a trade where the taker SOLD yes (taker_side == "no") at or below our bid
    fills our bid  -> we buy YES
  * a trade where the taker BOUGHT yes (taker_side == "yes") at or above our ask
    fills our ask  -> we sell YES

Inventory is capped at +/- --max-inv; at settlement any open inventory pays
$1 (yes) or $0 (no). Maker fees use Kalshi's maker coefficient (0.0175).

P&L is split into:
  spread captured  = what each fill earned versus the mid at the time
  inventory P&L    = everything else: price moving against what you hold
                     (adverse selection) plus settling leftover inventory
Market makers live on the first and die on the second.

Limitation: queue position is ignored. We assume we fill whenever a trade
prints at our price, but real makers wait behind earlier orders. Fills are
therefore OVERcounted: treat a profit as an upper bound, a loss as conclusive.

Quote modes
  touch   quote at the market's best bid/ask (join the inside; most fills)
  offset  quote at mid +/- --half-spread (wider; fewer, safer fills)
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys

import kalshi_common as kc

MAKER_COEF = 0.0175


def _ts(s: str) -> int:
    return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def fetch_quotes(ticker: str, start: int, end: int) -> dict[int, tuple[float, float]]:
    """minute-end ts -> (yes_bid, yes_ask) from 1-minute candlesticks."""
    try:
        data = kc.get(f"/series/{kc.series_of(ticker)}/markets/{ticker}/candlesticks",
                      {"start_ts": start - 60, "end_ts": end + 60, "period_interval": 1})
    except RuntimeError:
        return {}
    out = {}
    for c in data.get("candlesticks", []):
        try:
            yb = float(c["yes_bid"]["close_dollars"])
            ya = float(c["yes_ask"]["close_dollars"])
            e = int(c["end_period_ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < yb <= ya <= 1:
            out[e] = (yb, ya)
    return out


def fetch_trades(ticker: str, start: int, end: int, max_pages: int):
    """Sorted [(ts, yes_price, count, taker_side)], plus a truncated flag."""
    out, cursor = [], None
    for _ in range(max_pages):
        params = {"ticker": ticker, "min_ts": start, "max_ts": end, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        try:
            d = kc.get("/markets/trades", params)
        except RuntimeError:
            return None, True
        for tr in d.get("trades", []):
            try:
                out.append((_ts(tr["created_time"]), float(tr["yes_price_dollars"]),
                            float(tr["count_fp"]), tr.get("taker_side")))
            except (KeyError, TypeError, ValueError):
                continue
        cursor = d.get("cursor")
        if not cursor:
            out.sort()
            return out, False
    out.sort()
    return out, True


def prefetch(m: dict, args):
    """Download one market's quotes + trades once. Returns tuple, None, or 'truncated'."""
    try:
        open_ts, close_ts = _ts(m["open_time"]), _ts(m["close_time"])
    except (KeyError, ValueError):
        return None
    start = open_ts if args.window_hours <= 0 else max(open_ts, close_ts - int(args.window_hours * 3600))
    quotes = fetch_quotes(m["ticker"], start, close_ts)
    if len(quotes) < 5:
        return None
    trades, truncated = fetch_trades(m["ticker"], start, close_ts, args.max_trade_pages)
    if trades is None or truncated:
        return "truncated"
    return quotes, trades, close_ts


def simulate(m: dict, data, cfg: dict, args):
    quotes, trades, close_ts = data
    keys = sorted(quotes)
    stop_ts = close_ts - int(cfg["stop_before_min"] * 60)
    inv = cash = spread = fees = volume = 0.0
    fills = 0
    max_abs_inv = 0.0
    ti = 0

    for i, k in enumerate(keys):
        yb, ya = quotes[k]
        mid = (yb + ya) / 2
        nxt = keys[i + 1] if i + 1 < len(keys) else close_ts
        while ti < len(trades) and trades[ti][0] <= k:
            ti += 1                      # trades before this quote took effect

        if cfg["quote"] == "touch":
            bid, ask = yb, ya
        else:
            bid = max(0.01, math.floor((mid - cfg["half_spread"]) * 100 + 1e-9) / 100)
            ask = min(0.99, math.ceil((mid + cfg["half_spread"]) * 100 - 1e-9) / 100)
        quoting = k < stop_ts and args.quote_lo <= mid <= args.quote_hi and bid < ask
        bid_left = args.size if (quoting and inv < args.max_inv) else 0.0
        ask_left = args.size if (quoting and inv > -args.max_inv) else 0.0

        while ti < len(trades) and trades[ti][0] <= nxt:
            _, p, cnt, side = trades[ti]
            ti += 1
            if side == "no" and bid_left > 0 and p <= bid:        # seller hit bids
                q = min(bid_left, cnt, args.max_inv - inv)
                if q <= 0:
                    continue
                bid_left -= q
                inv += q
                cash -= q * bid
                spread += q * (mid - bid)
                fees += MAKER_COEF * q * bid * (1 - bid)
            elif side == "yes" and ask_left > 0 and p >= ask:     # buyer lifted asks
                q = min(ask_left, cnt, args.max_inv + inv)
                if q <= 0:
                    continue
                ask_left -= q
                inv -= q
                cash += q * ask
                spread += q * (ask - mid)
                fees += MAKER_COEF * q * ask * (1 - ask)
            else:
                continue
            fills += 1
            volume += q
            max_abs_inv = max(max_abs_inv, abs(inv))

    settle = 1.0 if m["result"] == "yes" else 0.0
    total = cash + inv * settle - fees
    return {"ticker": m["ticker"], "fills": fills, "volume": volume,
            "spread": spread, "fees": fees, "inv_pnl": total + fees - spread,
            "total": total, "final_inv": inv, "max_inv": max_abs_inv}


def cfg_label(cfg: dict) -> str:
    q = ("touch" if cfg["quote"] == "touch"
         else f"mid+/-{cfg['half_spread']*100:.0f}c")
    return q + (f", stop {cfg['stop_before_min']:g}m early" if cfg["stop_before_min"] else "")


def run(args) -> int:
    if args.compare:
        stop = args.compare_stop_min
        cfgs = [{"quote": "touch", "half_spread": 0, "stop_before_min": 0},
                {"quote": "touch", "half_spread": 0, "stop_before_min": stop},
                {"quote": "offset", "half_spread": args.half_spread, "stop_before_min": 0},
                {"quote": "offset", "half_spread": args.half_spread, "stop_before_min": stop}]
    else:
        cfgs = [{"quote": args.quote, "half_spread": args.half_spread,
                 "stop_before_min": args.stop_before_min}]

    results = {i: [] for i in range(len(cfgs))}
    skipped_trunc = scanned = 0
    for m in kc.paginate_markets("settled", series_ticker=args.series, max_markets=args.max):
        if m.get("result") not in ("yes", "no"):
            continue
        data = prefetch(m, args)
        if data == "truncated":
            skipped_trunc += 1
            continue
        if data is None:
            continue
        scanned += 1
        print(f"  [{scanned}] {m['ticker']}", file=sys.stderr)
        for i, cfg in enumerate(cfgs):
            r = simulate(m, data, cfg, args)
            if r["fills"] > 0:
                results[i].append(r)

    print(f"\n######## {args.series}: {scanned} markets simulated | size {args.size:g}/side, "
          f"max inventory {args.max_inv:g} ########")
    if skipped_trunc:
        print(f"(skipped {skipped_trunc} markets whose trade history exceeded the page cap)")
    for i, cfg in enumerate(cfgs):
        print(f"\n--- {cfg_label(cfg)} ---")
        summarize(results[i])
    return 0


def summarize(rows: list) -> None:
    if not rows:
        print("no markets with fills")
        return
    n = len(rows)
    tot = {k: sum(r[k] for r in rows) for k in ("fills", "volume", "spread", "fees",
                                                  "inv_pnl", "total")}
    winners = sum(1 for r in rows if r["total"] > 0)
    worst = min(rows, key=lambda r: r["total"])
    best = max(rows, key=lambda r: r["total"])
    print(f"markets with fills: {n}   fills: {tot['fills']:.0f}   "
          f"contracts traded: {tot['volume']:,.0f}")
    print(f"spread captured : ${tot['spread']:+,.2f}")
    print(f"maker fees      : ${-tot['fees']:+,.2f}")
    print(f"inventory P&L   : ${tot['inv_pnl']:+,.2f}   (adverse selection + settling leftovers)")
    print(f"TOTAL P&L       : ${tot['total']:+,.2f}   (${tot['total']/n:+.2f} per market, "
          f"{tot['total']/max(tot['volume'],1)*100:+.2f}c per contract)")
    print(f"profitable markets: {winners}/{n} ({winners/n*100:.0f}%)")
    print(f"best  {best['ticker']}: ${best['total']:+.2f}")
    print(f"worst {worst['ticker']}: ${worst['total']:+.2f}  "
          f"(max inventory {worst['max_inv']:.0f})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", default="KXBTC15M", help="Series to simulate. Default KXBTC15M.")
    ap.add_argument("--max", type=int, default=100, help="Settled markets to scan. Default 100.")
    ap.add_argument("--quote", choices=["touch", "offset"], default="touch",
                    help="touch = join best bid/ask; offset = mid +/- --half-spread.")
    ap.add_argument("--half-spread", type=float, default=0.02,
                    help="[offset] distance from mid for each quote. Default 0.02.")
    ap.add_argument("--size", type=float, default=10, help="Contracts per side per minute. Default 10.")
    ap.add_argument("--max-inv", type=float, default=50,
                    help="Stop quoting the side that would grow |inventory| past this. Default 50.")
    ap.add_argument("--quote-lo", type=float, default=0.05,
                    help="Only quote while mid >= this (avoid near-resolved prices). Default 0.05.")
    ap.add_argument("--quote-hi", type=float, default=0.95,
                    help="Only quote while mid <= this. Default 0.95.")
    ap.add_argument("--stop-before-min", type=float, default=0,
                    help="Stop quoting this many minutes before close. Default 0.")
    ap.add_argument("--window-hours", type=float, default=0,
                    help="Only simulate the final N hours before close (e.g. 4 for sports). 0 = all.")
    ap.add_argument("--max-trade-pages", type=int, default=80,
                    help="Cap on 1000-trade pages fetched per market; markets over it are skipped.")
    ap.add_argument("--compare", action="store_true",
                    help="Run four variants on the same data: touch / touch+stop early / "
                         "offset / offset+stop early.")
    ap.add_argument("--compare-stop-min", type=float, default=3,
                    help="[compare] minutes before close to stop quoting. Default 3.")
    return run(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
