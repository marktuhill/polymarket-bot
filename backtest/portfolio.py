"""Portfolio combination utilities for strategy diversification analysis.

Given per-bar strategy return series, vol-target each one to a common annualised
target so they're comparable, then combine 50/50 and measure how the pair behaves.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

BARS_PER_YEAR = 365


def vol_scale(returns: pd.Series, target_vol: float = 0.10, bars_per_year: int = BARS_PER_YEAR) -> pd.Series:
    """Scale a return series to a target annualised standard deviation."""
    realised = returns.std(ddof=0) * np.sqrt(bars_per_year)
    if realised == 0 or np.isnan(realised):
        return returns * 0.0
    return returns * (target_vol / realised)


def equity_from_returns(returns: pd.Series, starting: float = 100_000.0) -> pd.Series:
    return starting * (1.0 + returns.fillna(0.0)).cumprod()


@dataclass
class StrategyRun:
    label: str
    returns: pd.Series  # raw daily strategy return (fraction of capital)


def align(runs: list[StrategyRun]) -> pd.DataFrame:
    """Align all return series to the union of dates, missing values = 0 (no trade)."""
    frame = pd.concat({r.label: r.returns for r in runs}, axis=1).sort_index()
    return frame.fillna(0.0)


def metrics(returns: pd.Series, bars_per_year: int = BARS_PER_YEAR) -> dict:
    r = returns.fillna(0.0)
    equity = equity_from_returns(r, 1.0)
    sd = r.std(ddof=0)
    sharpe = float(r.mean() / sd * np.sqrt(bars_per_year)) if sd > 0 else 0.0
    running_max = equity.cummax()
    max_dd = float((equity / running_max - 1.0).min())
    total = float(equity.iloc[-1] - 1.0)
    n = (r != 0).sum()
    ann = float(equity.iloc[-1] ** (bars_per_year / max(len(r), 1)) - 1.0)
    return {
        "total_return": total,
        "ann_return": ann,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "active_bars": int(n),
    }


def rank_pairs(
    frame: pd.DataFrame,
    *,
    target_vol: float = 0.10,
    require_positive_legs: bool = True,
) -> pd.DataFrame:
    """Score every pair: 50/50 combo Sharpe of vol-scaled legs, plus correlation."""
    labels = list(frame.columns)
    scaled = pd.DataFrame({lab: vol_scale(frame[lab], target_vol) for lab in labels})

    rows = []
    for i, a in enumerate(labels):
        for b in labels[i + 1 :]:
            leg_a = scaled[a]
            leg_b = scaled[b]
            if require_positive_legs:
                if metrics(leg_a)["total_return"] <= 0 or metrics(leg_b)["total_return"] <= 0:
                    continue
            combined = 0.5 * leg_a + 0.5 * leg_b
            m = metrics(combined)
            corr = float(frame[a].corr(frame[b]))
            rows.append(
                {
                    "leg_a": a,
                    "leg_b": b,
                    "corr": corr,
                    "combined_sharpe": m["sharpe"],
                    "combined_ann_return": m["ann_return"],
                    "combined_max_dd": m["max_dd"],
                    "combined_total": m["total_return"],
                }
            )
    return pd.DataFrame(rows).sort_values("combined_sharpe", ascending=False).reset_index(drop=True)


def rank_triplets(
    frame: pd.DataFrame,
    *,
    target_vol: float = 0.10,
    require_positive_legs: bool = True,
) -> pd.DataFrame:
    """Score every triplet: equal-weight combo Sharpe of vol-scaled legs."""
    labels = list(frame.columns)
    scaled = pd.DataFrame({lab: vol_scale(frame[lab], target_vol) for lab in labels})
    if require_positive_legs:
        candidates = [lab for lab in labels if metrics(scaled[lab])["total_return"] > 0]
    else:
        candidates = labels

    rows = []
    for i, a in enumerate(candidates):
        for j in range(i + 1, len(candidates)):
            b = candidates[j]
            for k in range(j + 1, len(candidates)):
                c = candidates[k]
                combined = (scaled[a] + scaled[b] + scaled[c]) / 3.0
                m = metrics(combined)
                avg_corr = (
                    frame[a].corr(frame[b])
                    + frame[a].corr(frame[c])
                    + frame[b].corr(frame[c])
                ) / 3.0
                rows.append(
                    {
                        "leg_a": a,
                        "leg_b": b,
                        "leg_c": c,
                        "avg_corr": float(avg_corr),
                        "combined_sharpe": m["sharpe"],
                        "combined_ann_return": m["ann_return"],
                        "combined_max_dd": m["max_dd"],
                        "combined_total": m["total_return"],
                    }
                )
    return pd.DataFrame(rows).sort_values("combined_sharpe", ascending=False).reset_index(drop=True)
