#!/usr/bin/env python3
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

"""
resolve_trades.py — Resolve paper and live trades against Polymarket outcomes.

Checks every unresolved row in paper_trades.csv and live_trades.csv against
the Polymarket CLOB API, then writes resolved/outcome/pnl columns in-place.

Usage:
  python resolve_trades.py              # resolve both files
  python resolve_trades.py --paper      # paper trades only
  python resolve_trades.py --live       # live trades only
  python resolve_trades.py --dry-run    # show what would be resolved, no writes
"""

import argparse
import csv
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT     = Path(__file__).parent
DATA_DIR = ROOT / "data"

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

PAPER_CSV = DATA_DIR / "paper_trades.csv"
LIVE_CSV  = DATA_DIR / "live_trades.csv"

PAPER_HEADERS = [
    "date_logged", "tournament", "match_or_market", "direction",
    "olbg_stars", "olbg_odds", "tipster_name", "tipster_profit",
    "tipster_expert", "polymarket_price", "tipster_implied_prob",
    "value_gap_pp", "confidence", "polymarket_condition_id",
    "resolved", "outcome", "pnl_paper",
]

LIVE_HEADERS = [
    "timestamp", "condition_id", "market", "direction",
    "price", "shares", "stake_usd", "confidence", "signal_sources",
    "order_id", "status", "resolved", "outcome", "pnl",
]


# ── Polymarket outcome lookup ─────────────────────────────────────────────────

def _fetch_outcome(session: requests.Session, condition_id: str) -> str | None:
    """
    Return 'YES', 'NO', or None (still open/unknown).

    Queries Gamma API for the market; if closed, reads outcomePrices[0].
    """
    if not condition_id:
        return None
    try:
        r = session.get(
            f"{GAMMA_API}/markets",
            params={"condition_id": condition_id},
            timeout=10,
        )
        if not r.ok:
            return None
        data = r.json()
        markets = data if isinstance(data, list) else [data]
        for m in markets:
            if not isinstance(m, dict):
                continue
            if not m.get("closed"):
                return None
            op = m.get("outcomePrices")
            if isinstance(op, str):
                import json
                try:
                    op = json.loads(op)
                except Exception:
                    return None
            if isinstance(op, list) and len(op) >= 2:
                try:
                    return "YES" if float(op[0]) > 0.5 else "NO"
                except (ValueError, TypeError):
                    return None
    except Exception as e:
        print(f"  [WARN] outcome lookup {condition_id[:16]}...: {e}", file=sys.stderr)
    return None


# ── Paper trades resolver ─────────────────────────────────────────────────────

def resolve_paper(dry_run: bool = False):
    if not PAPER_CSV.exists():
        print("paper_trades.csv not found — skipping")
        return

    rows = []
    with open(PAPER_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("paper_trades.csv is empty")
        return

    session = requests.Session()
    session.headers["Accept"] = "application/json"

    updated = 0
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for row in rows:
        if row.get("resolved", "").strip():
            continue   # already resolved

        cid = row.get("polymarket_condition_id", "").strip()
        outcome = _fetch_outcome(session, cid)
        time.sleep(0.05)

        if outcome is None:
            continue

        entry_price = float(row.get("polymarket_price") or 0)

        if outcome == "YES":
            pnl = round(1.0 - entry_price, 4)  # paper trade: 1 share, win = $1
        else:
            pnl = round(-entry_price, 4)        # lose the stake

        row["resolved"] = now_str
        row["outcome"]  = outcome
        row["pnl_paper"] = pnl
        updated += 1

        direction = row.get("direction", "")
        print(f"  PAPER  {direction[:40]:<40}  {outcome}  pnl={pnl:+.4f}")

    if dry_run:
        print(f"\n[DRY RUN] Would resolve {updated} paper trade(s)")
        return

    if updated:
        with open(PAPER_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=PAPER_HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"  Resolved {updated} paper trade(s) → {PAPER_CSV}")
    else:
        print("  No new paper trade resolutions")


# ── Live trades resolver ──────────────────────────────────────────────────────

def resolve_live(dry_run: bool = False):
    if not LIVE_CSV.exists():
        print("live_trades.csv not found — skipping")
        return

    rows = []
    with open(LIVE_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("live_trades.csv is empty")
        return

    session = requests.Session()
    session.headers["Accept"] = "application/json"

    updated = 0
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for row in rows:
        if row.get("resolved", "").strip():
            continue
        if row.get("status", "").strip() in ("FAILED", "DRY_RUN"):
            continue   # nothing to resolve

        cid = row.get("condition_id", "").strip()
        outcome = _fetch_outcome(session, cid)
        time.sleep(0.05)

        if outcome is None:
            continue

        try:
            entry_price = float(row.get("price") or 0)
            shares      = float(row.get("shares") or 0)
        except (ValueError, TypeError):
            continue

        if outcome == "YES":
            # Win: each share pays $1.00, we paid entry_price per share
            pnl = round((1.0 - entry_price) * shares, 2)
        else:
            # Loss: lose entry_price × shares (our stake)
            pnl = round(-entry_price * shares, 2)

        row["resolved"] = now_str
        row["outcome"]  = outcome
        row["pnl"]      = pnl
        updated += 1

        market = row.get("market", "")[:45]
        print(f"  LIVE   {market:<45}  {outcome}  pnl={pnl:+.2f}")

    if dry_run:
        print(f"\n[DRY RUN] Would resolve {updated} live trade(s)")
        return

    if updated:
        with open(LIVE_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LIVE_HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"  Resolved {updated} live trade(s) → {LIVE_CSV}")
    else:
        print("  No new live trade resolutions")


# ── Clear DRY_RUN rows ────────────────────────────────────────────────────────

def clear_dryruns():
    """
    Mark every DRY_RUN row in live_trades.csv as resolved=yes, outcome=DRYRUN,
    pnl=0 so they no longer block real trading via the open-positions cap.
    """
    if not LIVE_CSV.exists():
        print("live_trades.csv not found — nothing to clear")
        return

    with open(LIVE_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    cleared = 0
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    for row in rows:
        if row.get("status", "").strip() != "DRY_RUN":
            continue
        if row.get("resolved", "").strip():
            continue   # already cleared
        row["resolved"] = now_str
        row["outcome"]  = "DRYRUN"
        row["pnl"]      = 0
        cleared += 1

    if cleared:
        with open(LIVE_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LIVE_HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"  Cleared {cleared} DRY_RUN row(s) → {LIVE_CSV}")
    else:
        print("  No DRY_RUN rows to clear")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Resolve paper and live trades")
    ap.add_argument("--paper",         action="store_true", help="Paper trades only")
    ap.add_argument("--live",          action="store_true", help="Live trades only")
    ap.add_argument("--dry-run",       action="store_true", help="Preview only, no writes")
    ap.add_argument("--clear-dryruns", action="store_true",
                    help="Mark all DRY_RUN rows in live_trades.csv as resolved=DRYRUN, pnl=0")
    args = ap.parse_args()

    print(f"Resolver  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
          + ("  [DRY RUN]" if args.dry_run else ""))

    if args.clear_dryruns:
        print("\n── Clearing DRY_RUN rows ────────────")
        clear_dryruns()
        return

    do_paper = args.paper or (not args.paper and not args.live)
    do_live  = args.live  or (not args.paper and not args.live)

    if do_paper:
        print("\n── Paper trades ─────────────────────")
        resolve_paper(dry_run=args.dry_run)

    if do_live:
        print("\n── Live trades ──────────────────────")
        resolve_live(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
