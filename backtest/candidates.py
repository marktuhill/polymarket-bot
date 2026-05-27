"""Shared candidate-strategy panel builder.

Builds the same return panel used by both the static pair-ranker and the
walk-forward harness so they evaluate exactly the same universe.
"""
from __future__ import annotations

import pandas as pd

from backtest.bb_event import run_bb_backtest
from backtest.crypto_ohlc import load_ohlc as load_crypto_ohlc
from backtest.crypto_ohlc import load_ratio_ohlc
from backtest.data import load_panel
from backtest.engine import backtest as vec_backtest
from backtest.portfolio import StrategyRun
from backtest.strategies import (
    bb_mean_reversion,
    donchian_breakout,
    ema_cross,
    ema_cross_long,
    ema_cross_trailing_stop,
)
from backtest.xau_data import fetch_xauusd_daily


def _bb_event_returns(label: str, df: pd.DataFrame, **kwargs) -> StrategyRun:
    equity, _, _ = run_bb_backtest(df, **kwargs)
    rets = equity.pct_change().fillna(0.0)
    rets.name = label
    return StrategyRun(label=label, returns=rets)


def _vec_returns(label: str, df: pd.DataFrame, signal_fn) -> StrategyRun:
    res = vec_backtest(df, signal_fn, asset=label, strategy_name="")
    rets = res.returns.copy()
    rets.index = df["date"].values
    rets.name = label
    return StrategyRun(label=label, returns=rets)


def collect_runs(start: str = "2020-01-01") -> list[StrategyRun]:
    runs: list[StrategyRun] = []

    crypto_panel = load_panel(["BTC", "ETH", "LTC"], start=start)
    for asset, df in crypto_panel.items():
        runs.append(_vec_returns(f"EMACross/{asset}", df, ema_cross))
        runs.append(_vec_returns(f"EMACrossTS/{asset}", df, ema_cross_trailing_stop))
        runs.append(_vec_returns(f"BBvec/{asset}", df, bb_mean_reversion))

    ltc_ohlc = load_crypto_ohlc("LTC", start=start)
    btceth_ohlc = load_ratio_ohlc("BTC", "ETH", start=start)
    runs.append(_bb_event_returns("BBevent/LTC", ltc_ohlc))
    runs.append(_bb_event_returns("BBevent/BTC-ETH", btceth_ohlc))
    runs.append(
        _bb_event_returns(
            "BBevent/BTC-ETH+filter",
            btceth_ohlc,
            regime_lookback=100,
            regime_threshold=0.25,
        )
    )

    xau = fetch_xauusd_daily()
    xau = xau[xau.index >= pd.Timestamp(start)]
    runs.append(_bb_event_returns("BBevent/XAU", xau))

    xau_vec = xau[["open", "high", "low", "close"]].copy()
    xau_vec["date"] = xau.index
    xau_vec = xau_vec.reset_index(drop=True)
    runs.append(_vec_returns("EMACross/XAU", xau_vec, ema_cross))
    runs.append(_vec_returns("EMACrossTS/XAU", xau_vec, ema_cross_trailing_stop))
    runs.append(_vec_returns("EMALong-50-100/XAU", xau_vec, lambda d: ema_cross_long(d, 50, 100)))
    runs.append(_vec_returns("EMALong-50-200/XAU", xau_vec, lambda d: ema_cross_long(d, 50, 200)))
    runs.append(_vec_returns("EMALong-20-50/XAU", xau_vec, lambda d: ema_cross_long(d, 20, 50)))
    runs.append(_vec_returns("Donchian-20-10/XAU", xau_vec, lambda d: donchian_breakout(d, 20, 10)))
    runs.append(_vec_returns("Donchian-55-20/XAU", xau_vec, lambda d: donchian_breakout(d, 55, 20)))

    return runs
