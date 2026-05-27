"""Daily signal generation for the deployed walk-forward tuned triplet.

Given a price frame per asset, returns target dollar exposure per leg, after
vol-scaling (rolling 6-month vol target 10% annualised) and the leg-dropping
risk control (a leg is muted whenever its rolling 6-month Sharpe is non-positive).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from backtest.bb_event import run_bb_backtest
from backtest.engine import backtest as vec_backtest
from backtest.strategies import ema_cross, ema_cross_long

BARS_PER_YEAR = 365
ROLLING_WINDOW = 180
TARGET_VOL = 0.10


@dataclass
class LegSignal:
    name: str
    asset: str
    target_units: float  # signed asset units to hold; positive = long, negative = short
    target_notional: float  # signed notional in USD
    raw_position: int  # the strategy's native -1 / 0 / +1 position
    vol_scale: float  # multiplier applied to translate raw into target notional
    is_active: bool  # False when leg is muted by the risk control
    last_sharpe: float
    last_price: float


def _btc_signal(btc_df: pd.DataFrame) -> tuple[int, pd.Series, float]:
    """Returns (current_position, daily_return_series, last_price)."""
    res = vec_backtest(btc_df, lambda d: ema_cross(d, 10, 20), asset="BTC", strategy_name="")
    pos = int(res.position.iloc[-1])
    rets = res.returns.copy()
    rets.index = btc_df["date"].values
    last_price = float(btc_df["close"].iloc[-1])
    return pos, rets, last_price


def _ltc_signal(ltc_ohlc: pd.DataFrame) -> tuple[int, pd.Series, float]:
    eq, _, positions = run_bb_backtest(ltc_ohlc)
    rets = eq.pct_change().fillna(0.0)
    pos = int(positions.iloc[-1])
    last_price = float(ltc_ohlc["close"].iloc[-1])
    return pos, rets, last_price


def _xau_signal(xau_df: pd.DataFrame) -> tuple[int, pd.Series, float]:
    res = vec_backtest(xau_df, lambda d: ema_cross_long(d, 50, 100), asset="XAU", strategy_name="")
    pos = int(res.position.iloc[-1])
    rets = res.returns.copy()
    rets.index = xau_df["date"].values
    last_price = float(xau_df["close"].iloc[-1])
    return pos, rets, last_price


def _rolling_sharpe(returns: pd.Series, window: int = ROLLING_WINDOW) -> float:
    recent = returns.iloc[-window:].dropna()
    sd = recent.std(ddof=0)
    if sd == 0 or len(recent) < window // 2:
        return 0.0
    return float(recent.mean() / sd * np.sqrt(BARS_PER_YEAR))


def _rolling_vol_scale(returns: pd.Series, window: int = ROLLING_WINDOW, target: float = TARGET_VOL) -> float:
    recent = returns.iloc[-window:].dropna()
    vol = recent.std(ddof=0) * np.sqrt(BARS_PER_YEAR)
    if vol == 0 or np.isnan(vol):
        return 0.0
    return target / vol


def compute_signals(
    btc_df: pd.DataFrame,
    ltc_ohlc: pd.DataFrame,
    xau_df: pd.DataFrame,
    equity_usd: float,
    *,
    drop_negative_sharpe: bool = True,
) -> list[LegSignal]:
    """Compute target exposures for each leg given today's data and current equity.

    Each leg targets ``TARGET_VOL`` annualised volatility on its raw return series,
    estimated from the last ``ROLLING_WINDOW`` bars. Active legs split the equity
    equally; muted legs (negative rolling Sharpe) get zero allocation.
    """
    btc_pos, btc_ret, btc_price = _btc_signal(btc_df)
    ltc_pos, ltc_ret, ltc_price = _ltc_signal(ltc_ohlc)
    xau_pos, xau_ret, xau_price = _xau_signal(xau_df)

    legs_raw = [
        ("EMACross/BTC", "BTC", btc_pos, btc_ret, btc_price),
        ("BBevent/LTC", "LTC", ltc_pos, ltc_ret, ltc_price),
        ("EMALong/XAU", "XAU", xau_pos, xau_ret, xau_price),
    ]

    active_count = 0
    if drop_negative_sharpe:
        for _, _, _, rets, _ in legs_raw:
            if _rolling_sharpe(rets) > 0:
                active_count += 1
    else:
        active_count = len(legs_raw)
    if active_count == 0:
        active_count = 1  # avoid division by zero; everything muted -> flat anyway

    per_leg_equity = equity_usd / active_count
    signals: list[LegSignal] = []
    for name, asset, pos, rets, last_price in legs_raw:
        sharpe = _rolling_sharpe(rets)
        is_active = (not drop_negative_sharpe) or sharpe > 0
        vs = _rolling_vol_scale(rets) if is_active else 0.0
        notional = pos * vs * per_leg_equity
        units = notional / last_price if last_price > 0 else 0.0
        signals.append(
            LegSignal(
                name=name,
                asset=asset,
                target_units=units,
                target_notional=notional,
                raw_position=pos,
                vol_scale=vs,
                is_active=is_active,
                last_sharpe=sharpe,
                last_price=last_price,
            )
        )
    return signals
