"""Walk-forward validation of the pair-selection procedure.

For each rolling window:
  - in-sample (IS): rank pairs by combined Sharpe (positive legs only).
  - out-of-sample (OOS): trade the top-1 pair, vol-scaling each leg with
    the *in-sample* realised vol so there's no lookahead.

Stitching the OOS windows gives a realised equity curve. We compare it to
trading the static "in-sample-best-overall" pair across the same OOS
windows. If walk-forward holds up vs the static choice, the selection is
not just an artefact of looking at the full history.
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.candidates import collect_runs
from backtest.portfolio import (
    BARS_PER_YEAR,
    align,
    equity_from_returns,
    metrics,
    rank_pairs,
)

START = "2020-01-01"
TARGET_VOL = 0.10
IS_BARS = 24 * 30  # ~24 months on daily data
OOS_BARS = 6 * 30  # ~6 months
PLOT_PATH = ROOT / "results" / "walk_forward_equity.png"


def _is_vol_scale(returns: pd.Series, is_slice: pd.Series, target_vol: float = TARGET_VOL) -> pd.Series:
    """Scale ``returns`` (any window) by the in-sample realised vol of ``is_slice``."""
    is_vol = is_slice.std(ddof=0) * np.sqrt(BARS_PER_YEAR)
    if is_vol == 0 or np.isnan(is_vol):
        return returns * 0.0
    return returns * (target_vol / is_vol)


def walk_forward(frame: pd.DataFrame):
    n = len(frame)
    oos_returns: list[pd.Series] = []
    picks: list[dict] = []

    start = 0
    while start + IS_BARS + OOS_BARS <= n:
        is_slice = frame.iloc[start : start + IS_BARS]
        oos_slice = frame.iloc[start + IS_BARS : start + IS_BARS + OOS_BARS]

        ranking = rank_pairs(is_slice, target_vol=TARGET_VOL, require_positive_legs=True)
        if ranking.empty:
            oos_returns.append(pd.Series(0.0, index=oos_slice.index))
            picks.append(
                {
                    "is_start": is_slice.index[0],
                    "is_end": is_slice.index[-1],
                    "oos_start": oos_slice.index[0],
                    "oos_end": oos_slice.index[-1],
                    "leg_a": None,
                    "leg_b": None,
                    "is_sharpe": np.nan,
                    "oos_total": 0.0,
                }
            )
            start += OOS_BARS
            continue

        top = ranking.iloc[0]
        a, b = top["leg_a"], top["leg_b"]
        leg_a_oos = _is_vol_scale(oos_slice[a], is_slice[a])
        leg_b_oos = _is_vol_scale(oos_slice[b], is_slice[b])
        combo = 0.5 * leg_a_oos + 0.5 * leg_b_oos
        oos_returns.append(combo)
        picks.append(
            {
                "is_start": is_slice.index[0],
                "is_end": is_slice.index[-1],
                "oos_start": oos_slice.index[0],
                "oos_end": oos_slice.index[-1],
                "leg_a": a,
                "leg_b": b,
                "is_sharpe": float(top["combined_sharpe"]),
                "oos_total": float((1.0 + combo).prod() - 1.0),
            }
        )
        start += OOS_BARS

    stitched = pd.concat(oos_returns) if oos_returns else pd.Series(dtype=float)
    return stitched, pd.DataFrame(picks)


def static_best(frame: pd.DataFrame, picks: pd.DataFrame) -> pd.Series:
    """Trade the same OOS windows with the IS-best pair from the very first window."""
    if picks.empty:
        return pd.Series(dtype=float)
    a = picks.iloc[0]["leg_a"]
    b = picks.iloc[0]["leg_b"]
    if a is None or b is None:
        return pd.Series(0.0, index=frame.index)

    is_first = frame.iloc[:IS_BARS]
    series: list[pd.Series] = []
    for _, row in picks.iterrows():
        oos = frame.loc[row["oos_start"] : row["oos_end"]]
        leg_a_oos = _is_vol_scale(oos[a], is_first[a])
        leg_b_oos = _is_vol_scale(oos[b], is_first[b])
        series.append(0.5 * leg_a_oos + 0.5 * leg_b_oos)
    return pd.concat(series)


def overall_best(frame: pd.DataFrame, picks: pd.DataFrame) -> tuple[str, str, pd.Series]:
    """Trade the same OOS windows with the pair that's best across the WHOLE history.

    This is a lookahead-biased benchmark — it represents the theoretical upper
    bound a perfect pair-picker could have hit if it knew the future.
    """
    if picks.empty:
        return "", "", pd.Series(dtype=float)
    ranking = rank_pairs(frame, target_vol=TARGET_VOL, require_positive_legs=True)
    if ranking.empty:
        return "", "", pd.Series(dtype=float)
    a, b = ranking.iloc[0]["leg_a"], ranking.iloc[0]["leg_b"]
    is_first = frame.iloc[:IS_BARS]
    series: list[pd.Series] = []
    for _, row in picks.iterrows():
        oos = frame.loc[row["oos_start"] : row["oos_end"]]
        leg_a_oos = _is_vol_scale(oos[a], is_first[a])
        leg_b_oos = _is_vol_scale(oos[b], is_first[b])
        series.append(0.5 * leg_a_oos + 0.5 * leg_b_oos)
    return a, b, pd.concat(series)


def main() -> None:
    runs = collect_runs(start=START)
    frame = align(runs)
    print(f"Panel: {len(frame)} bars, {frame.shape[1]} strategies, {frame.index.min().date()} -> {frame.index.max().date()}")
    print(f"Walk-forward params: IS={IS_BARS} bars, OOS={OOS_BARS} bars, step={OOS_BARS} bars")

    stitched, picks = walk_forward(frame)
    print(f"\nGenerated {len(picks)} OOS windows covering {stitched.index.min().date() if len(stitched) else '-'} -> {stitched.index.max().date() if len(stitched) else '-'}")

    print("\nPer-window picks and realised OOS performance:")
    show = picks.copy()
    show["pair"] = show.apply(lambda r: f"{r['leg_a']} + {r['leg_b']}" if r["leg_a"] else "(none)", axis=1)
    show["oos_total"] = show["oos_total"].map(lambda x: f"{x:+.2%}")
    show["is_sharpe"] = show["is_sharpe"].map(lambda x: f"{x:.2f}" if not pd.isna(x) else "-")
    show = show[["oos_start", "oos_end", "pair", "is_sharpe", "oos_total"]]
    show["oos_start"] = show["oos_start"].dt.date
    show["oos_end"] = show["oos_end"].dt.date
    print(show.to_string(index=False))

    wf_metrics = metrics(stitched)
    static = static_best(frame, picks)
    static_metrics = metrics(static)
    look_a, look_b, lookahead = overall_best(frame, picks)
    lookahead_metrics = metrics(lookahead)

    pick_changes = picks["leg_a"].astype(str).add("+").add(picks["leg_b"].astype(str))
    n_unique = pick_changes.nunique()
    print(f"\nPick stability: {n_unique} distinct pair selections across {len(picks)} windows")

    print(f"\n{'metric':<14} {'walk-fwd':>14} {'first-window static':>22} {'full-period lookahead':>24}")
    for key in ("total_return", "ann_return", "sharpe", "max_dd"):
        if key == "sharpe":
            print(f"{key:<14} {wf_metrics[key]:>14.2f} {static_metrics[key]:>22.2f} {lookahead_metrics[key]:>24.2f}")
        else:
            print(f"{key:<14} {wf_metrics[key]:>14.2%} {static_metrics[key]:>22.2%} {lookahead_metrics[key]:>24.2%}")
    print(f"\nfull-period lookahead pair: {look_a} + {look_b}")

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(equity_from_returns(stitched).index, equity_from_returns(stitched).values, label="walk-forward (no lookahead)", linewidth=2.0)
    ax.plot(equity_from_returns(static).index, equity_from_returns(static).values, label="first-window static pair", alpha=0.8)
    ax.plot(equity_from_returns(lookahead).index, equity_from_returns(lookahead).values, label=f"full-period lookahead ({look_a}+{look_b})", alpha=0.6, linestyle="--")
    ax.set_title("Walk-forward OOS equity vs static benchmarks")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity ($)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    PLOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(PLOT_PATH, dpi=120)
    print(f"\nSaved walk-forward equity curve: {PLOT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
