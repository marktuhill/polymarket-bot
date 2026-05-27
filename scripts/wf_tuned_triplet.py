"""Walk-forward tuned triplet portfolio — the headline result.

Three uncorrelated legs, each with a small parameter grid:
  - EMACross on BTC      (trend on crypto major)
  - BBevent on LTC       (mean reversion on range-bound alt)
  - EMALong on XAU       (long-only trend on gold)

Every 6 months we re-tune each leg's parameters from the prior 24 months of
in-sample data, vol-scale each leg with the in-sample vol, drop any leg whose
in-sample Sharpe is non-positive, and trade equal weights of the survivors for
the next 6 months. The vol scaling and the leg-dropping use IS information
only — no peeking at OOS.

Compared against:
  - the default (no-tuning, no-walk-forward, equal-weight) triplet,
  - the tuned static pair from tune_pair.py,
  - the simplest dumb baseline (50/50 BTC + XAU, daily-rebalanced).
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

from backtest.bb_event import run_bb_backtest
from backtest.crypto_ohlc import load_ohlc as load_crypto_ohlc
from backtest.data import load_panel
from backtest.engine import backtest as vec_backtest
from backtest.portfolio import BARS_PER_YEAR, equity_from_returns, metrics
from backtest.strategies import ema_cross, ema_cross_long
from backtest.xau_data import fetch_xauusd_daily

START = "2020-01-01"
END = "2025-12-31"
IS_BARS = 24 * 30
OOS_BARS = 6 * 30
TARGET_VOL = 0.10
PLOT_PATH = ROOT / "results" / "wf_tuned_triplet_equity.png"

BB_GRID = [
    {"bb_period": 15, "bb_std": 1.5, "atr_mult": 1.0},
    {"bb_period": 20, "bb_std": 2.0, "atr_mult": 1.5},
    {"bb_period": 20, "bb_std": 1.5, "atr_mult": 2.0},
    {"bb_period": 25, "bb_std": 2.0, "atr_mult": 2.0},
]
EMACROSS_BTC_GRID = [(5, 20), (10, 20), (10, 30), (20, 50)]
EMALONG_XAU_GRID = [(20, 50), (20, 100), (50, 100), (50, 200)]


def _trim(s: pd.Series) -> pd.Series:
    return s.loc[(s.index >= pd.Timestamp(START)) & (s.index <= pd.Timestamp(END))]


def _bb_returns(ohlc: pd.DataFrame, **kwargs) -> pd.Series:
    eq, _ = run_bb_backtest(ohlc, **kwargs)
    return eq.pct_change().fillna(0.0)


def _vec_returns(df: pd.DataFrame, signal_fn) -> pd.Series:
    res = vec_backtest(df, signal_fn, asset="", strategy_name="")
    r = res.returns.copy()
    r.index = df["date"].values
    return r


def build_candidates() -> dict[str, pd.Series]:
    """Precompute the daily return series for every (leg, param-set) candidate."""
    candidates: dict[str, pd.Series] = {}

    crypto = load_panel(["BTC", "LTC"], start=START)
    btc_df = crypto["BTC"]
    for fast, slow in EMACROSS_BTC_GRID:
        r = _vec_returns(btc_df, lambda d, f=fast, s=slow: ema_cross(d, f, s))
        candidates[f"BTC|EMACross|{fast}-{slow}"] = _trim(r)

    ltc_ohlc = load_crypto_ohlc("LTC", start=START)
    for cfg in BB_GRID:
        tag = f"{cfg['bb_period']}-{cfg['bb_std']}-{cfg['atr_mult']}"
        r = _bb_returns(ltc_ohlc, **cfg)
        candidates[f"LTC|BBevent|{tag}"] = _trim(r)

    xau = fetch_xauusd_daily()
    xau = xau[xau.index >= pd.Timestamp(START)]
    xv = xau[["open", "high", "low", "close"]].copy()
    xv["date"] = xau.index
    xv = xv.reset_index(drop=True)
    for fast, slow in EMALONG_XAU_GRID:
        r = _vec_returns(xv, lambda d, f=fast, s=slow: ema_cross_long(d, f, s))
        candidates[f"XAU|EMALong|{fast}-{slow}"] = _trim(r)

    return candidates


def _legs(label: str) -> tuple[str, str]:
    asset, _name, _params = label.split("|", 2)
    return asset, label


def _is_sharpe(r: pd.Series) -> float:
    sd = r.std(ddof=0)
    if sd == 0 or np.isnan(sd):
        return -np.inf
    return float(r.mean() / sd * np.sqrt(BARS_PER_YEAR))


def _is_vol(r: pd.Series) -> float:
    return float(r.std(ddof=0) * np.sqrt(BARS_PER_YEAR))


def walk_forward(frame: pd.DataFrame, candidates: dict[str, pd.Series]) -> tuple[pd.Series, pd.DataFrame]:
    """Stitch OOS returns selecting best param per leg from each IS window."""
    n = len(frame)
    legs = ["BTC", "LTC", "XAU"]
    by_leg: dict[str, list[str]] = {leg: [] for leg in legs}
    for label in candidates:
        by_leg[label.split("|", 1)[0]].append(label)

    oos_returns: list[pd.Series] = []
    picks_log: list[dict] = []

    start = 0
    while start + IS_BARS + OOS_BARS <= n:
        is_dates = frame.index[start : start + IS_BARS]
        oos_dates = frame.index[start + IS_BARS : start + IS_BARS + OOS_BARS]

        selections: dict[str, dict] = {}
        for leg in legs:
            best_label = None
            best_sharpe = -np.inf
            for label in by_leg[leg]:
                r = candidates[label].reindex(is_dates).fillna(0.0)
                sh = _is_sharpe(r)
                if sh > best_sharpe:
                    best_sharpe = sh
                    best_label = label
            is_r = candidates[best_label].reindex(is_dates).fillna(0.0)
            selections[leg] = {
                "label": best_label,
                "is_sharpe": best_sharpe,
                "is_vol": _is_vol(is_r),
            }

        active = [sel for sel in selections.values() if sel["is_sharpe"] > 0]
        if not active:
            window_combo = pd.Series(0.0, index=oos_dates)
        else:
            scaled: list[pd.Series] = []
            for sel in active:
                vol = sel["is_vol"]
                if vol == 0 or np.isnan(vol):
                    continue
                k = TARGET_VOL / vol
                oos_r = candidates[sel["label"]].reindex(oos_dates).fillna(0.0)
                scaled.append(oos_r * k)
            window_combo = sum(scaled) / len(scaled)

        oos_returns.append(window_combo)
        picks_log.append(
            {
                "is_start": is_dates[0],
                "is_end": is_dates[-1],
                "oos_start": oos_dates[0],
                "oos_end": oos_dates[-1],
                "btc": selections["BTC"]["label"] if selections["BTC"]["is_sharpe"] > 0 else "(dropped)",
                "ltc": selections["LTC"]["label"] if selections["LTC"]["is_sharpe"] > 0 else "(dropped)",
                "xau": selections["XAU"]["label"] if selections["XAU"]["is_sharpe"] > 0 else "(dropped)",
                "n_active": len(active),
                "oos_total": float((1.0 + window_combo).prod() - 1.0),
            }
        )
        start += OOS_BARS

    stitched = pd.concat(oos_returns) if oos_returns else pd.Series(dtype=float)
    return stitched, pd.DataFrame(picks_log)


def static_default_triplet(candidates: dict[str, pd.Series], picks_dates: list[pd.Timestamp]) -> pd.Series:
    """Default-parameter triplet, vol-scaled with full-OOS-window vol, traded on the same dates."""
    btc = candidates["BTC|EMACross|10-20"]
    ltc = candidates["LTC|BBevent|20-2.0-1.5"]
    xau = candidates["XAU|EMALong|50-100"]
    legs = []
    for r in (btc, ltc, xau):
        oos = r.reindex(picks_dates).fillna(0.0)
        vol = oos.std(ddof=0) * np.sqrt(BARS_PER_YEAR)
        if vol > 0:
            legs.append(oos * (TARGET_VOL / vol))
    return sum(legs) / len(legs)


def bh_50_50_btc_xau(picks_dates: list[pd.Timestamp]) -> pd.Series:
    crypto = load_panel(["BTC"], start=START)
    btc_close = crypto["BTC"].set_index("date")["close"].astype(float).pct_change()
    xau = fetch_xauusd_daily()
    xau = xau[xau.index >= pd.Timestamp(START)]["close"].pct_change()
    btc_oos = btc_close.reindex(picks_dates).fillna(0.0)
    xau_oos = xau.reindex(picks_dates).fillna(0.0)
    combined = 0.5 * btc_oos + 0.5 * xau_oos
    vol = combined.std(ddof=0) * np.sqrt(BARS_PER_YEAR)
    return combined * (TARGET_VOL / vol) if vol > 0 else combined * 0.0


def _report(name: str, r: pd.Series) -> dict:
    m = metrics(r)
    print(
        f"{name:<40} ann {m['ann_return']:+.2%}   sharpe {m['sharpe']:.2f}   "
        f"max DD {m['max_dd']:+.2%}   total {m['total_return']:+.2%}"
    )
    return m


def main() -> None:
    print("Building candidate strategies...")
    candidates = build_candidates()
    print(f"  {len(candidates)} candidate return series across 3 legs")

    frame = pd.concat(candidates, axis=1).sort_index().fillna(0.0)
    stitched, picks = walk_forward(frame, candidates)
    print(f"\nWalk-forward generated {len(picks)} OOS windows ({stitched.index.min().date()} -> {stitched.index.max().date()})")

    print("\nPer-window param picks:")
    for _, row in picks.iterrows():
        print(
            f"  OOS {row['oos_start'].date()} -> {row['oos_end'].date()}  "
            f"active={row['n_active']}  oos {row['oos_total']:+.2%}  "
            f"BTC={row['btc']}  LTC={row['ltc']}  XAU={row['xau']}"
        )

    picks_dates = stitched.index
    default_static = static_default_triplet(candidates, picks_dates)
    bh = bh_50_50_btc_xau(picks_dates)

    print("\n=== Out-of-sample comparison (vol-scaled to 10% ann) ===")
    m_wf = _report("WALK-FORWARD TUNED TRIPLET", stitched)
    m_def = _report("Default static triplet", default_static)
    m_bh = _report("BH 50/50 BTC + XAU", bh)

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.plot(equity_from_returns(stitched).index, equity_from_returns(stitched).values, label="walk-forward tuned triplet", linewidth=2.4, color="#1f77b4")
    ax.plot(equity_from_returns(default_static).index, equity_from_returns(default_static).values, label="default static triplet", linewidth=1.6, color="#ff7f0e")
    ax.plot(equity_from_returns(bh).index, equity_from_returns(bh).values, label="BH 50/50 BTC + XAU", linewidth=1.6, linestyle="--", color="#2ca02c")
    ax.set_title(
        f"Out-of-sample equity: walk-forward tuned triplet vs benchmarks\n"
        f"(WF Sharpe {m_wf['sharpe']:.2f} vs default {m_def['sharpe']:.2f} vs BH50/50 {m_bh['sharpe']:.2f})"
    )
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity ($)")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    PLOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(PLOT_PATH, dpi=120)
    print(f"\nSaved {PLOT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
