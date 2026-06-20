#!/usr/bin/env python3
"""
Paper-trading bot for the "sustained 99c favorite" weather strategy. FAKE MONEY.

Strategy
--------
For each daily high-temperature market, once the favorite side has HELD >= 99c
for longer than --min-hold-hours, buy $--stake of it. Hold to settlement. Win
pays the remaining ~1c per contract; a loss costs ~the full stake. No real
orders are ever placed -- this only tracks what *would* have happened.

Modes
-----
  backsim   Replay all available settled history and simulate the portfolio now.
            Gives concrete numbers immediately.
  poll      Live step: snapshot open markets, advance the >1h hold timers, open
            new paper positions, settle any resolved ones. Run it on a schedule
            (e.g. every 15-30 min); state persists in data/paper_state.json.
  report    Print the current paper portfolio (open positions, closed P&L, ROI).
  reset     Wipe the paper state and start fresh.

Examples
--------
  python kalshi_paper_bot.py backsim --stake 100
  python kalshi_paper_bot.py poll    --stake 100      # run repeatedly
  python kalshi_paper_bot.py report
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import kalshi_common as kc
from kalshi_hold_audit import held_candles, WEATHER

STATE_DIR = "data"
STATE_FILE = os.path.join(STATE_DIR, "paper_state.json")
HOUR = 3600


# --------------------------------------------------------------------------- #
# Position economics (shared by backsim and live)
# --------------------------------------------------------------------------- #
def size_position(entry_price: float, stake: float, coef: float):
    """Return (contracts, cost, fee) for spending ~`stake` dollars at entry_price."""
    contracts = int(stake // entry_price)
    if contracts <= 0:
        return 0, 0.0, 0.0
    cost = contracts * entry_price
    fee = kc.fee_for_order(entry_price, contracts, coef)
    return contracts, round(cost, 4), round(fee, 4)


def settle_pnl(contracts: int, cost: float, fee: float, won: bool) -> float:
    """Realized P&L when the market resolves."""
    payout = contracts * 1.0 if won else 0.0
    return round(payout - cost - fee, 4)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state(start_cash: float) -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"start_cash": start_cash, "cash": start_cash,
            "hold_since": {}, "open": {}, "closed": []}


def save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str | None):
    if not s:
        return None
    try:
        return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def log_line(msg: str) -> None:
    """Print and append to data/poll.log (so a no-console pythonw run still logs)."""
    print(msg)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "poll.log"), "a") as f:
            f.write(msg + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# backsim: historical portfolio over all available settled markets
# --------------------------------------------------------------------------- #
def backsim(args) -> int:
    thr, hold, stake, coef = args.threshold, args.min_hold_hours, args.stake, args.fee_coef
    entry, win_min = args.entry, args.close_window_min
    trades = []
    realized = 0.0
    wins = losses = 0

    for s in WEATHER:
      try:
        for m in kc.paginate_markets("settled", series_ticker=s, max_markets=args.max):
            result = m.get("result")
            if result not in ("yes", "no"):
                continue

            # candidate (side, won, trigger-info) list for this market
            candidates = []
            if entry == "hold":
                held = held_candles(m, thr)
                if held is None:
                    continue
                yes_hours, no_hours = held
                for side, hours, won in (("YES", yes_hours, result == "yes"),
                                         ("NO", no_hours, result == "no")):
                    if hours > hold:
                        candidates.append((side, won, f"held {hours:.1f}h"))
            else:  # near-close: favorite price in the final window before close
                cts = parse_ts(m.get("close_time"))
                if cts is None:
                    continue
                yp = kc.candlestick_price_at(m["ticker"], cts - int(win_min * 60))
                if yp is None or not (0 < yp < 1):
                    continue
                if yp >= thr:
                    candidates.append(("YES", result == "yes", f"~{win_min:g}m pre-close"))
                elif (1 - yp) >= thr:
                    candidates.append(("NO", result == "no", f"~{win_min:g}m pre-close"))

            for side, won, trig in candidates:
                contracts, cost, fee = size_position(thr, stake, coef)
                if contracts == 0:
                    continue
                pnl = settle_pnl(contracts, cost, fee, won)
                realized += pnl
                wins += 1 if won else 0
                losses += 0 if won else 1
                trades.append({"ticker": m["ticker"], "side": side, "won": won,
                               "trigger": trig, "pnl": pnl})
      except RuntimeError as e:
        print(f"  (skipped {s}: {e})", file=sys.stderr)
        continue

    n = len(trades)
    invested = n * stake
    label = (f"held > {hold:g}h" if entry == "hold"
             else f"~{win_min:g} min before close")
    print(f"\n=== PAPER BACKSIM: ${stake:g} per {thr*100:.0f}c favorite "
          f"({label}) ===")
    print(f"Cities: {len(WEATHER)} | trades: {n} | wins: {wins} | losses: {losses}")
    if n:
        print(f"Total staked (sum of entries): ${invested:,.0f}")
        print(f"Realized P&L: ${realized:+,.2f}   "
              f"(return on staked: {realized/invested*100:+.2f}%)")
        avg_win = sum(t["pnl"] for t in trades if t["won"]) / max(wins, 1)
        print(f"Avg win: ${avg_win:+.2f}   "
              f"Each loss costs ~ the full ${stake:g} stake.")
    losers = [t for t in trades if not t["won"]]
    if losers:
        print(f"\nLosing paper trades ({len(losers)}):")
        for t in losers:
            print(f"  {t['ticker']:<26} {t['side']:<4} {t['trigger']:<16} "
                  f"P&L ${t['pnl']:+.2f}")
    else:
        print("\nNo losing trades in the available history (but it's only ~2 months;\n"
              "one freak weather day could take several positions at once).")
    return 0


# --------------------------------------------------------------------------- #
# live paper trading
# --------------------------------------------------------------------------- #
def fav_quote(m: dict, thr: float):
    """Return (side, fav_price_mid, fav_ask) for the favored side, or None."""
    yb, ya = kc.fnum(m, "yes_bid_dollars"), kc.fnum(m, "yes_ask_dollars")
    last = kc.fnum(m, "last_price_dollars")
    yes_mid = (yb + ya) / 2 if (yb > 0 and ya > 0) else last
    if yes_mid <= 0:
        return None
    no_mid = 1 - yes_mid
    if yes_mid >= thr:
        return "YES", yes_mid, (ya if ya > 0 else yes_mid)
    if no_mid >= thr:
        no_ask = 1 - yb if yb > 0 else no_mid     # buy NO = sell YES at yes_bid
        return "NO", no_mid, no_ask
    return None


def poll(args) -> int:
    thr, hold, stake, coef = args.threshold, args.min_hold_hours, args.stake, args.fee_coef
    entry, win_min = args.entry, args.close_window_min
    state = load_state(args.start_cash)
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    opened = settled = 0

    # 1) settle any open positions whose market has resolved
    for key, pos in list(state["open"].items()):
        try:
            data = kc.get(f"/markets/{pos['ticker']}", {})
        except RuntimeError:
            continue
        mk = data.get("market", {})
        if mk.get("status") == "settled" and mk.get("result") in ("yes", "no"):
            won = (mk["result"] == "yes" and pos["side"] == "YES") or \
                  (mk["result"] == "no" and pos["side"] == "NO")
            pnl = settle_pnl(pos["contracts"], pos["cost"], pos["fee"], won)
            state["cash"] += pos["contracts"] * (1.0 if won else 0.0)
            pos.update(status="win" if won else "loss", result=mk["result"],
                       pnl=pnl, closed_at=now_iso())
            state["closed"].append(pos)
            del state["open"][key]
            settled += 1

    def open_position(tkr, s, side, ask, trigger):
        key = f"{tkr}:{side}"
        if key in state["open"] or any(c["ticker"] == tkr for c in state["closed"]):
            return 0
        if not (0 < ask < 1.0):
            return 0
        contracts, cost, fee = size_position(ask, stake, coef)
        if contracts == 0 or cost + fee > state["cash"]:
            return 0
        state["cash"] -= (cost + fee)
        state["open"][key] = {
            "ticker": tkr, "series": s, "side": side, "entry_price": ask,
            "contracts": contracts, "cost": cost, "fee": fee, "stake": stake,
            "trigger": trigger, "opened_at": now_iso(), "status": "open"}
        return 1

    # 2) scan live open markets and open new positions per the entry rule
    seen_fav = set()
    for s in WEATHER:
        for m in kc.paginate_markets("open", series_ticker=s, max_markets=args.max):
            tkr = m.get("ticker")
            q = fav_quote(m, thr)
            if q is None:
                state["hold_since"].pop(tkr, None)         # dropped below thr: reset
                continue
            side, mid, ask = q

            if entry == "close":
                cts = parse_ts(m.get("close_time"))
                if cts is None:
                    continue
                mins_to_close = (cts - now) / 60.0
                if 0 < mins_to_close <= win_min:
                    opened += open_position(tkr, s, side, ask,
                                            f"{mins_to_close:.1f}m pre-close")
            else:  # hold >= min_hold_hours
                seen_fav.add(tkr)
                since = state["hold_since"].setdefault(tkr, now)
                if now - since > hold * HOUR:
                    opened += open_position(tkr, s, side, ask,
                                            f"held {(now-since)/HOUR:.1f}h")

    if entry == "hold":  # forget timers for markets no longer favored/open
        state["hold_since"] = {k: v for k, v in state["hold_since"].items() if k in seen_fav}
    save_state(state)
    log_line(f"[{now_iso()}] poll({entry}): opened {opened}, settled {settled}, "
             f"open {len(state['open'])}, cash ${state['cash']:,.2f}")
    return 0


def report(args) -> int:
    if not os.path.exists(STATE_FILE):
        print("No paper state yet. Run 'poll' or 'backsim' first.")
        return 0
    state = load_state(args.start_cash)
    realized = sum(c["pnl"] for c in state["closed"])
    wins = sum(1 for c in state["closed"] if c["status"] == "win")
    losses = sum(1 for c in state["closed"] if c["status"] == "loss")
    invested_open = sum(p["cost"] + p["fee"] for p in state["open"].values())

    print("\n=== PAPER PORTFOLIO (fake money) ===")
    print(f"Start cash: ${state['start_cash']:,.2f}   Cash now: ${state['cash']:,.2f}")
    print(f"Open positions: {len(state['open'])}  (capital at risk ${invested_open:,.2f})")
    print(f"Closed: {wins} wins / {losses} losses   Realized P&L: ${realized:+,.2f}")
    if state["open"]:
        print("\nOpen:")
        for p in state["open"].values():
            print(f"  {p['ticker']:<26} {p['side']:<4} {p['contracts']:>4} @ "
                  f"{p['entry_price']*100:.0f}c  (${p['stake']:g}, {p.get('trigger','')})")
    if losses:
        print("\nClosed losers:")
        for c in state["closed"]:
            if c["status"] == "loss":
                print(f"  {c['ticker']:<26} {c['side']:<4} P&L ${c['pnl']:+.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["backsim", "poll", "report", "reset"])
    ap.add_argument("--stake", type=float, default=100.0, help="Dollars per contract. Default 100.")
    ap.add_argument("--threshold", type=float, default=0.99, help="Favorite price. Default 0.99.")
    ap.add_argument("--entry", choices=["close", "hold"], default="close",
                    help="close = buy in the final minutes before close (default); "
                         "hold = buy once favorite has held >= threshold for >min-hold-hours.")
    ap.add_argument("--close-window-min", type=float, default=5.0,
                    help="[close mode] buy when within this many minutes of close. Default 5.")
    ap.add_argument("--min-hold-hours", type=float, default=1.0,
                    help="[hold mode] require favorite to hold >= threshold longer than this. Default 1.")
    ap.add_argument("--start-cash", type=float, default=100000.0,
                    help="Starting paper bankroll. Default 100000.")
    ap.add_argument("--fee-coef", type=float, default=kc.DEFAULT_FEE_COEF)
    ap.add_argument("--max", type=int, default=None, help="Cap markets per series.")
    args = ap.parse_args()

    if args.mode == "reset":
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
        print("Paper state reset.")
        return 0
    if args.mode == "backsim":
        return backsim(args)
    if args.mode == "poll":
        try:
            return poll(args)
        except Exception as e:  # noqa: BLE001 - log and exit cleanly under pythonw
            import traceback
            log_line(f"[{now_iso()}] poll ERROR: {e}")
            log_line(traceback.format_exc())
            return 1
    return report(args)


if __name__ == "__main__":
    sys.exit(main())
