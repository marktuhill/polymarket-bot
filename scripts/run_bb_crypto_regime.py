"""Compare the BB + ATR-stop strategy on crypto with and without a regime filter.

Targets where mean-reversion is supposed to thrive: a range-bound single name
(LTC) and the BTC/ETH ratio (cointegrated by construction, so the mean is real).
Each is run with no regime filter and with a 100-day "flat" filter that blocks
signals when |100d return| >= 25%.
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
from backtest.crypto_ohlc import load_ohlc, load_ratio_ohlc

START = "2018-01-01"
REGIME_LOOKBACK = 100
REGIME_THRESHOLD = 0.25
PLOT_PATH = ROOT / "results" / "bb_crypto_regime_equity.png"


def _run(label: str, df: pd.DataFrame, *, regime: bool):
    eq, trades = run_bb_backtest(
        df,
        regime_lookback=REGIME_LOOKBACK if regime else None,
        regime_threshold=REGIME_THRESHOLD,
    )
    m = metrics(eq, trades)
    m["asset"] = label
    m["regime_filter"] = regime
    return eq, trades, m


def main() -> None:
    series = {
        "LTC": load_ohlc("LTC", start=START),
        "BTC/ETH": load_ratio_ohlc("BTC", "ETH", start=START),
    }
    for label, df in series.items():
        print(f"{label}: {len(df)} bars, {df.index.min().date()} -> {df.index.max().date()}")

    rows = []
    curves = {}
    for label, df in series.items():
        for regime in (False, True):
            eq, trades, m = _run(label, df, regime=regime)
            rows.append(m)
            curves[(label, regime)] = eq

    summary = pd.DataFrame(rows)[
        [
            "asset",
            "regime_filter",
            "total_return_pct",
            "win_rate_pct",
            "profit_factor",
            "max_dd_pct",
            "trades",
            "avg_bars_held",
        ]
    ]
    fmt = summary.copy()
    fmt["total_return_pct"] = fmt["total_return_pct"].map(lambda x: f"{x:+.2f}%")
    fmt["win_rate_pct"] = fmt["win_rate_pct"].map(lambda x: f"{x:.1f}%")
    fmt["max_dd_pct"] = fmt["max_dd_pct"].map(lambda x: f"{x:.2f}%")
    fmt["profit_factor"] = fmt["profit_factor"].map(lambda x: f"{x:.2f}")
    fmt["avg_bars_held"] = fmt["avg_bars_held"].map(lambda x: f"{x:.1f}")
    print("\n" + fmt.to_string(index=False))

    fig, axes = plt.subplots(len(series), 1, figsize=(11, 4 * len(series)), sharex=False)
    if len(series) == 1:
        axes = [axes]
    for ax, (label, _df) in zip(axes, series.items()):
        ax.plot(curves[(label, False)].index, curves[(label, False)].values, label="no regime filter")
        ax.plot(
            curves[(label, True)].index,
            curves[(label, True)].values,
            label=f"regime filter (|{REGIME_LOOKBACK}d ret| < {REGIME_THRESHOLD:.0%})",
        )
        ax.set_title(f"BB mean reversion on {label} — equity curve")
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
