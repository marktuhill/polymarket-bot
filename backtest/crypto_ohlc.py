"""Build daily OHLC frames from the Coin Metrics close-only series.

The coinmetrics-io/data CSVs only ship a daily ``PriceUSD`` close, so we
synthesise open=high=low=close. This means the event engine's intrabar stop
check degenerates to a close-vs-stop comparison, which is the best we can do
without a real OHLC source.
"""
from __future__ import annotations

import pandas as pd

from backtest.data import fetch_asset


def _to_ohlc(close: pd.Series) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": close.values,
            "high": close.values,
            "low": close.values,
            "close": close.values,
            "volume": 0.0,
        },
        index=close.index,
    )


def load_close(asset: str, *, start: str | None = None) -> pd.Series:
    df = fetch_asset(asset)
    if start is not None:
        df = df[df["date"] >= pd.Timestamp(start)]
    return df.set_index("date")["close"].astype(float).sort_index()


def load_ohlc(asset: str, *, start: str | None = None) -> pd.DataFrame:
    return _to_ohlc(load_close(asset, start=start))


def load_ratio_ohlc(numerator: str, denominator: str, *, start: str | None = None) -> pd.DataFrame:
    num = load_close(numerator, start=start)
    den = load_close(denominator, start=start)
    joined = pd.concat({"num": num, "den": den}, axis=1).dropna()
    ratio = joined["num"] / joined["den"]
    ratio.name = f"{numerator.upper()}/{denominator.upper()}"
    return _to_ohlc(ratio)
