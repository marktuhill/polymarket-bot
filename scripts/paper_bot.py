"""Paper-trading entry point.

Single run: pulls data, computes signals, executes against a PaperBroker, persists
state. Schedule this with cron / Task Scheduler / a systemd timer to run once
per day after the daily Coin Metrics + XAU CSVs refresh.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from live.broker import PaperBroker
from live.runner import run_once
from live.state import BotState


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily trading runner (paper or MT5).")
    parser.add_argument("--broker", choices=["paper", "mt5"], default="paper",
                        help="paper = in-process simulator (default); mt5 = live IC Markets / any MT5 broker (Windows only).")
    parser.add_argument("--starting-equity", type=float, default=100_000.0,
                        help="Paper-broker initial cash; ignored for mt5.")
    parser.add_argument("--force", action="store_true", help="Run even if already executed today.")
    parser.add_argument("--reset", action="store_true", help="Wipe persisted state and start fresh.")
    parser.add_argument("--slippage-bps", type=float, default=10.0, help="Paper broker slippage (per side).")
    parser.add_argument("--commission-bps", type=float, default=10.0, help="Paper broker commission (per side).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.reset:
        from live.state import DEFAULT_STATE_PATH

        if DEFAULT_STATE_PATH.exists():
            DEFAULT_STATE_PATH.unlink()
            print(f"Removed {DEFAULT_STATE_PATH}")

    state = BotState.load()
    if not state.equity_history:
        state.cash_usd = args.starting_equity

    if args.broker == "mt5":
        from live.adapters.mt5_broker import MT5Broker

        broker = MT5Broker()
    else:
        broker = PaperBroker(
            cash_usd=state.cash_usd,
            slippage_bps=args.slippage_bps,
            commission_bps=args.commission_bps,
            positions=dict(state.positions),
        )

    result = run_once(broker, state, force=args.force)
    state.save()

    print(f"\n=== Run summary for {result.run_date.date()} ===")
    if result.skipped_reason:
        print(f"  skipped: {result.skipped_reason}")
    print(f"  equity: ${result.equity_usd:,.2f}")
    print(f"  cash:   ${broker.cash_usd:,.2f}")
    print(f"  positions: {broker.get_positions()}")
    print(f"  drawdown:  {state.current_drawdown():+.2%}")
    print(f"  kill switch: {state.kill_switch_tripped}")
    if result.signals:
        print(f"\n  Target signals:")
        for sig in result.signals:
            tag = "ACTIVE" if sig.is_active else "MUTED"
            print(
                f"    {sig.name:<16} [{tag}] raw={sig.raw_position:+d} "
                f"sharpe={sig.last_sharpe:+.2f} vol_scale={sig.vol_scale:.2f} "
                f"target_units={sig.target_units:+.6f} target_notional=${sig.target_notional:+,.0f}"
            )
    if result.fills:
        print(f"\n  Fills this run:")
        for fill in result.fills:
            side = "BUY" if fill.units > 0 else "SELL"
            print(
                f"    {side} {abs(fill.units):.6f} {fill.asset} @ {fill.price:.4f}  fee ${fill.fee_usd:.2f}"
            )


if __name__ == "__main__":
    main()
