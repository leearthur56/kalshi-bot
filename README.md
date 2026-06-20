# kalshi-bot — skewed market tracker

Scans **open** [Kalshi](https://kalshi.com) prediction markets and surfaces the
ones priced at an extreme (default **≥ 99¢ / ≤ 1¢**), with the real
expected-value math attached.

Data comes from Kalshi's public market-data endpoint
(`https://external-api.kalshi.com/trade-api/v2/markets`), which needs **no API
key**. Docs: <https://docs.kalshi.com/>.

## Run it

Pure standard library — no `pip install` needed.

```bash
python kalshi_tracker.py                 # all open markets, 99c+ skew
python kalshi_tracker.py --threshold 0.95 --min-volume 100
python kalshi_tracker.py --watch 60      # re-scan every 60 seconds
python kalshi_tracker.py --json          # machine-readable output
```

| Flag | Meaning | Default |
|------|---------|---------|
| `--threshold` | Min price (0–1) of the favored side | `0.99` |
| `--min-volume` | Skip markets below this volume | `0` |
| `--max-markets` | Cap markets scanned (quick tests) | none (all) |
| `--watch N` | Re-scan every N seconds | off |
| `--json` | Emit JSON instead of a table | off |

## ⚠️ Read this before you trade on it

You asked for markets that **always close in favor of the 99 side.** That does
not exist, and the tool proves why rather than pretending otherwise:

- A **99¢** price *is* the market saying "this happens ~99% of the time."
  By construction, **~1 in 100 of these resolves the other way.**
- Buy the 99¢ side and you win **+1¢** when right, lose **−99¢** when wrong.
  Your break-even win rate is exactly 99% — you need to be right *as often as
  the market already expects* just to not lose money.
- **Fees: smaller than they look, if you buy in size.** Kalshi's fee is
  `round_up(0.07 × C × P × (1−P))` **per order** (C = contracts), rounded up to
  the next cent *once*. So 1 contract @ 99¢ pays the 1¢ floor, but **100
  contracts @ 99¢ pay only 7¢ total — 0.07¢ each.** Buying in size amortizes the
  round-up floor. Use `--order-size` to model this.

So the real question isn't fees — it's whether the favorite wins **more** than
its price implies (favorite-longshot bias). If it wins even ~0.1% more than
priced, the thin in-size fee can leave a small **positive** edge. That is an
empirical question — measure it with the backtest and logger; don't assume it.

This is still the *"picking up pennies in front of a steamroller"* shape: many
tiny wins and rare large losses, so tail risk and sample size matter more than
anything. The tracker is a **monitoring/research tool**, not a guarantee.

## The tools

| Script | What it does |
|--------|--------------|
| `kalshi_tracker.py` | Live scan of open markets for extreme prices, with EV math. |
| `kalshi_backtest.py` | Backtests "buy the favorite" on **settled** markets — actual win rate vs implied, net of fees, bucketed by entry price. |
| `kalshi_logger.py` | Logs current prices now and resolutions later, building an **unbiased** forward dataset (`snapshot` / `settle` / `report`). |

```bash
# Does the favorite win more than its price implies? (last_price = biased/optimistic)
python kalshi_backtest.py --series KXHIGHNY --min-price 0.90 --min-volume 500

# Honest version: entry price 6h before close, no peeking at the outcome (slower)
python kalshi_backtest.py --series KXHIGHNY --max 250 --lead-hours 6

# Build your own unbiased dataset over time (run on a schedule)
python kalshi_logger.py snapshot --threshold 0.90 --min-volume 500
python kalshi_logger.py settle      # run after markets close
python kalshi_logger.py report
```

## What the backtest actually found (NYC-weather series, sample run)

Unbiased entry price 6h before close, **100-contract orders**:

| Entry price | N | Implied | Actual win | Edge | Net EV/contract |
|-------------|---|---------|-----------|------|-----------------|
| 99–100¢ | 217 | 99.06% | 100.00% | **+0.94%** | **+0.87¢** |
| 98–99¢ | 10 | 98.35% | 100% | +1.65% | +1.53¢ |
| 90–95¢ | 8 | 92.06% | 75% | **−17%** | **−17.6¢** |

Your hypothesis held up in this sample: favorites won slightly *more* than
priced (the tick-floor / longshot effect is real), and with the **correct
in-size fee (~0.07¢/contract, not 1¢)** that edge survives — total **+$1.11**
across the sample. Note the 90–95¢ row though: two upsets there lost 17¢ *each*,
the steamroller in miniature.

**Do not trust this yet.** It is one small sample (217 markets) of one unusually
predictable series (daily NYC high temp) in which zero 99¢ favorites happened to
lose. The whole edge rests on the favorite winning ~0.07%+ more than priced — a
thin margin that a handful of upsets would erase. It also assumes fills at mid
(real fills are worse) and ignores capital lockup. Run it across many series and
a much bigger `--max`, or accumulate live data with the logger, before believing
the edge is real.

## Output columns

```
SIDE   the heavily-favored side (YES or NO)
PRICE  price of that side, in cents
WIN+   profit per contract if that side is correct
LOSE-  loss per contract if it is wrong
BE%    win rate needed just to break even (= PRICE)
FEE    estimated Kalshi fee per contract
EV     expected value per contract at the market's own implied probability,
       after fees (negative = the house edge against you)
```
