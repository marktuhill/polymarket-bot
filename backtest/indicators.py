"""Indicators ported to match NautilusTrader's conventions.

Note: NautilusTrader's RelativeStrengthIndex returns values in [0, 1], not [0, 100].
We follow the same convention so the published thresholds (0.30 / 0.70) port directly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi_unit(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder-style RSI rescaled to the [0, 1] range used by NautilusTrader."""
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    roll_up = up.ewm(alpha=1.0 / period, adjust=False).mean()
    roll_down = down.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = roll_up / roll_down.replace(0.0, np.nan)
    rsi_0_100 = 100.0 - 100.0 / (1.0 + rs)
    return (rsi_0_100 / 100.0).fillna(0.5)


def bollinger_bands(series: pd.Series, period: int = 20, std: float = 2.0):
    mid = series.rolling(period).mean()
    sd = series.rolling(period).std(ddof=0)
    return mid - std * sd, mid, mid + std * sd


def close_atr(close: pd.Series, period: int = 14) -> pd.Series:
    """Close-to-close volatility proxy used in place of true ATR.

    Coin Metrics daily files give a single reference rate per day with no high/low,
    so we approximate the average true range with the rolling mean of |close.diff()|.
    """
    return close.diff().abs().ewm(alpha=1.0 / period, adjust=False).mean()
