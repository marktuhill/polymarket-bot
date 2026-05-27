"""MetaTrader 5 broker adapter, tuned for IC Markets Raw Spread accounts.

Requires the ``MetaTrader5`` Python package (Windows only) and a running MT5
terminal logged into the target account. The adapter only touches positions
opened with its own magic number, so it coexists safely with manual trades or
other EAs running on the same account.

Credentials are read from environment variables — never accept them via CLI
flags or write them to log files.

  MT5_LOGIN    integer account number
  MT5_PASSWORD account password (use the read-write password, not investor)
  MT5_SERVER   broker server name shown in MT5, e.g. "ICMarketsSC-Demo"
  MT5_PATH     optional path to terminal64.exe (only needed if non-default)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import pandas as pd

try:
    import MetaTrader5 as mt5  # type: ignore
except ImportError:  # platform without the package (e.g. this Linux container)
    mt5 = None  # type: ignore[assignment]

from ..broker import Broker, Fill

LOGGER = logging.getLogger(__name__)

# IC Markets Raw Spread accounts expose these symbol names directly.
DEFAULT_SYMBOL_MAP = {"BTC": "BTCUSD", "LTC": "LTCUSD", "XAU": "XAUUSD"}

# Identifies orders placed by this bot; survives terminal restarts.
DEFAULT_MAGIC = 19850528


class MT5Broker(Broker):
    """MetaTrader 5 broker adapter.

    Only manages positions tagged with the configured magic number so it
    coexists with other strategies and manual trades on the same account.
    """

    def __init__(
        self,
        *,
        symbol_map: dict[str, str] | None = None,
        magic: int = DEFAULT_MAGIC,
        deviation_points: int = 50,
    ) -> None:
        if mt5 is None:
            raise RuntimeError(
                "MetaTrader5 Python package not available. "
                "Install with `pip install MetaTrader5` on a Windows host."
            )

        init_kwargs: dict[str, object] = {}
        path = os.environ.get("MT5_PATH")
        login = os.environ.get("MT5_LOGIN")
        password = os.environ.get("MT5_PASSWORD")
        server = os.environ.get("MT5_SERVER")
        if path:
            init_kwargs["path"] = path
        if login:
            init_kwargs["login"] = int(login)
        if password:
            init_kwargs["password"] = password
        if server:
            init_kwargs["server"] = server

        if not mt5.initialize(**init_kwargs):
            raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")

        info = mt5.account_info()
        if info is None:
            raise RuntimeError("MT5 connected but account_info unavailable; check credentials.")
        LOGGER.info(
            "MT5 connected: account=%s server=%s currency=%s balance=%.2f equity=%.2f",
            info.login, info.server, info.currency, info.balance, info.equity,
        )

        self.symbol_map = symbol_map or DEFAULT_SYMBOL_MAP
        self.magic = magic
        self.deviation_points = deviation_points
        self._symbol_info: dict[str, object] = {}

        for asset, sym in self.symbol_map.items():
            si = mt5.symbol_info(sym)
            if si is None:
                raise RuntimeError(
                    f"Symbol {sym!r} (for {asset}) not found. "
                    "Check Market Watch -> Show All in MT5 and enable it on your broker account."
                )
            if not si.visible and not mt5.symbol_select(sym, True):
                raise RuntimeError(f"Failed to add {sym!r} to Market Watch")
            self._symbol_info[sym] = si
            LOGGER.info(
                "  %s: contract_size=%.4f volume_min=%.4f volume_step=%.4f digits=%d",
                sym, si.trade_contract_size, si.volume_min, si.volume_step, si.digits,
            )

    def shutdown(self) -> None:
        if mt5 is not None:
            mt5.shutdown()

    def get_positions(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for asset, sym in self.symbol_map.items():
            positions = mt5.positions_get(symbol=sym) or []
            contract_size = self._symbol_info[sym].trade_contract_size
            net_lots = 0.0
            for p in positions:
                if p.magic != self.magic:
                    continue
                signed_lots = p.volume if p.type == mt5.POSITION_TYPE_BUY else -p.volume
                net_lots += signed_lots
            out[asset] = net_lots * contract_size
        return out

    def get_equity(self, mark_prices: dict[str, float]) -> float:  # noqa: ARG002 — broker is source of truth
        info = mt5.account_info()
        if info is None:
            raise RuntimeError("account_info unavailable mid-session")
        return float(info.equity)

    def submit_order(self, asset: str, units: float, price_hint: float, timestamp: pd.Timestamp) -> Fill:
        if units == 0:
            return Fill(asset=asset, units=0.0, price=price_hint, fee_usd=0.0, timestamp=timestamp)

        sym = self.symbol_map[asset]
        si = self._symbol_info[sym]
        contract_size = si.trade_contract_size

        lots = abs(units) / contract_size
        step = si.volume_step
        lots = round(lots / step) * step
        lots = max(si.volume_min, min(si.volume_max, lots))
        if lots < si.volume_min:
            LOGGER.warning("%s: requested units=%.6f below volume_min %.4f lots — skipping",
                           sym, units, si.volume_min)
            return Fill(asset=asset, units=0.0, price=price_hint, fee_usd=0.0, timestamp=timestamp)

        is_buy = units > 0
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            raise RuntimeError(f"No tick for {sym}")
        ref_price = tick.ask if is_buy else tick.bid

        filling_mode = mt5.ORDER_FILLING_IOC
        # IC Markets Raw Spread supports IOC; FOK is also fine. If broker rejects IOC,
        # fall back to RETURN.
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": lots,
            "type": order_type,
            "price": ref_price,
            "deviation": self.deviation_points,
            "magic": self.magic,
            "comment": "wf_triplet",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = mt5.last_error() if mt5 is not None else "unknown"
            raise RuntimeError(
                f"order_send failed for {sym}: retcode={getattr(result, 'retcode', '?')} "
                f"comment={getattr(result, 'comment', '?')} last_error={err}"
            )

        signed_units = lots * contract_size * (1 if is_buy else -1)
        LOGGER.info(
            "  filled %s %s: %.4f lots (%.6f units) @ %s",
            "BUY" if is_buy else "SELL", sym, lots, signed_units, result.price,
        )
        return Fill(
            asset=asset,
            units=signed_units,
            price=float(result.price),
            fee_usd=0.0,  # MT5 commission is debited from equity; not surfaced per-fill
            timestamp=timestamp,
        )
