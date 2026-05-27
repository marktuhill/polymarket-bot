# Paper trading the walk-forward tuned triplet

Daily-cadence bot for the three-leg portfolio: `EMACross/BTC` + `BBevent/LTC` +
`EMALong/XAU`. Each leg is vol-targeted at 10% annualised on a rolling 6-month
window; a leg is muted whenever its rolling 6-month Sharpe goes non-positive; a
kill switch trips and flattens everything if portfolio drawdown reaches -20%.

## What it does in one daily run

1. Reload BTC, LTC, and XAU daily history (Coin Metrics CSVs + the GitHub XAU
   mirror — same sources the backtest used).
2. Recompute each leg's current target position from the strategy logic.
3. Vol-scale each leg with its rolling 6-month vol, mute any leg whose rolling
   6-month Sharpe is `<= 0`, equal-weight the survivors.
4. Diff target positions against what the broker reports, send orders for the
   delta. No-ops below `$5` notional.
5. Record equity, check the kill switch, persist state to
   `data/live_state.json`.

## Run it

```
python scripts/paper_bot.py                 # daily run
python scripts/paper_bot.py --force         # re-run even if state shows today already done
python scripts/paper_bot.py --reset         # wipe state and start over
python scripts/paper_bot.py --slippage-bps 20 --commission-bps 15
```

The state file is plain JSON; open it any time to audit positions, equity
history, and kill-switch status.

## Schedule it

The bot is idempotent within a UTC date, so over-scheduling is safe.

**Linux / macOS cron** (runs daily at 00:30 UTC, after the day's data files
typically refresh):

```
30 0 * * * cd /path/to/polymarket-bot && /usr/bin/python3 scripts/paper_bot.py >> data/bot.log 2>&1
```

**Windows Task Scheduler**: create a daily task triggering
`python.exe scripts\paper_bot.py` from the repo root.

## Container note

This Linux container's egress allowlist blocks every exchange, so the bot can
only run paper here against historical data — useful for validating logic, not
for true forward paper trading. Run it on your local machine or a VPS for
genuine paper / live deployment.

## Going live on IC Markets MT5 (Windows)

All three legs trade as CFDs on a single IC Markets MT5 account — `BTCUSD`,
`LTCUSD`, `XAUUSD`. Raw Spread account is recommended for tighter spreads on
crypto.

### One-time setup

1. **Install MT5** and log into your IC Markets demo (or live) account.
2. **Enable the symbols.** Market Watch -> Show All, then right-click and add
   `BTCUSD`, `LTCUSD`, `XAUUSD` if they aren't already visible.
3. **Install the Python bridge** (Python 3.9-3.12 on Windows):
   ```
   pip install MetaTrader5
   ```
4. **Set credentials** as environment variables — never as CLI flags or in
   files committed to git:
   ```
   setx MT5_LOGIN 12345678
   setx MT5_PASSWORD "your-mt5-password"
   setx MT5_SERVER "ICMarketsSC-Demo"
   ```
   The server name appears in MT5 (Tools -> Options -> Server). For live
   accounts it's typically `ICMarkets-Live01` or similar.
5. **Close any conflicting EAs** on the same symbols, or use a separate MT5
   account. The bot only touches positions with magic `19850528`, so it
   coexists with manual trades, but two strategies fighting for the same
   symbol is a bad idea.

### Run it

```
python scripts/paper_bot.py --broker mt5
```

The MT5 terminal must be running and logged in when this executes. The bot
will fail loudly if credentials are missing, symbols aren't enabled, or the
terminal isn't responding.

### What the adapter does

`live/adapters/mt5_broker.py` (`MT5Broker`):
- Initialises the MT5 terminal connection using env vars
- Queries `symbol_info` for `trade_contract_size`, `volume_min`, `volume_step`
  to convert between strategy "units" (asset units) and MT5 "lots"
- Aggregates `positions_get(symbol=...)` filtered by magic into signed unit
  exposure per asset
- Sends market orders via `TRADE_ACTION_DEAL` with `ORDER_FILLING_IOC` and
  `deviation=50` points
- Reads live equity from `account_info().equity`

### IC Markets specifics

| Symbol | Contract size | Min volume | Typical spread (RS) | Notes                          |
| ------ | ------------- | ---------- | ------------------- | ------------------------------ |
| BTCUSD | 1 BTC         | 0.01 lot   | $5-30 (~5-40 bps)   | Swap charged both sides        |
| LTCUSD | 1 LTC         | 1 lot      | $0.05-0.30 (~50 bps)| Min size 1 LTC is the constraint for short legs |
| XAUUSD | 100 oz        | 0.01 lot   | 1.5-3.0 pips (~5 bps)| Long swap ~-4%/yr, short swap usually positive |

Watch out for **LTCUSD min size = 1 LTC**: at ~$80/LTC, a $5 dust trade
wouldn't fire on a $100k account either way, but at <$10k account size you
may not be able to position-size LTC precisely.

### Fees, swaps, what the backtest doesn't model

| Friction        | Estimate           | Where it bites                            |
| --------------- | ------------------ | ----------------------------------------- |
| Spread          | 5-50 bps round-trip| Every rebalance                           |
| Commission (RS) | ~6 bps round-trip  | Every rebalance                           |
| BTC/LTC swap    | ±20-100 bps/day    | Both sides; punishing on overnight shorts |
| XAU long swap   | ~-4% annualised    | When EMALong/XAU is in the market         |

Cumulative drag: **3-7% annualised** off the backtest's +10.4%.
Realistic live target: 3-7% annual return, Sharpe 1.0-1.4.

### Schedule it (Windows Task Scheduler)

Create a daily task triggering:

```
Program:    C:\Python311\python.exe
Arguments:  scripts\paper_bot.py --broker mt5
Start in:   C:\path\to\polymarket-bot
```

Recommended trigger: daily at 23:30 UTC (around the daily candle close on
most brokers; check your broker's server time).

### Sanity checks before going live

1. Run on demo for at least 4 weeks. Daily-diff the bot's equity against
   `python scripts/wf_tuned_triplet.py` — drift > 1% over 30 days means
   your fee/swap model is wrong.
2. Verify the kill switch works by manually editing `data/live_state.json`
   to set `kill_switch_tripped: true`, then running the bot — it should
   flatten all positions and refuse to re-enter.
3. Verify your magic number is unique — `mt5.positions_get()` should return
   nothing other than your bot's positions when filtered by magic.

## Drift monitoring

Daily, compare:

- Today's paper-bot equity (from `data/live_state.json`)
- Today's backtest equity (recompute by running `scripts/wf_tuned_triplet.py`)

If the two diverge by more than ~50 bps over a 30-day rolling window, your
slippage / fee / funding assumptions need updating before any live capital.

## Sanity-check rules baked in

- **No-op trades**: order delta below `$5` is skipped (avoids dust trades).
- **Kill switch**: tripped at -20% drawdown from running peak; once tripped,
  every subsequent run flattens all positions and refuses to re-enter until
  the state file is manually reset.
- **Idempotency**: the bot won't run twice in one UTC day (override with
  `--force`).

## Files

- `live/strategy.py` — daily signal generator (pure function of price frames).
- `live/broker.py` — `Broker` interface + `PaperBroker`.
- `live/state.py` — persisted `BotState` (JSON on disk).
- `live/runner.py` — `run_once`: fetch -> compute -> reconcile -> persist.
- `scripts/paper_bot.py` — CLI entry.
