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
| `kalshi_sweep.py` | Runs the backtest across **all series at once** and ranks them by net edge. |
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

## Multi-series sweep findings (the honest bottom line)

Swept ~60,000 settled markets and deep-dived the candidates. Two clear groups:

**1. Auto-generated parlays (`KXMVE...`) — the bulk of skewed favorites, but untradeable for this.**
Their settled records are degenerate: open and close times collapse to a ~5-second
settlement instant, many with 0 volume and no candlestick history. You cannot
reconstruct an honest entry price, so the tempting biased "+1% edge" is unverifiable.
Skip them.

**2. Daily weather (`KXHIGH*`) — genuinely tradeable, and the favorite edge is real but tiny.**
Unbiased (candlestick) backtest, 99¢ bucket, net of fees on 100-lots:

| Series | N @99¢ | Net EV/contract | Losses in sample |
|--------|--------|-----------------|------------------|
| KXHIGHNY | 348 | +0.87¢ | 0 |
| KXHIGHCHI | 386 | +0.86¢ | 0 |
| KXHIGHMIA | 392 | +0.85¢ | 0 |
| KXHIGHLAX | 393 | +0.85¢ | 0 |
| KXHIGHDEN | 382 | +0.85¢ | 0 |

Consistent across cities and across lead times (6h/12h/18h/24h before close): the
99¢ favorite won **100%** of the time → a small, *real* structural edge (the price
can't exceed 99¢ while open, but the true probability of these extreme weather
thresholds is ~99.9%). **It is positive after fees: ~+0.9¢ per contract.**

### The loss audit overturns even that — the bot would have LOST money

The +0.9¢ figure prices each market at ONE calm moment (6h before close). But the
real bot buys *whenever* it sees 99¢. `kalshi_loss_audit.py` asks the honest
question: over each market's **full candlestick life**, did the side that
ultimately lost ever trade at ≥99¢? If yes, the bot would have bought it and eaten
~−99¢. Result across all 7 weather cities (~2 months, all the API serves):

| | |
|---|---|
| Markets audited | 2,774 |
| Favorite touched ≥99¢ in | 2,774 (all of them) |
| **Times the 99¢ favorite LOST** | **60** |
| Empirical upset rate at 99¢ | **2.16% — 1 loss per ~46 bets** |

Buying 1 contract in each: winners make ~+1¢ × 2,714 = +$27, losers cost ~−99¢ ×
60 = **−$59**. **Net ≈ −$32 over two months — about −1.2¢ per contract.** The "edge"
was an illusion of measuring price at a single benign instant; a bot trading every
99¢ spike is **net negative**. Worse, losses *cluster* on volatile weather days
(several threshold markets spike to 99¢ on the wrong side at once), so the bad days
are correlated — exactly the wrong risk shape.

**So: would the bot ever have lost? Yes — 60 times in two months, and the losses
outweigh all the penny wins combined.**

### The fix: only buy if it HELD 99¢ for >2 hours

If the killers are brief spikes, require the favorite to *sit* at ≥99¢ before
buying. `kalshi_hold_audit.py` measures the longest consecutive span (hourly,
volume-backed candles, conservative: a single dip breaks the run) the favorite
held ≥99¢, and only "enters" markets where that span exceeds `--min-hold-hours`.

At **>2h hold**, across all 7 weather cities (~2 months):

| | |
|---|---|
| Markets audited | 2,776 |
| Qualifying entries (held ≥99¢ >2h) | 1,706 |
| **Losses** | **0** |
| Crude P&L (1 contract each) | **+$17** |

**Every one of the 60 losses was a transient spike** that never held 99¢ for two
hours. Filtering them out flips the strategy from −$32 to +$17 in-sample. The
favorite-longshot edge appears to be *real* — but only on sustained quotes, not
every 99¢ blip.

**The honest caveats that remain:**
- **Two months is not proof.** 0 losses in 1,706 bounds the true upset rate at
  roughly ≤0.2% (rule of 3), no lower. Tail events are rare; a 2-month window may
  simply not contain the freak weather day that breaks a sustained 99¢.
- **Correlated blowups.** When a sustained favorite finally breaks, it may take
  several threshold markets the same day with it. One such day could erase months
  of pennies. We haven't seen one yet — that's not the same as it can't happen.
- **Thin capacity.** The edge exists because few trade the last cent, so there's
  little size to buy at 99¢. It doesn't scale.
- **Still ~1¢/contract.** A real but small edge, now with rare-but-catastrophic
  tail risk instead of frequent losses.

So: a credible micro-edge on *sustained* 99¢ favorites, validated in-sample, with
tail risk that only a longer history (or live logging) can price. Run
`kalshi_hold_audit.py --min-hold-hours 2` to reproduce; widen the window with
`kalshi_logger.py` going forward.

### Why even the sustained version is not a money printer
- **Tail risk dominates.** The edge is +0.9¢; one loss is **−99¢** — a single upset
  erases ~110 winning trades. The sample has zero losses *yet*; the math guarantees
  one eventually. You're paid 0.9¢ to carry 99¢ of tail risk (tiny Kelly fraction).
- **No capacity.** The edge exists *because* nobody bothers with the last cent —
  which means there's almost no size to buy at 99¢. Scale up and you eat the book /
  move the price and the edge vanishes. The backtest assumes unlimited fill at mid;
  reality caps you hard.
- **Capital lockup.** ~0.9% per resolution on fully-locked collateral, only on the
  thin size you can actually get.

So: a genuine micro-edge exists in the liquid daily series, but its risk/reward is
exactly why the price sits at 99 — small scalable profit, rare catastrophic loss.
Real, measurable, and not something to bet the farm on. Use `kalshi_logger.py` to
keep accruing live, unbiased resolutions and watch for the first loss.

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
