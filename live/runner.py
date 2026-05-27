"""One-shot daily runner: fetch data, compute signals, send orders, save state.

Designed to be called once per day (cron / Task Scheduler). It is idempotent
within a single calendar date — if you run it twice in the same UTC day it
won't double-trade, because it checks state.last_run_date.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from backtest.crypto_ohlc import load_ohlc as load_crypto_ohlc
from backtest.data import load_panel
from backtest.xau_data import fetch_xauusd_daily

from .broker import Broker, Fill
from .state import BotState
from .strategy import LegSignal, compute_signals

LOGGER = logging.getLogger(__name__)

KILL_SWITCH_DD = -0.20  # 20% drawdown from peak forces a flat halt


@dataclass
class RunResult:
    run_date: pd.Timestamp
    equity_usd: float
    signals: list[LegSignal]
    fills: list[Fill]
    kill_switch_tripped: bool
    skipped_reason: str | None = None


def _load_data(history_start: str = "2020-01-01"):
    crypto = load_panel(["BTC"], start=history_start)
    btc_df = crypto["BTC"]
    ltc_ohlc = load_crypto_ohlc("LTC", start=history_start)
    xau = fetch_xauusd_daily()
    xau = xau[xau.index >= pd.Timestamp(history_start)]
    xau_df = xau[["open", "high", "low", "close"]].copy()
    xau_df["date"] = xau.index
    xau_df = xau_df.reset_index(drop=True)
    return btc_df, ltc_ohlc, xau_df


def _mark_prices(signals: list[LegSignal]) -> dict[str, float]:
    return {sig.asset: sig.last_price for sig in signals}


def run_once(broker: Broker, state: BotState, *, force: bool = False) -> RunResult:
    """Compute today's signals, reconcile to broker positions, record equity."""
    btc_df, ltc_ohlc, xau_df = _load_data()
    today = pd.Timestamp(btc_df["date"].max())
    today_str = str(today.date())

    marks_for_equity = {
        "BTC": float(btc_df["close"].iloc[-1]),
        "LTC": float(ltc_ohlc["close"].iloc[-1]),
        "XAU": float(xau_df["close"].iloc[-1]),
    }
    equity = broker.get_equity(marks_for_equity)

    if not force and state.last_run_date == today_str:
        LOGGER.info("already ran for %s, skipping", today_str)
        return RunResult(run_date=today, equity_usd=equity, signals=[], fills=[], kill_switch_tripped=state.kill_switch_tripped, skipped_reason="already_ran_today")
    LOGGER.info("equity at %s: $%.2f", today_str, equity)

    if state.kill_switch_tripped:
        LOGGER.warning("kill switch already tripped — flattening any remaining positions and halting")
        fills = _flatten(broker, marks_for_equity, today)
        state.cash_usd = broker.cash_usd if hasattr(broker, "cash_usd") else state.cash_usd
        state.positions = broker.get_positions()
        state.record_equity(today, broker.get_equity(marks_for_equity), note="kill_switch")
        return RunResult(run_date=today, equity_usd=equity, signals=[], fills=fills, kill_switch_tripped=True, skipped_reason="kill_switch")

    signals = compute_signals(btc_df, ltc_ohlc, xau_df, equity)
    current_positions = broker.get_positions()
    fills: list[Fill] = []
    for sig in signals:
        current_units = current_positions.get(sig.asset, 0.0)
        delta = sig.target_units - current_units
        if abs(delta * sig.last_price) < 5.0:
            LOGGER.info("  %s: no-op (delta notional < $5)", sig.name)
            continue
        LOGGER.info(
            "  %s: target %.6f units ($%.0f notional), have %.6f -> ordering %.6f",
            sig.name,
            sig.target_units,
            sig.target_notional,
            current_units,
            delta,
        )
        fill = broker.submit_order(sig.asset, delta, sig.last_price, today)
        fills.append(fill)

    new_equity = broker.get_equity(marks_for_equity)
    state.cash_usd = broker.cash_usd if hasattr(broker, "cash_usd") else state.cash_usd
    state.positions = broker.get_positions()
    state.record_equity(today, new_equity)

    dd = state.current_drawdown()
    if dd <= KILL_SWITCH_DD:
        LOGGER.error("KILL SWITCH: drawdown %.2f%% breached threshold %.2f%%", dd * 100, KILL_SWITCH_DD * 100)
        state.kill_switch_tripped = True
        flat_fills = _flatten(broker, marks_for_equity, today)
        fills.extend(flat_fills)
        state.positions = broker.get_positions()
        state.record_equity(today, broker.get_equity(marks_for_equity), note="kill_switch_trip")

    return RunResult(
        run_date=today,
        equity_usd=new_equity,
        signals=signals,
        fills=fills,
        kill_switch_tripped=state.kill_switch_tripped,
    )


def _flatten(broker: Broker, marks: dict[str, float], today: pd.Timestamp) -> list[Fill]:
    fills = []
    for asset, units in list(broker.get_positions().items()):
        if abs(units) < 1e-9:
            continue
        fills.append(broker.submit_order(asset, -units, marks.get(asset, 0.0), today))
    return fills
