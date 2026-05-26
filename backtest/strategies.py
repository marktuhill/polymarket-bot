"""Three NautilusTrader strategies ported to vectorised pandas signal functions.

Each strategy takes a price DataFrame with a 'close' column and returns a 'position'
Series valued in {-1, 0, +1}. Position is the *target* exposure for the bar after
the signal is observed; the backtest engine shifts it by one bar to avoid lookahead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import bollinger_bands, close_atr, ema, rsi_unit


def ema_cross(df: pd.DataFrame, fast: int = 10, slow: int = 20) -> pd.Series:
    """Long when fast EMA >= slow EMA, short otherwise. Mirrors EMACross."""
    fast_e = ema(df["close"], fast)
    slow_e = ema(df["close"], slow)
    pos = pd.Series(0, index=df.index, dtype=int)
    pos[fast_e >= slow_e] = 1
    pos[fast_e < slow_e] = -1
    pos.iloc[: max(fast, slow)] = 0
    return pos


def ema_cross_trailing_stop(
    df: pd.DataFrame,
    fast: int = 10,
    slow: int = 20,
    atr_period: int = 14,
    atr_mult: float = 3.0,
) -> pd.Series:
    """EMA cross entries with an ATR-based trailing stop.

    Loop is iterative because trailing stops are path-dependent.
    """
    close = df["close"].to_numpy()
    fast_e = ema(df["close"], fast).to_numpy()
    slow_e = ema(df["close"], slow).to_numpy()
    atr = close_atr(df["close"], atr_period).to_numpy()
    n = len(close)

    pos = np.zeros(n, dtype=int)
    state = 0
    stop = np.nan
    warmup = max(fast, slow, atr_period)

    for i in range(warmup, n):
        signal_long = fast_e[i] >= slow_e[i] and fast_e[i - 1] < slow_e[i - 1]
        signal_short = fast_e[i] < slow_e[i] and fast_e[i - 1] >= slow_e[i - 1]

        if state == 1:
            stop = max(stop, close[i] - atr_mult * atr[i])
            if close[i] < stop or signal_short:
                state = 0
        elif state == -1:
            stop = min(stop, close[i] + atr_mult * atr[i])
            if close[i] > stop or signal_long:
                state = 0

        if state == 0:
            if signal_long:
                state = 1
                stop = close[i] - atr_mult * atr[i]
            elif signal_short:
                state = -1
                stop = close[i] + atr_mult * atr[i]

        pos[i] = state

    return pd.Series(pos, index=df.index)


def bb_mean_reversion(
    df: pd.DataFrame,
    bb_period: int = 20,
    bb_std: float = 2.0,
    rsi_period: int = 14,
    rsi_buy: float = 0.30,
    rsi_sell: float = 0.70,
) -> pd.Series:
    """Bollinger Band mean reversion with RSI confirmation.

    Long when close <= lower band AND RSI < buy threshold; short on the mirror.
    Exit (flat) when price reverts through the middle band in the favourable direction.
    """
    close = df["close"]
    lower, mid, upper = bollinger_bands(close, bb_period, bb_std)
    rsi = rsi_unit(close, rsi_period)

    n = len(df)
    pos = np.zeros(n, dtype=int)
    state = 0
    warmup = max(bb_period, rsi_period)

    c = close.to_numpy()
    lo = lower.to_numpy()
    md = mid.to_numpy()
    up = upper.to_numpy()
    r = rsi.to_numpy()

    for i in range(warmup, n):
        if state == 1 and c[i] >= md[i]:
            state = 0
        elif state == -1 and c[i] <= md[i]:
            state = 0

        if state == 0:
            if c[i] <= lo[i] and r[i] < rsi_buy:
                state = 1
            elif c[i] >= up[i] and r[i] > rsi_sell:
                state = -1

        pos[i] = state

    return pd.Series(pos, index=df.index)


def buy_and_hold(df: pd.DataFrame) -> pd.Series:
    """Static long-only baseline for comparison."""
    return pd.Series(1, index=df.index, dtype=int)


def ema_cross_long(df: pd.DataFrame, fast: int = 50, slow: int = 100) -> pd.Series:
    """Long-only EMA cross. Position is +1 when fast >= slow, 0 otherwise."""
    fast_e = ema(df["close"], fast)
    slow_e = ema(df["close"], slow)
    pos = pd.Series(0, index=df.index, dtype=int)
    pos[fast_e >= slow_e] = 1
    pos.iloc[: max(fast, slow)] = 0
    return pos


def donchian_breakout(
    df: pd.DataFrame, entry_window: int = 20, exit_window: int = 10
) -> pd.Series:
    """Long-only Donchian (Turtle-style) breakout.

    Enter long when close breaks above the prior ``entry_window``-bar high.
    Exit when close breaks below the prior ``exit_window``-bar low.
    Uses high/low if present, otherwise falls back to close.
    """
    high = df["high"] if "high" in df.columns else df["close"]
    low = df["low"] if "low" in df.columns else df["close"]
    close = df["close"]

    entry_high = high.rolling(entry_window).max().shift(1)
    exit_low = low.rolling(exit_window).min().shift(1)

    c = close.to_numpy()
    eh = entry_high.to_numpy()
    el = exit_low.to_numpy()
    n = len(df)
    pos = np.zeros(n, dtype=int)
    state = 0
    warmup = max(entry_window, exit_window) + 1
    for i in range(warmup, n):
        if state == 0 and c[i] > eh[i]:
            state = 1
        elif state == 1 and c[i] < el[i]:
            state = 0
        pos[i] = state
    return pd.Series(pos, index=df.index)


STRATEGIES = {
    "BuyAndHold": buy_and_hold,
    "EMACross": ema_cross,
    "EMACrossTrailingStop": ema_cross_trailing_stop,
    "BBMeanReversion": bb_mean_reversion,
}
