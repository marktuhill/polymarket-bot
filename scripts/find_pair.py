"""Find the lowest-correlation pair of profitable strategies and back-test the 50/50 combo.

Runs every available strategy on every reachable asset, harmonises their daily
return series, vol-targets each to 10% annualised, and ranks all pairs by the
Sharpe of an equal-weight combo (only considering pairs where both legs make
money standalone).
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

from backtest.bb_event import run_bb_backtest
from backtest.crypto_ohlc import load_ohlc as load_crypto_ohlc
from backtest.crypto_ohlc import load_ratio_ohlc
from backtest.data import load_panel
from backtest.engine import backtest as vec_backtest
from backtest.portfolio import (
    StrategyRun,
    align,
    equity_from_returns,
    metrics,
    rank_pairs,
    vol_scale,
)
from backtest.strategies import (
    bb_mean_reversion,
    ema_cross,
    ema_cross_trailing_stop,
)
from backtest.xau_data import fetch_xauusd_daily

START = "2020-01-01"
TARGET_VOL = 0.10
PLOT_PATH = ROOT / "results" / "best_pair_equity.png"


def _bb_event_returns(label: str, df: pd.DataFrame, **kwargs) -> StrategyRun:
    equity, _ = run_bb_backtest(df, **kwargs)
    rets = equity.pct_change().fillna(0.0)
    rets.name = label
    return StrategyRun(label=label, returns=rets)


def _vec_returns(label: str, df: pd.DataFrame, signal_fn) -> StrategyRun:
    res = vec_backtest(df, signal_fn, asset=label, strategy_name="")
    rets = res.returns.copy()
    rets.index = df["date"].values
    rets.name = label
    return StrategyRun(label=label, returns=rets)


def collect_runs() -> list[StrategyRun]:
    runs: list[StrategyRun] = []

    crypto_panel = load_panel(["BTC", "ETH", "LTC"], start=START)
    for asset, df in crypto_panel.items():
        runs.append(_vec_returns(f"EMACross/{asset}", df, ema_cross))
        runs.append(_vec_returns(f"EMACrossTS/{asset}", df, ema_cross_trailing_stop))
        runs.append(_vec_returns(f"BBvec/{asset}", df, bb_mean_reversion))

    ltc_ohlc = load_crypto_ohlc("LTC", start=START)
    btceth_ohlc = load_ratio_ohlc("BTC", "ETH", start=START)
    runs.append(_bb_event_returns("BBevent/LTC", ltc_ohlc))
    runs.append(_bb_event_returns("BBevent/BTC-ETH", btceth_ohlc))
    runs.append(_bb_event_returns("BBevent/BTC-ETH+filter", btceth_ohlc, regime_lookback=100, regime_threshold=0.25))

    xau = fetch_xauusd_daily()
    xau = xau[xau.index >= pd.Timestamp(START)]
    runs.append(_bb_event_returns("BBevent/XAU", xau))

    return runs


def main() -> None:
    runs = collect_runs()
    frame = align(runs)
    print(f"Aligned panel: {len(frame)} bars, {frame.shape[1]} strategies, {frame.index.min().date()} -> {frame.index.max().date()}")

    print("\nStandalone metrics (vol-scaled to 10% ann.):")
    standalone = []
    for label in frame.columns:
        m = metrics(vol_scale(frame[label], TARGET_VOL))
        m["label"] = label
        standalone.append(m)
    sdf = pd.DataFrame(standalone)[["label", "total_return", "ann_return", "sharpe", "max_dd"]]
    sdf = sdf.sort_values("sharpe", ascending=False)
    for col in ("total_return", "ann_return", "max_dd"):
        sdf[col] = sdf[col].map(lambda x: f"{x:+.2%}")
    sdf["sharpe"] = sdf["sharpe"].map(lambda x: f"{x:.2f}")
    print(sdf.to_string(index=False))

    print("\nCorrelation matrix of raw daily returns (lower-triangle):")
    corr = frame.corr()
    print(corr.round(2).to_string())

    ranking = rank_pairs(frame, target_vol=TARGET_VOL, require_positive_legs=True)
    if ranking.empty:
        print("\nNo pair found where both legs are profitable.")
        return

    print(f"\nTop 10 pairs by combined Sharpe (50/50 of vol-scaled legs, target {TARGET_VOL:.0%} ann):")
    top = ranking.head(10).copy()
    for col in ("combined_ann_return", "combined_max_dd", "combined_total"):
        top[col] = top[col].map(lambda x: f"{x:+.2%}")
    top["combined_sharpe"] = top["combined_sharpe"].map(lambda x: f"{x:.2f}")
    top["corr"] = top["corr"].map(lambda x: f"{x:+.2f}")
    print(top.to_string(index=False))

    winner = ranking.iloc[0]
    a, b = winner["leg_a"], winner["leg_b"]
    print(f"\n=== Best pair: {a}  +  {b} ===")
    print(f"  raw return correlation : {winner['corr']:+.3f}")
    leg_a_scaled = vol_scale(frame[a], TARGET_VOL)
    leg_b_scaled = vol_scale(frame[b], TARGET_VOL)
    combined = 0.5 * leg_a_scaled + 0.5 * leg_b_scaled

    print(f"\n  {'metric':<14} {'leg A':>12} {'leg B':>12} {'50/50':>12}")
    for key in ("total_return", "ann_return", "sharpe", "max_dd"):
        ma = metrics(leg_a_scaled)[key]
        mb = metrics(leg_b_scaled)[key]
        mc = metrics(combined)[key]
        if key == "sharpe":
            print(f"  {key:<14} {ma:>12.2f} {mb:>12.2f} {mc:>12.2f}")
        else:
            print(f"  {key:<14} {ma:>12.2%} {mb:>12.2%} {mc:>12.2%}")

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(equity_from_returns(leg_a_scaled).index, equity_from_returns(leg_a_scaled).values, label=f"{a} (vol-scaled)")
    ax.plot(equity_from_returns(leg_b_scaled).index, equity_from_returns(leg_b_scaled).values, label=f"{b} (vol-scaled)")
    ax.plot(equity_from_returns(combined).index, equity_from_returns(combined).values, label="50/50 combo", linewidth=2.0)
    ax.set_title(f"Best uncorrelated pair: {a}  +  {b}   (corr={winner['corr']:+.2f})")
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
