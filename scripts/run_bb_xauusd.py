"""Run the Bollinger Band mean-reversion + ATR-stop backtest on XAUUSD daily data.

Splits 2020-01-01 -> 2022-12-31 (train) and 2023-01-01 -> end (test), reports
metrics for each split, writes an equity curve PNG, and prints sample trades.
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.bb_event import metrics, run_bb_backtest
from backtest.xau_data import fetch_xauusd_daily

TRAIN_START = "2020-01-01"
TRAIN_END = "2022-12-31"
TEST_START = "2023-01-01"
TEST_END = "2025-12-31"
PLOT_PATH = ROOT / "results" / "bb_xauusd_equity.png"


def _slice(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    mask = (df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))
    return df.loc[mask].copy()


def _print_metrics(name: str, m: dict) -> None:
    print(f"\n--- {name} ---")
    print(f"  total return : {m['total_return_pct']:+.2f}%")
    print(f"  win rate     : {m['win_rate_pct']:.1f}%")
    print(f"  profit factor: {m['profit_factor']:.2f}")
    print(f"  max drawdown : {m['max_dd_pct']:.2f}%")
    print(f"  trades       : {m['trades']}")
    print(f"  avg bars held: {m['avg_bars_held']:.1f}")


def main() -> None:
    df = fetch_xauusd_daily()
    print(f"Loaded XAUUSD daily: {len(df)} bars, {df.index.min().date()} -> {df.index.max().date()}")

    train_df = _slice(df, TRAIN_START, TRAIN_END)
    test_df = _slice(df, TEST_START, TEST_END)
    print(f"  train: {len(train_df)} bars, {train_df.index.min().date()} -> {train_df.index.max().date()}")
    print(f"  test : {len(test_df)} bars, {test_df.index.min().date()} -> {test_df.index.max().date()}")

    train_eq, train_trades = run_bb_backtest(train_df)
    test_eq, test_trades = run_bb_backtest(test_df)

    _print_metrics("TRAIN 2020-2022", metrics(train_eq, train_trades))
    _print_metrics(f"TEST {TEST_START[:4]}-{test_df.index.max().year}", metrics(test_eq, test_trades))

    print("\nSample trades (first 5 from test split):")
    print(f"  {'entry_date':<12} {'dir':>4} {'entry':>10} {'stop':>10} {'exit_type':>12} {'pnl_usd':>12}")
    for t in test_trades[:5]:
        print(
            f"  {str(t.entry_date.date()):<12} {('LONG' if t.direction == 1 else 'SHORT'):>4} "
            f"{t.entry:>10.2f} {t.stop:>10.2f} {t.exit_type:>12} {t.pnl_usd:>+12.2f}"
        )

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(train_eq.index, train_eq.values, label=f"Train ({TRAIN_START} – {TRAIN_END})")
    ax.plot(test_eq.index, test_eq.values, label=f"Test ({TEST_START} – {test_df.index.max().date()})")
    ax.set_title("BB mean reversion on XAUUSD daily — equity curve")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity ($)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    PLOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(PLOT_PATH, dpi=120)
    print(f"\nSaved equity curve: {PLOT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
