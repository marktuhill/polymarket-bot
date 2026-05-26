# polymarket-bot

## NautilusTrader strategy backtests on crypto

`backtest/` ports three example strategies from `nautilus_trader/examples/strategies`
to vectorised pandas, runs them on daily BTC/ETH/LTC close prices, and reports
PnL, Sharpe, max drawdown, trade count, and win rate alongside a buy-and-hold baseline.

### Data source

Binance and other exchange APIs are not reachable from the current container's
network allowlist, so prices come from the `coinmetrics-io/data` GitHub mirror
(`PriceUSD` column). This is daily close-only; ATR-based strategies use a
close-to-close volatility proxy in place of true ATR.

### Run

```
pip install -r requirements.txt
python scripts/run_backtest.py        # vectorised EMA / BB on BTC, ETH, LTC
python scripts/run_bb_xauusd.py       # event-driven BB + ATR stop on XAUUSD daily
```

The vectorised script outputs `results/summary.csv`; the XAUUSD script outputs
`results/bb_xauusd_equity.png` plus a metrics block to stdout.

### Strategies ported

| Source file in `nautilus_trader/examples/strategies/` | Ported as |
| --- | --- |
| `ema_cross.py` | `ema_cross` |
| `ema_cross_trailing_stop.py` | `ema_cross_trailing_stop` |
| `bb_mean_reversion.py` | `bb_mean_reversion` (vectorised) and `bb_event` (intrabar SL) |

Market-making strategies (`grid_market_maker`, `volatility_market_maker`,
`orderbook_imbalance`, etc.) need live order book data and are not included.

### XAUUSD backtest notes

Dukascopy's data servers are blocked by the container's egress policy, so the
XAUUSD daily series is pulled from `FeziweMelvin/XAUUSD-Gold-Price` on GitHub
(a Dukascopy-style daily file). Coverage ends 2025-06-06, so the test split
ends there rather than at 2025-12-31. The cache lives at
`data/XAUUSD_daily.parquet`.
