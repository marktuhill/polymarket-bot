"""Vectorised backtest engine with linear fees and slippage."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class BacktestResult:
    asset: str
    strategy: str
    equity: pd.Series
    position: pd.Series
    returns: pd.Series

    def metrics(self, bars_per_year: int = 365) -> dict:
        ret = self.returns.dropna()
        n = len(ret)
        if n == 0:
            return {"asset": self.asset, "strategy": self.strategy, "n_bars": 0}

        eq_end = float(self.equity.iloc[-1])
        total_ret = eq_end - 1.0
        ann_ret = eq_end ** (bars_per_year / n) - 1.0 if eq_end > 0 else -1.0
        sd = ret.std(ddof=0)
        sharpe = (ret.mean() / sd) * np.sqrt(bars_per_year) if sd > 0 else 0.0
        running_max = self.equity.cummax()
        max_dd = float((self.equity / running_max - 1.0).min())

        trades, win_rate = _trade_stats(self.position, self.returns)

        return {
            "asset": self.asset,
            "strategy": self.strategy,
            "n_bars": n,
            "total_return": total_ret,
            "ann_return": ann_ret,
            "sharpe": sharpe,
            "max_dd": max_dd,
            "trades": trades,
            "win_rate": win_rate,
        }


def _trade_stats(position: pd.Series, returns: pd.Series) -> tuple[int, float]:
    """Count non-flat position runs and the fraction with positive cumulative return."""
    pos = position.fillna(0).astype(int).to_numpy()
    rets = returns.fillna(0.0).to_numpy()
    trades = 0
    wins = 0
    i = 0
    n = len(pos)
    while i < n:
        if pos[i] == 0:
            i += 1
            continue
        j = i
        while j < n and pos[j] == pos[i]:
            j += 1
        trade_ret = float(np.prod(1.0 + rets[i:j]) - 1.0)
        trades += 1
        if trade_ret > 0:
            wins += 1
        i = j
    win_rate = wins / trades if trades else 0.0
    return trades, win_rate


def backtest(
    df: pd.DataFrame,
    signal_fn,
    *,
    asset: str,
    strategy_name: str,
    fee_bps: float = 5.0,
    slippage_bps: float = 1.0,
) -> BacktestResult:
    """Run a single (asset, strategy) backtest.

    fee_bps and slippage_bps are *per side*. Total round-trip cost for a position flip
    of size 1.0 is 2 * (fee_bps + slippage_bps).
    """
    position = signal_fn(df).astype(int)
    target = position.shift(1).fillna(0)
    bar_ret = df["close"].pct_change().fillna(0.0)
    gross = target * bar_ret
    turnover = target.diff().abs().fillna(0.0)
    cost_per_unit = (fee_bps + slippage_bps) / 10_000.0
    net = gross - turnover * cost_per_unit
    equity = (1.0 + net).cumprod()
    return BacktestResult(
        asset=asset,
        strategy=strategy_name,
        equity=equity,
        position=position,
        returns=net,
    )


def run_grid(
    panel: dict[str, pd.DataFrame],
    strategies: dict[str, callable],
    *,
    fee_bps: float = 5.0,
    slippage_bps: float = 1.0,
) -> pd.DataFrame:
    rows = []
    for asset, df in panel.items():
        for name, fn in strategies.items():
            res = backtest(
                df,
                fn,
                asset=asset,
                strategy_name=name,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
            )
            rows.append(res.metrics())
    return pd.DataFrame(rows)
