"""Honest baseline comparison: does the strategy machinery beat just owning the assets?

For every candidate (buy-and-hold each asset, simple equal-weight baskets, and
each strategy we've built), report native and vol-scaled metrics over a common
2020-01-01 to 2025-12-31 window. This makes it obvious whether the pair/triplet
work adds real Sharpe or whether buy-and-hold already covers the same ground.
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
from backtest.strategies import ema_cross, ema_cross_long
from backtest.xau_data import fetch_xauusd_daily

START = "2020-01-01"
END = "2025-12-31"
BARS_PER_YEAR = 365
TARGET_VOL = 0.10
PLOT_PATH = ROOT / "results" / "baselines_equity.png"
CSV_PATH = ROOT / "results" / "baselines_comparison.csv"


def _metrics(r: pd.Series) -> dict:
    r = r.dropna()
    if len(r) < 2 or r.std(ddof=0) == 0:
        return {"total": 0.0, "ann": 0.0, "ann_vol": 0.0, "sharpe": 0.0, "max_dd": 0.0}
    eq = (1.0 + r.fillna(0.0)).cumprod()
    total = float(eq.iloc[-1] - 1.0)
    years = len(r) / BARS_PER_YEAR
    ann = float(eq.iloc[-1] ** (1.0 / years) - 1.0) if eq.iloc[-1] > 0 else -1.0
    vol = float(r.std(ddof=0) * np.sqrt(BARS_PER_YEAR))
    sharpe = float(r.mean() / r.std(ddof=0) * np.sqrt(BARS_PER_YEAR))
    dd = float((eq / eq.cummax() - 1.0).min())
    return {"total": total, "ann": ann, "ann_vol": vol, "sharpe": sharpe, "max_dd": dd}


def _vol_scale(r: pd.Series, target: float = TARGET_VOL) -> pd.Series:
    vol = r.std(ddof=0) * np.sqrt(BARS_PER_YEAR)
    if vol == 0 or np.isnan(vol):
        return r * 0.0
    return r * (target / vol)


def _trim(s: pd.Series) -> pd.Series:
    return s.loc[(s.index >= pd.Timestamp(START)) & (s.index <= pd.Timestamp(END))]


def collect_returns() -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}

    crypto = load_panel(["BTC", "ETH", "LTC"], start=START)
    for asset, df in crypto.items():
        s = df.set_index("date")["close"].astype(float).pct_change()
        out[f"BH-{asset}"] = _trim(s)

    xau = fetch_xauusd_daily()
    xau = xau[xau.index >= pd.Timestamp(START)]
    out["BH-XAU"] = _trim(xau["close"].pct_change())

    btc = out["BH-BTC"]
    eth = out["BH-ETH"]
    xau_r = out["BH-XAU"]
    out["BH-50/50-BTC-XAU"] = ((btc.fillna(0) + xau_r.fillna(0)) / 2.0).rename("BH-50/50-BTC-XAU")
    out["BH-1/3-BTC-ETH-XAU"] = (
        (btc.fillna(0) + eth.fillna(0) + xau_r.fillna(0)) / 3.0
    ).rename("BH-1/3-BTC-ETH-XAU")

    ltc_ohlc = load_crypto_ohlc("LTC", start=START)
    eq_ltc, _ = run_bb_backtest(ltc_ohlc)
    out["STRAT-BBevent/LTC"] = _trim(eq_ltc.pct_change())

    xv = xau[["open", "high", "low", "close"]].copy()
    xv["date"] = xau.index
    xv = xv.reset_index(drop=True)
    res_xau = vec_backtest(xv, lambda d: ema_cross_long(d, 50, 100), asset="XAU", strategy_name="")
    r_xau = res_xau.returns.copy()
    r_xau.index = xv["date"].values
    out["STRAT-EMALong-50-100/XAU"] = _trim(r_xau)

    btc_df = crypto["BTC"]
    res_btc = vec_backtest(btc_df, ema_cross, asset="BTC", strategy_name="")
    r_btc = res_btc.returns.copy()
    r_btc.index = btc_df["date"].values
    out["STRAT-EMACross/BTC"] = _trim(r_btc)

    pair = pd.concat([out["STRAT-BBevent/LTC"], out["STRAT-EMALong-50-100/XAU"]], axis=1).fillna(0.0)
    out["STRAT-pair (native avg)"] = pair.mean(axis=1)
    triplet = pd.concat(
        [out["STRAT-EMACross/BTC"], out["STRAT-BBevent/LTC"], out["STRAT-EMALong-50-100/XAU"]],
        axis=1,
    ).fillna(0.0)
    out["STRAT-triplet (native avg)"] = triplet.mean(axis=1)

    return out


def main() -> None:
    returns = collect_returns()
    common = pd.concat(returns, axis=1).sort_index()
    print(f"Common date range: {common.index.min().date()} -> {common.index.max().date()} ({len(common)} bars)")

    rows_native = []
    rows_scaled = []
    for name, r in returns.items():
        m = _metrics(r)
        m["name"] = name
        rows_native.append(m)
        s = _metrics(_vol_scale(r, TARGET_VOL))
        s["name"] = name
        rows_scaled.append(s)

    cols = ["name", "ann", "ann_vol", "sharpe", "max_dd", "total"]
    native = pd.DataFrame(rows_native)[cols].sort_values("sharpe", ascending=False)
    scaled = pd.DataFrame(rows_scaled)[cols].sort_values("sharpe", ascending=False)

    def fmt(df):
        df = df.copy()
        for c in ("ann", "ann_vol", "max_dd", "total"):
            df[c] = df[c].map(lambda x: f"{x:+.1%}")
        df["sharpe"] = df["sharpe"].map(lambda x: f"{x:.2f}")
        return df

    print("\n=== NATIVE units (no leverage / scaling) ===")
    print(fmt(native).to_string(index=False))
    print(f"\n=== Vol-scaled to {TARGET_VOL:.0%} annualised (Sharpe and DD become directly comparable) ===")
    print(fmt(scaled).to_string(index=False))

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    native.assign(scale="native").to_csv(CSV_PATH, index=False)
    print(f"\nSaved {CSV_PATH.relative_to(ROOT)}")

    fig, ax = plt.subplots(figsize=(11, 6))
    for name, r in returns.items():
        eq = (1.0 + _vol_scale(r, TARGET_VOL).fillna(0.0)).cumprod()
        style = "-" if name.startswith("STRAT") else "--"
        ax.plot(eq.index, eq.values, style, label=name, alpha=0.85)
    ax.set_title(f"All candidates vol-scaled to {TARGET_VOL:.0%} annualised (Sharpe-comparable)")
    ax.set_xlabel("Date")
    ax.set_ylabel("Equity ($)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(PLOT_PATH, dpi=120)
    print(f"Saved {PLOT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
