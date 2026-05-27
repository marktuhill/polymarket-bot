"""Broker abstraction. PaperBroker simulates fills at next-bar open with slippage.

To deploy live, write a subclass: implement get_positions, get_equity, submit_order
against your exchange's API (Binance Spot Testnet for BTC/LTC, OANDA fxTrade
Practice for XAU is the recommended starting combination).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Fill:
    asset: str
    units: float  # signed; positive = bought, negative = sold
    price: float
    fee_usd: float
    timestamp: pd.Timestamp


class Broker(ABC):
    @abstractmethod
    def get_positions(self) -> dict[str, float]:
        """Returns mapping asset -> signed units currently held."""

    @abstractmethod
    def get_equity(self, mark_prices: dict[str, float]) -> float:
        """Cash + sum(units * mark_price) per asset."""

    @abstractmethod
    def submit_order(self, asset: str, units: float, price_hint: float, timestamp: pd.Timestamp) -> Fill:
        """Submit a market order for ``units`` of ``asset``. Returns the simulated/live Fill."""


@dataclass
class PaperBroker(Broker):
    """In-process simulator. Fills are instantaneous at price_hint * (1 + slip).

    Defaults are deliberately pessimistic: 10 bps slippage and 10 bps per-side
    commission, so a round-trip costs ~40 bps total.
    """

    cash_usd: float = 100_000.0
    slippage_bps: float = 10.0
    commission_bps: float = 10.0
    positions: dict[str, float] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)

    def get_positions(self) -> dict[str, float]:
        return dict(self.positions)

    def get_equity(self, mark_prices: dict[str, float]) -> float:
        eq = self.cash_usd
        for asset, units in self.positions.items():
            mark = mark_prices.get(asset, 0.0)
            eq += units * mark
        return eq

    def submit_order(self, asset: str, units: float, price_hint: float, timestamp: pd.Timestamp) -> Fill:
        if units == 0:
            return Fill(asset=asset, units=0.0, price=price_hint, fee_usd=0.0, timestamp=timestamp)
        slip = self.slippage_bps / 10_000.0
        fill_price = price_hint * (1.0 + slip) if units > 0 else price_hint * (1.0 - slip)
        notional = abs(units * fill_price)
        fee = notional * (self.commission_bps / 10_000.0)
        self.cash_usd -= units * fill_price + fee
        self.positions[asset] = self.positions.get(asset, 0.0) + units
        if abs(self.positions[asset]) < 1e-12:
            self.positions[asset] = 0.0
        fill = Fill(asset=asset, units=units, price=fill_price, fee_usd=fee, timestamp=timestamp)
        self.fills.append(fill)
        return fill
