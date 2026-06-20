"""
Shared Kalshi API helpers (standard library only).

Public market-data API, no key required.
Docs: https://docs.kalshi.com/
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request

API_BASE = "https://external-api.kalshi.com/trade-api/v2"
USER_AGENT = "kalshi-skew-tracker/1.0 (+https://docs.kalshi.com)"

# Kalshi's standard trading-fee coefficient: fee = ceil(coef * C * P * (1-P)).
# Most markets use 0.07; a few (e.g. S&P/Nasdaq ranges) use 0.035.
DEFAULT_FEE_COEF = 0.07


def get(path: str, params: dict | None = None, retries: int = 3) -> dict:
    """GET a JSON endpoint with light retry on transient errors."""
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - network layer, want to retry broadly
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {retries} tries: {last_err}")


def fnum(market: dict, key: str, default: float = 0.0) -> float:
    """Parse a Kalshi *_dollars / *_fp string field into a float."""
    val = market.get(key)
    if val in (None, ""):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def paginate_markets(status: str, *, series_ticker: str | None = None,
                     max_markets: int | None = None, page_limit: int = 1000):
    """Yield markets of a given status, paging through the cursor."""
    cursor = ""
    seen = 0
    while True:
        params = {"status": status, "limit": page_limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        data = get("/markets", params)
        batch = data.get("markets", [])
        for m in batch:
            yield m
            seen += 1
            if max_markets and seen >= max_markets:
                return
        cursor = data.get("cursor") or ""
        if not cursor or not batch:
            return


def series_of(ticker: str) -> str:
    """Series ticker is the substring before the first dash."""
    return ticker.split("-", 1)[0]


def yes_price(market: dict) -> float:
    """Representative YES price (0..1): mid of the live quote, else last trade."""
    bid = fnum(market, "yes_bid_dollars")
    ask = fnum(market, "yes_ask_dollars")
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return fnum(market, "last_price_dollars")


def fee_per_contract(price: float, coef: float = DEFAULT_FEE_COEF) -> float:
    """Kalshi fee per single contract at a given price (dollars)."""
    return math.ceil(coef * price * (1.0 - price) * 100) / 100.0


def candlestick_price_at(ticker: str, target_ts: int, *,
                         window_hours: int = 6) -> float | None:
    """
    Mid price (yes_bid/yes_ask close) at or just before `target_ts` (unix sec),
    reconstructed from hourly candlesticks. Returns None if no data.
    Unbiased entry price for backtests -- it does not peek at the outcome.
    """
    start = target_ts - window_hours * 3600
    end = target_ts + 3600
    try:
        data = get(f"/series/{series_of(ticker)}/markets/{ticker}/candlesticks",
                   {"start_ts": start, "end_ts": end, "period_interval": 60})
    except RuntimeError:
        return None
    best = None
    for c in data.get("candlesticks", []):
        ts = c.get("end_period_ts")
        if ts is None or ts > target_ts + 3600:
            continue
        bid = c.get("yes_bid", {}).get("close_dollars")
        ask = c.get("yes_ask", {}).get("close_dollars")
        try:
            bid_f, ask_f = float(bid), float(ask)
        except (TypeError, ValueError):
            continue
        if bid_f <= 0 and ask_f <= 0:
            mid = None
        elif bid_f <= 0 or ask_f <= 0:
            mid = max(bid_f, ask_f)
        else:
            mid = (bid_f + ask_f) / 2.0
        if mid is not None:
            best = mid  # keep latest valid candle <= target
    return best
