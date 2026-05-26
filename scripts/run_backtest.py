"""Run the EMA / BB strategy grid over BTC, ETH, SOL daily history.

Outputs a metrics table to stdout and to results/summary.csv.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backtest.data import load_panel
from backtest.engine import run_grid
from backtest.strategies import STRATEGIES

ASSETS = ["BTC", "ETH", "LTC"]
START = "2022-01-01"
FEE_BPS = 5.0
SLIPPAGE_BPS = 1.0


def main() -> None:
    print(f"Loading {ASSETS} from {START}...")
    panel = load_panel(ASSETS, start=START)
    for asset, df in panel.items():
        print(f"  {asset}: {len(df)} daily bars, {df['date'].min().date()} -> {df['date'].max().date()}")

    print(
        f"\nRunning {len(STRATEGIES)} strategies x {len(panel)} assets "
        f"with {FEE_BPS:.0f} bps fee + {SLIPPAGE_BPS:.0f} bps slippage per side..."
    )
    summary = run_grid(panel, STRATEGIES, fee_bps=FEE_BPS, slippage_bps=SLIPPAGE_BPS)

    display = summary.copy()
    for col in ("total_return", "ann_return", "max_dd", "win_rate"):
        display[col] = display[col].map(lambda x: f"{x:+.2%}")
    display["sharpe"] = display["sharpe"].map(lambda x: f"{x:.2f}")
    print("\n" + display.to_string(index=False))

    out = ROOT / "results" / "summary.csv"
    summary.to_csv(out, index=False)
    print(f"\nSaved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
