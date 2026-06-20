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
