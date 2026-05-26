"""Fetch and cache XAUUSD daily OHLC data.

The prompt asked for Dukascopy via `dukascopy-python`, but the container's network
allowlist blocks Dukascopy's data servers. A GitHub repo mirrors a Dukascopy-style
daily file (`XAU_1d_data.csv`) covering 2004-06-11 to 2025-12-31, reachable from
`raw.githubusercontent.com`.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import requests

CSV_URL = "https://raw.githubusercontent.com/xxkyuubixx354-dotcom/QLSTM-LSTM-Evaluation/main/XAU_1d_data.csv"
ROOT = Path(__file__).resolve().parent.parent
PARQUET_PATH = ROOT / "data" / "XAUUSD_daily.parquet"


def fetch_xauusd_daily(*, force: bool = False) -> pd.DataFrame:
    """Return XAUUSD daily OHLCV, cached as parquet under data/."""
    if PARQUET_PATH.exists() and not force:
        return pd.read_parquet(PARQUET_PATH)

    resp = requests.get(CSV_URL, timeout=30)
    resp.raise_for_status()
    csv_path = PARQUET_PATH.with_suffix(".csv")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_bytes(resp.content)

    df = pd.read_csv(csv_path, sep=";")
    df.columns = [c.lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"], format="%Y.%m.%d %H:%M")
    df = df.set_index("date").sort_index()
    df.index = pd.DatetimeIndex([str(d)[:10] for d in df.index])
    df = df[df.index.dayofweek < 6]
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df.to_parquet(PARQUET_PATH)
    return df
