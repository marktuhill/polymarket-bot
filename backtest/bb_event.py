"""Event-driven Bollinger Band mean reversion backtest with ATR stop.

Signal logic
------------
- Long entry  : prev close >= lower band AND current close < lower band
- Short entry : prev close <= upper band AND current close > upper band
- Long exit   : close crosses back above middle band
- Short exit  : close crosses back below middle band
- Stop loss   : 1.5 * ATR(14) from entry; checked intrabar via high/low
- No take profit

Execution model
---------------
- Entry on the open of the bar AFTER the signal bar.
- On each subsequent bar:
    1) Stop is checked first using high/low — if hit, exit at the stop price.
    2) Otherwise, if close crosses the middle band the position exits at that close.
- 2 bps round-trip commission is deducted on exit (notional-based).
- One position at a time, no pyramiding.
- Position sizing risks 1% of current equity per trade based on stop distance.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Trade:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    direction: int
    entry: float
    stop: float
    exit_price: float
    exit_type: str
    units: float
    pnl_usd: float
    bars_held: int


def bollinger_bands(close: pd.Series, period: int = 20, std: float = 2.0):
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    return mid - std * sd, mid, mid + std * sd


def atr_simple(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def run_bb_backtest(
    df: pd.DataFrame,
    *,
    bb_period: int = 20,
    bb_std: float = 2.0,
    atr_period: int = 14,
    atr_mult: float = 1.5,
    risk_per_trade: float = 0.01,
    commission_bps: float = 2.0,
    starting_equity: float = 100_000.0,
    regime_lookback: int | None = None,
    regime_threshold: float = 0.25,
) -> tuple[pd.Series, list[Trade], pd.Series]:
    """Run the strategy on a daily OHLC DataFrame indexed by date.

    When ``regime_lookback`` is set, signals are skipped on bars where the
    absolute rolling percentage change of close over that lookback exceeds
    ``regime_threshold`` (i.e. the asset is trending too strongly to fade).

    Returns (equity_series, completed_trades, position_series). The position
    series records the strategy's net direction at the close of each bar,
    valued in {-1, 0, +1}.
    """
    df = df.copy()
    lower, mid, upper = bollinger_bands(df["close"], bb_period, bb_std)
    atr = atr_simple(df["high"], df["low"], df["close"], atr_period)
    df["lower"], df["mid"], df["upper"], df["atr"] = lower, mid, upper, atr

    if regime_lookback is not None:
        regime = df["close"].pct_change(regime_lookback).abs()
        regime_ok = (regime < regime_threshold).to_numpy()
    else:
        regime_ok = np.ones(len(df), dtype=bool)

    o = df["open"].to_numpy()
    h = df["high"].to_numpy()
    l = df["low"].to_numpy()
    c = df["close"].to_numpy()
    lo_b = df["lower"].to_numpy()
    mi_b = df["mid"].to_numpy()
    up_b = df["upper"].to_numpy()
    atr_v = df["atr"].to_numpy()
    idx = df.index

    equity = starting_equity
    equity_series = np.full(len(df), starting_equity, dtype=float)
    position_series = np.zeros(len(df), dtype=int)
    trades: list[Trade] = []

    position = 0
    pending_entry: int | None = None  # direction queued for next bar's open
    entry_atr = np.nan
    entry_price = np.nan
    entry_idx = -1
    stop = np.nan
    units = 0.0
    commission_rate = commission_bps / 10_000.0

    warmup = max(bb_period, atr_period) + 1

    for i in range(len(df)):
        # 1) Open a queued position at this bar's open.
        if pending_entry is not None and position == 0:
            direction = pending_entry
            entry_price = o[i]
            stop_distance = atr_mult * entry_atr
            stop = entry_price - direction * stop_distance
            risk_dollars = risk_per_trade * equity
            units = risk_dollars / stop_distance if stop_distance > 0 else 0.0
            if units > 0:
                position = direction
                entry_idx = i
            pending_entry = None

        # 2) Manage an open position.
        if position != 0:
            exit_price: float | None = None
            exit_type = ""
            if position == 1 and l[i] <= stop:
                exit_price = stop
                exit_type = "stop"
            elif position == -1 and h[i] >= stop:
                exit_price = stop
                exit_type = "stop"
            else:
                if position == 1 and c[i] >= mi_b[i]:
                    exit_price = c[i]
                    exit_type = "middle_band"
                elif position == -1 and c[i] <= mi_b[i]:
                    exit_price = c[i]
                    exit_type = "middle_band"

            if exit_price is not None:
                gross = units * position * (exit_price - entry_price)
                notional = units * exit_price
                fee = commission_rate * notional
                pnl = gross - fee
                equity += pnl
                trades.append(
                    Trade(
                        entry_date=idx[entry_idx],
                        exit_date=idx[i],
                        direction=position,
                        entry=entry_price,
                        stop=stop,
                        exit_price=exit_price,
                        exit_type=exit_type,
                        units=units,
                        pnl_usd=pnl,
                        bars_held=i - entry_idx,
                    )
                )
                position = 0
                units = 0.0

        # 3) Look for a new signal (only if flat with no pending entry).
        if position == 0 and pending_entry is None and i >= warmup and regime_ok[i]:
            crossed_below_lower = c[i - 1] >= lo_b[i - 1] and c[i] < lo_b[i]
            crossed_above_upper = c[i - 1] <= up_b[i - 1] and c[i] > up_b[i]
            if crossed_below_lower:
                pending_entry = 1
                entry_atr = atr_v[i]
            elif crossed_above_upper:
                pending_entry = -1
                entry_atr = atr_v[i]

        equity_series[i] = equity
        position_series[i] = position

    eq = pd.Series(equity_series, index=df.index, name="equity")
    pos = pd.Series(position_series, index=df.index, name="position")
    return eq, trades, pos


def metrics(equity: pd.Series, trades: list[Trade]) -> dict:
    starting = equity.iloc[0]
    ending = equity.iloc[-1]
    total_return = ending / starting - 1.0

    if not trades:
        return {
            "total_return_pct": total_return * 100,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "max_dd_pct": 0.0,
            "trades": 0,
            "avg_bars_held": 0.0,
        }

    pnls = np.array([t.pnl_usd for t in trades])
    wins = pnls[pnls > 0].sum()
    losses = pnls[pnls < 0].sum()
    profit_factor = float(wins / -losses) if losses < 0 else float("inf")

    running_max = equity.cummax()
    max_dd = float((equity / running_max - 1.0).min())

    return {
        "total_return_pct": total_return * 100,
        "win_rate_pct": (pnls > 0).mean() * 100,
        "profit_factor": profit_factor,
        "max_dd_pct": max_dd * 100,
        "trades": len(trades),
        "avg_bars_held": float(np.mean([t.bars_held for t in trades])),
    }
