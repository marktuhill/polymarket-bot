"""Parameter tune the LTC mean-rev + XAU trend pair, honestly.

Split: 2020-01-01 to 2022-12-31 (train) is used to grid-search both legs.
Out-of-sample window 2023-01-01 to 2025-12-31 (test) is the only number
that matters.

Vol scaling uses TRAIN-period realised vol so the test result has no
forward-looking information.
"""
from __future__ import annotations

import sys
from itertools import product
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.bb_event import run_bb_backtest
from backtest.crypto_ohlc import load_ohlc as load_crypto_ohlc
from backtest.engine import backtest as vec_backtest
from backtest.portfolio import BARS_PER_YEAR, equity_from_returns, metrics
from backtest.strategies import ema_cross_long
from backtest.xau_data import fetch_xauusd_daily

TRAIN_START = "2020-01-01"
TRAIN_END = "2022-12-31"
TEST_START = "2023-01-01"
TEST_END = "2025-12-31"
TARGET_VOL = 0.10
PLOT_PATH = ROOT / "results" / "tuned_pair_equity.png"


def _split(r: pd.Series) -> tuple[pd.Series, pd.Series]:
    idx = r.index
    train = r[(idx >= TRAIN_START) & (idx <= TRAIN_END)]
    test = r[(idx >= TEST_START) & (idx <= TEST_END)]
    return train, test


def _train_vol_scale(r: pd.Series, train: pd.Series, target: float = TARGET_VOL) -> pd.Series:
    vol = train.std(ddof=0) * np.sqrt(BARS_PER_YEAR)
    if vol == 0 or np.isnan(vol):
        return r * 0.0
    return r * (target / vol)


def _bb_returns(ltc_ohlc: pd.DataFrame, **kwargs) -> pd.Series:
    eq, _ = run_bb_backtest(ltc_ohlc, **kwargs)
    return eq.pct_change().fillna(0.0)


def _ema_returns(xv: pd.DataFrame, fast: int, slow: int) -> pd.Series:
    res = vec_backtest(xv, lambda d: ema_cross_long(d, fast, slow), asset="XAU", strategy_name="")
    r = res.returns.copy()
    r.index = xv["date"].values
    return r


def sweep_bb(ltc_ohlc: pd.DataFrame) -> pd.DataFrame:
    bb_periods = (15, 20, 25, 30)
    bb_stds = (1.5, 2.0, 2.5)
    atr_mults = (1.0, 1.5, 2.0, 3.0)
    rows = []
    for bb_p, bb_s, atr_m in product(bb_periods, bb_stds, atr_mults):
        r = _bb_returns(ltc_ohlc, bb_period=bb_p, bb_std=bb_s, atr_mult=atr_m)
        train, _ = _split(r)
        if train.std(ddof=0) == 0:
            continue
        scaled = _train_vol_scale(train, train)
        m = metrics(scaled)
        rows.append(
            {
                "bb_period": bb_p,
                "bb_std": bb_s,
                "atr_mult": atr_m,
                "train_sharpe": m["sharpe"],
                "train_ann": m["ann_return"],
                "train_dd": m["max_dd"],
                "returns": r,
            }
        )
    return pd.DataFrame(rows).sort_values("train_sharpe", ascending=False).reset_index(drop=True)


def sweep_ema(xv: pd.DataFrame) -> pd.DataFrame:
    grid = [(20, 50), (30, 75), (50, 100), (50, 150), (50, 200), (100, 200), (20, 100), (30, 120)]
    rows = []
    for fast, slow in grid:
        r = _ema_returns(xv, fast, slow)
        train, _ = _split(r)
        if train.std(ddof=0) == 0:
            continue
        scaled = _train_vol_scale(train, train)
        m = metrics(scaled)
        rows.append(
            {
                "fast": fast,
                "slow": slow,
                "train_sharpe": m["sharpe"],
                "train_ann": m["ann_return"],
                "train_dd": m["max_dd"],
                "returns": r,
            }
        )
    return pd.DataFrame(rows).sort_values("train_sharpe", ascending=False).reset_index(drop=True)


def evaluate_pair(r_a: pd.Series, r_b: pd.Series, label: str) -> dict:
    train_a, test_a = _split(r_a)
    train_b, test_b = _split(r_b)
    scaled_train_a = _train_vol_scale(train_a, train_a)
    scaled_train_b = _train_vol_scale(train_b, train_b)
    scaled_test_a = _train_vol_scale(test_a, train_a)
    scaled_test_b = _train_vol_scale(test_b, train_b)
    combined_train = 0.5 * scaled_train_a + 0.5 * scaled_train_b
    combined_test = 0.5 * scaled_test_a + 0.5 * scaled_test_b
    mt = metrics(combined_train)
    me = metrics(combined_test)
    return {
        "label": label,
        "train_sharpe": mt["sharpe"],
        "train_ann": mt["ann_return"],
        "train_dd": mt["max_dd"],
        "test_sharpe": me["sharpe"],
        "test_ann": me["ann_return"],
        "test_dd": me["max_dd"],
        "test_total": me["total_return"],
        "test_eq": equity_from_returns(combined_test),
    }


def main() -> None:
    print("Loading data...")
    ltc_ohlc = load_crypto_ohlc("LTC", start=TRAIN_START)
    xau = fetch_xauusd_daily()
    xau = xau[xau.index >= pd.Timestamp(TRAIN_START)]
    xv = xau[["open", "high", "low", "close"]].copy()
    xv["date"] = xau.index
    xv = xv.reset_index(drop=True)

    print("\nSweeping BBevent/LTC params (4 x 3 x 4 = 48 combos)...")
    bb_sweep = sweep_bb(ltc_ohlc)
    print("Top 5 by TRAIN Sharpe:")
    show = bb_sweep.drop(columns="returns").head(5).copy()
    show["train_sharpe"] = show["train_sharpe"].map(lambda x: f"{x:.2f}")
    show["train_ann"] = show["train_ann"].map(lambda x: f"{x:+.1%}")
    show["train_dd"] = show["train_dd"].map(lambda x: f"{x:+.1%}")
    print(show.to_string(index=False))

    print("\nSweeping EMALong/XAU params (8 period combos)...")
    ema_sweep = sweep_ema(xv)
    print("Top 5 by TRAIN Sharpe:")
    show = ema_sweep.drop(columns="returns").head(5).copy()
    show["train_sharpe"] = show["train_sharpe"].map(lambda x: f"{x:.2f}")
    show["train_ann"] = show["train_ann"].map(lambda x: f"{x:+.1%}")
    show["train_dd"] = show["train_dd"].map(lambda x: f"{x:+.1%}")
    print(show.to_string(index=False))

    best_bb = bb_sweep.iloc[0]
    best_ema = ema_sweep.iloc[0]
    print(
        f"\nBest BB params:  period={int(best_bb['bb_period'])}, std={best_bb['bb_std']}, atr_mult={best_bb['atr_mult']}"
    )
    print(f"Best EMA params: fast={int(best_ema['fast'])}, slow={int(best_ema['slow'])}")

    default_bb = _bb_returns(ltc_ohlc)
    default_ema = _ema_returns(xv, 50, 100)

    default_eval = evaluate_pair(default_bb, default_ema, "DEFAULT (BB 20/2.0/1.5 + EMA 50/100)")
    tuned_eval = evaluate_pair(
        best_bb["returns"],
        best_ema["returns"],
        f"TUNED (BB {int(best_bb['bb_period'])}/{best_bb['bb_std']}/{best_bb['atr_mult']} + EMA {int(best_ema['fast'])}/{int(best_ema['slow'])})",
    )

    print("\n=== Pair comparison ===")
    header = f"{'pair':<55} {'train Sharpe':>14} {'train ann':>11} {'test Sharpe':>13} {'test ann':>10} {'test DD':>10} {'test total':>12}"
    print(header)
    for e in (default_eval, tuned_eval):
        print(
            f"{e['label']:<55} {e['train_sharpe']:>14.2f} {e['train_ann']:>10.2%} "
            f"{e['test_sharpe']:>13.2f} {e['test_ann']:>9.2%} {e['test_dd']:>9.2%} {e['test_total']:>11.2%}"
        )

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(default_eval["test_eq"].index, default_eval["test_eq"].values, label=default_eval["label"], linewidth=1.6)
    ax.plot(tuned_eval["test_eq"].index, tuned_eval["test_eq"].values, label=tuned_eval["label"], linewidth=2.2)
    ax.set_title(f"Tuned vs default pair — OUT-OF-SAMPLE test ({TEST_START} to {TEST_END}, vol-scaled with train vol)")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity ($)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    PLOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(PLOT_PATH, dpi=120)
    print(f"\nSaved test-window equity curve: {PLOT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
