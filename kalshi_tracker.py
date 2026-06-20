#!/usr/bin/env python3
"""
Kalshi skewed-market tracker.

Scans all open Kalshi markets and surfaces the ones priced at an extreme
(e.g. >= 99c / <= 1c). Uses only Python's standard library so it runs with
zero install.

IMPORTANT REALITY CHECK
-----------------------
A market trading at 99c is the market's estimate that the YES side happens
~99% of the time. By definition, roughly 1 in 100 of those resolves the OTHER
way. There is no way to make a 99c side "always" win -- if that were possible
the price would not be 99. Buying the 99c side nets you +1c when you're right
and -99c when you're wrong, which is break-even *before* fees and negative
after them. This tool shows you that math next to every market so the
"picking up pennies in front of a steamroller" risk is explicit.

Data source: https://external-api.kalshi.com/trade-api/v2/markets (public, no auth)
Docs: https://docs.kalshi.com/
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

API_BASE = "https://external-api.kalshi.com/trade-api/v2"
USER_AGENT = "kalshi-skew-tracker/1.0 (+https://docs.kalshi.com)"


# --------------------------------------------------------------------------- #
# Data fetching
# --------------------------------------------------------------------------- #
def _get(path: str, params: dict) -> dict:
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_open_markets(max_markets: int | None = None) -> list[dict]:
    """Page through every open market via the cursor."""
    markets: list[dict] = []
    cursor = ""
    while True:
        params = {"status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets", params)
        batch = data.get("markets", [])
        markets.extend(batch)
        cursor = data.get("cursor") or ""
        if max_markets and len(markets) >= max_markets:
            return markets[:max_markets]
        if not cursor or not batch:
            return markets


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _f(market: dict, key: str) -> float:
    """Parse a Kalshi *_dollars / *_fp string field into a float."""
    val = market.get(key)
    if val in (None, ""):
        return 0.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class SkewedMarket:
    ticker: str
    title: str
    yes_price: float          # representative YES price in dollars (0..1)
    yes_bid: float
    yes_ask: float
    volume: float
    close_time: str
    favored_side: str         # "YES" or "NO"
    favored_price: float      # price of the heavily-favored side (>= threshold)

    # --- the math that matters ---
    @property
    def implied_prob(self) -> float:
        return self.favored_price

    @property
    def profit_if_win(self) -> float:
        """$ profit per contract if the favored side is correct."""
        return round(1.0 - self.favored_price, 4)

    @property
    def loss_if_wrong(self) -> float:
        """$ loss per contract if the favored side is wrong."""
        return round(self.favored_price, 4)

    @property
    def breakeven_winrate(self) -> float:
        """Win rate needed just to break even (gross, no fees)."""
        return self.favored_price

    @property
    def est_fee(self) -> float:
        """Kalshi trading fee per contract ~= ceil(0.07 * P * (1-P)) in cents."""
        p = self.favored_price
        return math.ceil(0.07 * p * (1.0 - p) * 100) / 100.0

    @property
    def net_ev_at_implied(self) -> float:
        """
        Expected value per contract if the true probability equals the market
        price, minus fees. Gross EV at fair price is exactly 0, so this is
        essentially the fee drag -- i.e. how negative the edge is.
        """
        p = self.favored_price
        gross = p * (1 - p) + (1 - p) * (-p)  # == 0 at fair price
        return round(gross - self.est_fee, 4)


def find_skewed(markets: list[dict], threshold: float, min_volume: float) -> list[SkewedMarket]:
    out: list[SkewedMarket] = []
    for m in markets:
        yes_bid = _f(m, "yes_bid_dollars")
        yes_ask = _f(m, "yes_ask_dollars")
        last = _f(m, "last_price_dollars")
        volume = _f(m, "volume_fp")

        # representative YES price: prefer mid of the two-sided quote, else last
        if yes_bid > 0 and yes_ask > 0:
            yes_price = (yes_bid + yes_ask) / 2.0
        else:
            yes_price = last
        if yes_price <= 0:
            continue
        if volume < min_volume:
            continue

        no_price = 1.0 - yes_price
        if yes_price >= threshold:
            favored_side, favored_price = "YES", yes_price
        elif no_price >= threshold:
            favored_side, favored_price = "NO", no_price
        else:
            continue

        out.append(
            SkewedMarket(
                ticker=m.get("ticker", "?"),
                title=m.get("title", "")[:70],
                yes_price=yes_price,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                volume=volume,
                close_time=m.get("close_time", ""),
                favored_side=favored_side,
                favored_price=favored_price,
            )
        )
    out.sort(key=lambda s: (-s.favored_price, -s.volume))
    return out


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def print_table(skewed: list[SkewedMarket]) -> None:
    if not skewed:
        print("No markets matched the skew threshold right now.")
        return

    print(
        f"\nFound {len(skewed)} heavily-skewed open market(s).\n"
        "Columns: favored side & price | profit if right | loss if wrong | "
        "breakeven win% | est fee | net EV/contract at fair price\n"
    )
    header = f"{'TICKER':<26} {'SIDE':<4} {'PRICE':>6} {'WIN+':>6} {'LOSE-':>6} {'BE%':>6} {'FEE':>5} {'EV':>7}  TITLE"
    print(header)
    print("-" * len(header))
    for s in skewed:
        print(
            f"{s.ticker:<26} {s.favored_side:<4} "
            f"{s.favored_price*100:>5.1f}c "
            f"{s.profit_if_win*100:>5.1f}c "
            f"{s.loss_if_wrong*100:>5.1f}c "
            f"{s.breakeven_winrate*100:>5.1f}% "
            f"{s.est_fee*100:>4.1f}c "
            f"{s.net_ev_at_implied*100:>+6.2f}c  "
            f"{s.title}"
        )

    print(
        "\nReality check: 'net EV/contract at fair price' is what you'd expect to "
        "make per contract if the\nmarket's price is the true probability. It is "
        "negative because the fee eats the entire ~1c edge.\nThe 99c side is NOT a "
        "guaranteed win -- ~1 in 100 of these resolves the other way and costs you 99c."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Track heavily-skewed Kalshi markets.")
    ap.add_argument("--threshold", type=float, default=0.99,
                    help="Min price (0..1) of the favored side. Default 0.99 (= 99c/1c).")
    ap.add_argument("--min-volume", type=float, default=0.0,
                    help="Ignore markets with volume below this. Default 0.")
    ap.add_argument("--max-markets", type=int, default=None,
                    help="Cap how many markets to scan (for quick tests).")
    ap.add_argument("--watch", type=float, default=0.0, metavar="SECONDS",
                    help="Re-scan every N seconds instead of running once.")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    args = ap.parse_args()

    def run_once() -> None:
        markets = fetch_all_open_markets(max_markets=args.max_markets)
        skewed = find_skewed(markets, args.threshold, args.min_volume)
        if args.json:
            print(json.dumps([s.__dict__ | {
                "implied_prob": s.implied_prob,
                "profit_if_win": s.profit_if_win,
                "loss_if_wrong": s.loss_if_wrong,
                "breakeven_winrate": s.breakeven_winrate,
                "est_fee": s.est_fee,
                "net_ev_at_implied": s.net_ev_at_implied,
            } for s in skewed], indent=2))
        else:
            print(f"Scanned {len(markets)} open markets at {time.strftime('%Y-%m-%d %H:%M:%S')}.")
            print_table(skewed)

    if args.watch > 0:
        try:
            while True:
                run_once()
                print(f"\n(next scan in {args.watch:.0f}s -- Ctrl+C to stop)\n")
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        run_once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
