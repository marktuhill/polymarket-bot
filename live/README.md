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

## Going from paper to a real exchange

`live/broker.py` defines the `Broker` interface. `PaperBroker` is the default
in-process simulator (fills at next bar's price ± slippage_bps; commission
per side). To trade against a real venue, subclass `Broker`:

```python
class BinanceSpotTestnetBroker(Broker):
    def __init__(self, api_key: str, api_secret: str): ...
    def get_positions(self) -> dict[str, float]: ...
    def get_equity(self, mark_prices: dict[str, float]) -> float: ...
    def submit_order(self, asset, units, price_hint, timestamp) -> Fill: ...
```

Recommended starting venues for this triplet:

| Leg  | Exchange (paper)                      | API docs                                                      |
| ---- | ------------------------------------- | ------------------------------------------------------------- |
| BTC  | Binance Spot Testnet                  | https://testnet.binance.vision/                               |
| LTC  | Binance Spot Testnet (spot + margin)  | same — LTC/USDT margin is needed for the short side           |
| XAU  | OANDA fxTrade Practice                | https://developer.oanda.com/rest-live-v20/introduction/       |

LTC shorts on Binance margin incur funding/borrow cost (often 0.02-0.10% per
day in normal regimes, spiking higher in stress). The backtest does **not**
model this — expect 2-5% annualised drag once live.

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
