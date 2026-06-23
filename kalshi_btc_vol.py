#!/usr/bin/env python3
"""
BTC 15-minute volatility + long-strangle analysis for Kalshi's KXBTC15M market.

The idea being tested
---------------------
"Buy BOTH a higher strike and a lower strike at ~40c each (total 80c). If one
always wins we make money." That is NOT free money: the two 40c legs leave a
MIDDLE gap (~20% priced) where BTC ends up and BOTH legs lose. It's a long
strangle -- a bet that BTC moves MORE than the market priced in.

  win  (BTC exits the band): +$1 - cost
  lose (BTC stays in middle): -cost
  EV per pair = P(exit) - 2p - fees       (p = price per leg)

At fair prices P(exit) = 2p, so EV = -fees. You only profit if REALIZED vol >
the IMPLIED vol the market used to set the strikes. This script measures BTC's
realized 15-min vol and backtests the strangle, pricing each window's strikes
off the PRIOR day's realized vol (no look-ahead) and scoring it on the actual
move -- across several leg-price thresholds.

Data: Coinbase 15-min candles (public). Kalshi market: KXBTC15M.
"""

from __future__ import annotations

import argparse
import math
import statistics
import time
import urllib.request
import json

import kalshi_common as kc

CB = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
SECS = 900  # 15 minutes


def _norm_sf_inv(p: float) -> float:
    """Inverse survival function of the standard normal: z with P(Z> z)=p."""
    # Acklam's rational approximation of the normal quantile.
    q = 1.0 - p  # we want Phi^{-1}(1-p)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if q < plow:
        x = math.sqrt(-2 * math.log(q))
        return (((((c[0]*x+c[1])*x+c[2])*x+c[3])*x+c[4])*x+c[5]) / \
               ((((d[0]*x+d[1])*x+d[2])*x+d[3])*x+1)
    if q <= phigh:
        x = q - 0.5
        r = x*x
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*x / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    x = math.sqrt(-2 * math.log(1 - q))
    return -(((((c[0]*x+c[1])*x+c[2])*x+c[3])*x+c[4])*x+c[5]) / \
            ((((d[0]*x+d[1])*x+d[2])*x+d[3])*x+1)


def fetch_btc_15m(days: int) -> list[tuple[int, float, float]]:
    """Return [(ts, open, close), ...] of 15-min candles, oldest first."""
    end = int(time.time())
    start_floor = end - days * 86400
    out = {}
    cur = end
    while cur > start_floor:
        s = cur - 300 * SECS
        url = f"{CB}?granularity={SECS}&start={s}&end={cur}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research"})
            data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as e:  # noqa: BLE001
            print(f"  fetch warn: {e}")
            break
        if not data:
            break
        for row in data:  # [time, low, high, open, close, volume]
            out[int(row[0])] = (float(row[3]), float(row[4]))
        cur = s
        time.sleep(0.25)
    bars = [(t, o, c) for t, (o, c) in sorted(out.items())]
    return bars


def realized_stats(bars):
    rets = []
    for i in range(1, len(bars)):
        o = bars[i][1]
        c = bars[i][2]
        if o > 0:
            rets.append(math.log(c / o))  # intra-window log return (open->close)
    absmoves = sorted(abs(r) for r in rets)
    sd = statistics.pstdev(rets)
    n = len(rets)

    def pct(p):
        return absmoves[min(n - 1, int(p * n))]

    mean = statistics.fmean(rets)
    kurt = (sum((r - mean) ** 4 for r in rets) / n) / (sd ** 4) if sd > 0 else 0
    print(f"\n=== BTC realized 15-min volatility ({n} windows, "
          f"~{n*15/60/24:.1f} days) ===")
    print(f"per-15min move (1 sigma): {sd*100:.3f}%   "
          f"annualized: {sd*math.sqrt(4*24*365)*100:.0f}%")
    print(f"median |move|: {pct(0.5)*100:.3f}%   "
          f"90th pct: {pct(0.9)*100:.3f}%   "
          f"99th pct: {pct(0.99)*100:.3f}%   max: {absmoves[-1]*100:.2f}%")
    print(f"kurtosis: {kurt:.1f} (normal=3; higher = fatter tails / more big moves)")
    return rets


def backtest_strangle(bars, leg_prices, lookback, fee_coef):
    """
    For each window, price symmetric strikes off the prior `lookback` windows'
    realized vol (Gaussian), buy both legs at price p, score on the actual move.
    """
    rets = [math.log(bars[i][2] / bars[i][1]) for i in range(1, len(bars))
            if bars[i][1] > 0]
    print(f"\n=== Strangle backtest (strikes priced off prior {lookback} windows "
          f"= {lookback*15/60:.1f}h realized vol) ===")
    print(f"{'leg price':>9} {'strike dist':>11} {'implied exit':>12} "
          f"{'actual exit':>11} {'edge':>7} {'net EV/pair':>11} {'total':>9}")
    print("-" * 74)
    for p in leg_prices:
        z = _norm_sf_inv(p)            # strike at z*sigma so each tail ~ p
        # per-contract fee in size for a p-priced leg, x2 legs
        fee = 2 * (fee_coef * p * (1 - p))
        wins = trades = 0
        pnl = 0.0
        dist_acc = 0.0
        for i in range(lookback, len(rets)):
            sigma = statistics.pstdev(rets[i - lookback:i])
            if sigma <= 0:
                continue
            d = z * sigma                      # band half-width (log-return units)
            exited = abs(rets[i]) > d
            trades += 1
            wins += 1 if exited else 0
            pnl += (1.0 if exited else 0.0) - 2 * p - fee
            dist_acc += d
        if trades == 0:
            continue
        actual = wins / trades
        implied = 2 * p
        print(f"{p*100:>8.0f}c {dist_acc/trades*100:>10.3f}% "
              f"{implied*100:>11.1f}% {actual*100:>10.1f}% "
              f"{(actual-implied)*100:>+6.1f}% {pnl/trades*100:>+10.2f}c "
              f"{pnl*100:>+8.0f}c")
    print("\nedge = actual exit - implied exit. Positive AND surviving fees in the\n"
          "'net EV/pair' column = realized vol beat the priced-in vol. Negative =\n"
          "the market priced the move about right (or you overpaid the spread/fees).")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14, help="Days of 15-min history. Default 14.")
    ap.add_argument("--lookback", type=int, default=96,
                    help="Windows of realized vol used to price strikes (96=24h). Default 96.")
    ap.add_argument("--legs", type=str, default="0.30,0.35,0.40,0.45",
                    help="Comma-separated leg prices to test. Default 0.30,0.35,0.40,0.45.")
    ap.add_argument("--fee-coef", type=float, default=kc.DEFAULT_FEE_COEF)
    args = ap.parse_args()

    print(f"fetching ~{args.days}d of BTC 15-min candles from Coinbase...")
    bars = fetch_btc_15m(args.days)
    if len(bars) < args.lookback + 10:
        print(f"only {len(bars)} bars fetched; need more. try fewer --lookback.")
        return 1
    realized_stats(bars)
    legs = [float(x) for x in args.legs.split(",")]
    backtest_strangle(bars, legs, args.lookback, args.fee_coef)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
