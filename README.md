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
python scripts/run_backtest.py
```

Outputs the metrics table to stdout and writes `results/summary.csv`.

### Strategies ported

| Source file in `nautilus_trader/examples/strategies/` | Ported as |
| --- | --- |
| `ema_cross.py` | `ema_cross` |
| `ema_cross_trailing_stop.py` | `ema_cross_trailing_stop` |
| `bb_mean_reversion.py` | `bb_mean_reversion` |

Market-making strategies (`grid_market_maker`, `volatility_market_maker`,
`orderbook_imbalance`, etc.) need live order book data and are not included.
