"""Fetch and cache daily close prices for crypto assets from the coinmetrics-io/data GitHub mirror.

The Binance REST API is not reachable from this container's network policy, so we use the
Coin Metrics community CSVs published as plain files on GitHub. These give daily close
(ReferenceRateUSD) for major assets with multi-year history.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import requests

BASE_URL = "https://raw.githubusercontent.com/coinmetrics-io/data/master/csv"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data"


def _cache_path(asset: str) -> Path:
    return CACHE_DIR / f"{asset.lower()}.csv"


def fetch_asset(asset: str, *, force: bool = False) -> pd.DataFrame:
    """Return a daily price DataFrame indexed by date with a single 'close' column."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(asset)
    if force or not path.exists():
        url = f"{BASE_URL}/{asset.lower()}.csv"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        path.write_bytes(resp.content)

    df = pd.read_csv(path, parse_dates=["time"])
    df = df.rename(columns={"time": "date", "PriceUSD": "close"})
    df = df[["date", "close"]].dropna().reset_index(drop=True)
    return df


def load_panel(assets: list[str], start: str | None = None) -> dict[str, pd.DataFrame]:
    panel: dict[str, pd.DataFrame] = {}
    for asset in assets:
        df = fetch_asset(asset)
        if start is not None:
            df = df[df["date"] >= pd.Timestamp(start)].reset_index(drop=True)
        panel[asset.upper()] = df
    return panel
