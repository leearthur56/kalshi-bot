"""
Shared Kalshi API helpers (standard library only).

Public market-data API, no key required.
Docs: https://docs.kalshi.com/
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://external-api.kalshi.com/trade-api/v2"
USER_AGENT = "kalshi-skew-tracker/1.0 (+https://docs.kalshi.com)"

# Kalshi's standard trading-fee coefficient: fee = ceil(coef * C * P * (1-P)).
# Most markets use 0.07; a few (e.g. S&P/Nasdaq ranges) use 0.035.
DEFAULT_FEE_COEF = 0.07

# Client-side throttle: keep a minimum gap between requests so bursts don't trip
# Kalshi's rate limit (public cap is ~30/s; we stay well under to be safe).
MIN_INTERVAL = 0.12
_last_req = 0.0


def get(path: str, params: dict | None = None, retries: int = 6) -> dict:
    """GET a JSON endpoint, throttled, with backoff on transient errors / 429s."""
    global _last_req
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last_err: Exception | None = None
    for attempt in range(retries):
        wait = MIN_INTERVAL - (time.time() - _last_req)
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                _last_req = time.time()
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - network layer, want to retry broadly
            _last_req = time.time()
            last_err = e
            is_429 = isinstance(e, urllib.error.HTTPError) and e.code == 429
            time.sleep(min(8.0, (1.0 if is_429 else 0.5) * (2 ** attempt)))
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


def fee_for_order(price: float, contracts: int = 1,
                  coef: float = DEFAULT_FEE_COEF) -> float:
    """
    Total Kalshi fee for an ORDER, in dollars.

    fee = round_up( coef * C * P * (1-P) ), rounded up to the next cent ONCE
    for the whole order -- not per contract. So per-contract cost falls as the
    order grows (the 1-cent round-up floor gets amortized).
    """
    raw = coef * contracts * price * (1.0 - price)
    return math.ceil(raw * 100) / 100.0


def fee_per_contract(price: float, contracts: int = 1,
                     coef: float = DEFAULT_FEE_COEF) -> float:
    """Amortized fee per contract for an order of `contracts` at `price`."""
    return fee_for_order(price, contracts, coef) / contracts


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
