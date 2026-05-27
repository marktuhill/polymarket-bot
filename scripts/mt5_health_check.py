"""Read-only MT5 connection check. Sends NO orders.

Run this on your Windows VPS before scheduling the bot to confirm:
  - MetaTrader5 Python package is installed
  - MT5 terminal is running and logged in
  - MT5_LOGIN / MT5_PASSWORD / MT5_SERVER env vars are correct
  - BTCUSD, LTCUSD, XAUUSD are visible in Market Watch
  - Contract sizes and minimum volumes are what we expect

Exits with code 0 if everything looks good, non-zero with a clear error if not.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import MetaTrader5 as mt5
except ImportError:
    print("FAIL: MetaTrader5 not installed. Run:  pip install -r requirements-mt5.txt")
    sys.exit(1)


def main() -> int:
    print("=== MT5 health check (read-only, no orders sent) ===\n")

    creds = {k: os.environ.get(k) for k in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")}
    missing = [k for k, v in creds.items() if not v]
    if missing:
        print(f"FAIL: missing env vars: {missing}")
        print("Set them via:  setx MT5_LOGIN ... / setx MT5_PASSWORD ... / setx MT5_SERVER ...")
        print("Then OPEN A NEW shell — setx values aren't picked up by the current one.")
        return 2

    init_kwargs = {
        "login": int(creds["MT5_LOGIN"]),
        "password": creds["MT5_PASSWORD"],
        "server": creds["MT5_SERVER"],
    }
    path = os.environ.get("MT5_PATH")
    if path:
        init_kwargs["path"] = path

    if not mt5.initialize(**init_kwargs):
        print(f"FAIL: mt5.initialize failed: {mt5.last_error()}")
        print("Check:  MT5 terminal is running, logged into the account, and server name exactly matches.")
        return 3

    try:
        info = mt5.account_info()
        if info is None:
            print(f"FAIL: account_info returned None: {mt5.last_error()}")
            return 4
        print(f"OK  connected")
        print(f"    account : {info.login}")
        print(f"    server  : {info.server}")
        print(f"    currency: {info.currency}")
        print(f"    balance : {info.balance:,.2f}")
        print(f"    equity  : {info.equity:,.2f}")
        print(f"    margin  : {info.margin:,.2f}")
        print(f"    free    : {info.margin_free:,.2f}")
        print(f"    leverage: 1:{info.leverage}")
        is_hedging = info.margin_mode == mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
        print(f"    mode    : {'HEDGING' if is_hedging else 'NETTING'}{' (works either way)' if not is_hedging else ' (works, but flips positions instead of netting)'}")

        print("\nSymbol availability:")
        all_ok = True
        for asset, sym in [("BTC", "BTCUSD"), ("LTC", "LTCUSD"), ("XAU", "XAUUSD")]:
            si = mt5.symbol_info(sym)
            if si is None:
                print(f"  FAIL  {sym}: not found in this broker's instrument list")
                all_ok = False
                continue
            if not si.visible:
                if not mt5.symbol_select(sym, True):
                    print(f"  FAIL  {sym}: not visible and symbol_select failed")
                    all_ok = False
                    continue
                si = mt5.symbol_info(sym)
            tick = mt5.symbol_info_tick(sym)
            spread_pts = (tick.ask - tick.bid) / si.point if tick else 0
            print(
                f"  OK    {sym:<8} bid={tick.bid:<10.4f} ask={tick.ask:<10.4f} "
                f"spread={spread_pts:>5.1f}pt  contract={si.trade_contract_size:>9.2f}  "
                f"min={si.volume_min:>5.2f}  step={si.volume_step:.2f}"
            )

        if not all_ok:
            print("\nFAIL: enable missing symbols via MT5 Market Watch -> Show All, then re-run.")
            return 5

        print("\nCurrent open positions (any, all magic numbers):")
        positions = mt5.positions_get() or []
        if not positions:
            print("  (none)")
        else:
            for p in positions:
                side = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
                print(f"  {side:<4} {p.symbol:<8} {p.volume:.2f} lots  magic={p.magic}  P/L={p.profit:+.2f}")

        print("\nALL GREEN — safe to run:  python scripts\\paper_bot.py --broker mt5")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
