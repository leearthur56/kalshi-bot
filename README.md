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
- **Fees finish the job.** Kalshi's per-contract fee is about
  `ceil(0.07 × price × (1−price))`. At 99¢ that rounds up to **1¢** — larger
  than the entire 1¢ of upside. The tool's `EV` column shows this as a
  **negative** number on every skewed market.

This is the textbook *"picking up pennies in front of a steamroller"* trade:
many tiny wins, then one loss that erases all of them. The tracker is a
**monitoring/learning tool**, not a guaranteed-money strategy — there is no such
thing, and any tool claiming otherwise is wrong about how prices work.

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

Using an **unbiased** entry price 6h before close:

| Entry price | N | Implied | Actual win | Edge | Net EV/contract |
|-------------|---|---------|-----------|------|-----------------|
| 99–100¢ | 217 | 99.06% | 100.00% | **+0.94%** | **−0.06¢** |
| 98–99¢ | 10 | 98.35% | 100% | +1.65% | +0.65¢ |
| 90–95¢ | 8 | 92.06% | 75% | −17% | −18¢ |

Your hypothesis is **partly right**: favorites at 99¢ did win slightly *more*
than priced (+0.94% edge — the tick-floor/longshot effect is real and
measurable). **But** the 1¢ fee turns that into a small *negative* EV, and a
couple of upsets in the lower buckets blow a hole that dwarfs all the penny
wins. Total across the sample: **−$1.12**. Picking up pennies, meet steamroller.

Caveats: small sample, one series, fills assumed at mid (real fills are worse),
no capital-efficiency penalty for locking 99¢ to earn <1¢. Run it on more series
and a bigger `--max` before drawing conclusions — that's what the tools are for.

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
