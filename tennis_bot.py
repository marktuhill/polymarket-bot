#!/usr/bin/env python3
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

"""
tennis_bot.py — Autonomous tennis value trading bot.

Extends signal_engine.py: scrapes OLBG tips, finds matching Polymarket
markets, computes value gaps, and places real orders when signals fire.

Usage:
  python tennis_bot.py --once                           # run once, dry-run
  python tennis_bot.py --once --live                    # run once, REAL MONEY
  python tennis_bot.py --schedule 08:00,14:00           # daily schedule
  python tennis_bot.py --dry-run                        # force dry-run mode
  python tennis_bot.py --live --schedule 08:00,14:00    # LIVE scheduled bot
  python tennis_bot.py --test-player "Mirra Andreeva"   # pipeline test
"""

import asyncio
import csv
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT      = Path(__file__).parent
DATA_DIR  = ROOT / "data"
LIVE_CSV  = DATA_DIR / "live_trades.csv"
KILL_FILE = DATA_DIR / "KILL_SWITCH"
TG_OFFSET = DATA_DIR / "tg_offset.txt"

DATA_DIR.mkdir(exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(ROOT / "logs" / "tennis_bot.log",
                            encoding="utf-8", errors="replace"),
    ],
)
log = logging.getLogger("tennis_bot")
(ROOT / "logs").mkdir(exist_ok=True)

# ── Config ────────────────────────────────────────────────────────────────────

import config_tennis as cfg

# ── Executor (imported after config so .env is loaded) ────────────────────────

import python_executor as _executor

# ── Signal engine pieces ──────────────────────────────────────────────────────

sys.path.insert(0, str(ROOT / "scrapers"))
from scrape_olbg import scrape as olbg_scrape, TipSelection, TipsterRecord
from signal_engine import (
    find_pm_market, compute_signal, Signal, PMMarket,
    _load_env, _extract_yes_price, _get_price,
    MIN_STAR_PCT, MIN_ODDS, MAX_PM_PRICE, MIN_MARKET_PRICE,
    format_high_alert, format_medium_summary,
    gather_supplementary_picks, resolve_sources,
    is_tier_1,
    load_active_outright_markets, match_market_by_surname,
    format_market_universe,
)

# ── CSV columns ───────────────────────────────────────────────────────────────

LIVE_HEADERS = [
    "timestamp", "condition_id", "market", "direction",
    "price", "shares", "stake_usd", "confidence", "signal_sources",
    "order_id", "status", "resolved", "outcome", "pnl",
]

# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg(msg: str) -> bool:
    token   = cfg.TELEGRAM_BOT_TOKEN
    chat_id = cfg.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
        return r.ok and r.json().get("ok", False)
    except Exception as e:
        log.warning(f"Telegram: {e}")
        return False


def _tg_check_kill() -> bool:
    """Poll Telegram for /kill command. Returns True if kill was requested."""
    token   = cfg.TELEGRAM_BOT_TOKEN
    chat_id = str(cfg.TELEGRAM_CHAT_ID)
    if not token or not chat_id:
        return False

    offset = 0
    if TG_OFFSET.exists():
        try:
            offset = int(TG_OFFSET.read_text().strip())
        except Exception:
            pass

    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 1, "limit": 20},
            timeout=8,
        )
        if not r.ok:
            return False
        updates = r.json().get("result", [])
        kill_requested = False
        last_id = offset
        for upd in updates:
            uid = upd.get("update_id", 0)
            last_id = max(last_id, uid + 1)
            msg = upd.get("message", {})
            text = msg.get("text", "")
            from_chat = str(msg.get("chat", {}).get("id", ""))
            if text.strip().lower() == "/kill" and from_chat == chat_id:
                kill_requested = True
        TG_OFFSET.write_text(str(last_id))
        return kill_requested
    except Exception:
        return False

# ── Kill switch ───────────────────────────────────────────────────────────────

def _check_kill_switch() -> bool:
    """Return True if kill switch is active (file or Telegram /kill)."""
    if KILL_FILE.exists():
        log.warning("KILL_SWITCH file found — halting bot.")
        return True
    if _tg_check_kill():
        log.warning("/kill received via Telegram — halting bot.")
        KILL_FILE.touch()   # persist so future runs also halt
        return True
    return False

# ── P&L tracker ───────────────────────────────────────────────────────────────

def _daily_pnl() -> tuple[float, int, int]:
    """
    Read live_trades.csv and return (daily_loss_today, open_count, total_count).
    daily_loss_today = sum of negative pnl values resolved today (losses only).
    """
    if not LIVE_CSV.exists():
        return 0.0, 0, 0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily_loss = 0.0
    open_count = 0
    total = 0

    try:
        with open(LIVE_CSV, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total += 1
                resolved = row.get("resolved", "").strip()
                pnl_str  = row.get("pnl", "").strip()
                status   = row.get("status", "").strip()

                # DRY_RUN rows never count as open positions
                if status == "DRY_RUN":
                    continue

                if not resolved:
                    open_count += 1
                elif resolved.startswith(today) and pnl_str:
                    try:
                        pnl = float(pnl_str)
                        if pnl < 0:
                            daily_loss += abs(pnl)
                    except ValueError:
                        pass
    except Exception as e:
        log.warning(f"P&L read error: {e}")

    return daily_loss, open_count, total


def _open_condition_ids() -> set[str]:
    """Return set of condition_ids that have open (unresolved) live positions.
    DRY_RUN rows are excluded — they never count as open positions."""
    ids: set[str] = set()
    if not LIVE_CSV.exists():
        return ids
    try:
        with open(LIVE_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status", "").strip() == "DRY_RUN":
                    continue
                if not row.get("resolved", "").strip():
                    ids.add(row.get("condition_id", ""))
    except Exception:
        pass
    return ids

# ── Order placement ───────────────────────────────────────────────────────────

def _execute_signal(sig: Signal, dry_run: bool) -> dict:
    """
    Place a YES limit order for a signal (or simulate if dry_run).
    Returns a row dict for live_trades.csv.
    """
    tip   = sig.tip
    pm    = sig.pm
    price = pm.yes_price

    stake  = cfg.STAKE_HIGH if sig.confidence == "HIGH" else cfg.STAKE_MEDIUM
    stake  = min(stake, cfg.MAX_POSITION_SIZE)
    shares = int(max(round(stake / price), cfg.MIN_SHARES))
    actual_stake = round(shares * price, 2)

    prefix = "[DRY RUN] " if dry_run else ""

    if dry_run:
        order_id = f"DRY_{int(time.time())}"
        status   = "DRY_RUN"
        log.info(
            f"{prefix}WOULD BUY {shares} shares of YES @ {price:.3f} "
            f"= ${actual_stake:.2f}  [{pm.question[:55]}]"
        )
    else:
        log.info(f"Placing BUY: {shares}x YES @ {price:.3f}  [{pm.question[:55]}]")
        order_id = _executor.place_order("BUY", pm.yes_token_id, price, shares)
        status   = "PLACED" if order_id else "FAILED"

    # Telegram confirmation
    if status in ("PLACED", "DRY_RUN"):
        tg_msg = (
            f"{prefix}✅ ORDER {'PLACED' if not dry_run else 'SIMULATED'}\n"
            f"{pm.question[:70]}\n"
            f"Bought {shares} shares @ {price*100:.1f}c = ${actual_stake:.2f}\n"
            f"Signal: {sig.confidence}  Gap: +{sig.value_gap_pp:.1f}pp\n"
            f"Order ID: {order_id}"
        )
    else:
        tg_msg = (
            f"❌ ORDER FAILED\n"
            f"{pm.question[:70]}\n"
            f"Attempted {shares}x @ {price*100:.1f}c"
        )

    _tg(tg_msg)
    log.info(f"Telegram: {tg_msg[:80]}")

    tipster = sig.tipster
    src_str = "+".join(sig.sources)
    star_str = f"OLBG {round((tip.star_rating_pct or 0)/20)}★"
    sources = f"{src_str}  [{sig.label}]  {star_str}" + (
        f"  tipster:{tipster.name}" if tipster else ""
    )

    return {
        "timestamp":      datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "condition_id":   pm.condition_id,
        "market":         pm.question[:80],
        "direction":      f"{tip.selection} {tip.market}",
        "price":          price,
        "shares":         shares,
        "stake_usd":      actual_stake,
        "confidence":     sig.confidence,
        "signal_sources": sources,
        "order_id":       order_id,
        "status":         status,
        "resolved":       "",
        "outcome":        "",
        "pnl":            "",
    }


def _log_trade(row: dict):
    write_header = not LIVE_CSV.exists()
    with open(LIVE_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LIVE_HEADERS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

# ── Main engine ───────────────────────────────────────────────────────────────

def run_bot(dry_run: bool, verbose: bool = False):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mode  = "DRY RUN" if dry_run else "LIVE"
    log.info(f"{'='*60}")
    log.info(f"Tennis bot  {today}  [{mode}]")
    log.info(f"{'='*60}")

    # ── Kill switch ────────────────────────────────────────────────────────
    if _check_kill_switch():
        _tg("🛑 Tennis bot halted by KILL_SWITCH")
        return

    # ── Wallet check ───────────────────────────────────────────────────────
    if not dry_run:
        bal = _executor.get_usdc_balance()
        if bal is not None:
            log.info(f"USDC balance: ${bal:.2f}")
            if bal < 10:
                log.warning(f"Balance too low (${bal:.2f}) — skipping run")
                _tg(f"⚠️ Tennis bot: USDC balance too low (${bal:.2f})")
                return
        else:
            log.warning("Could not fetch USDC balance — proceeding anyway")

    # ── P&L / position check ───────────────────────────────────────────────
    daily_loss, open_count, _ = _daily_pnl()
    open_ids = _open_condition_ids()

    if daily_loss >= cfg.MAX_DAILY_LOSS:
        msg = f"🛑 Daily loss limit hit (${daily_loss:.2f} ≥ ${cfg.MAX_DAILY_LOSS}) — bot halted"
        log.warning(msg)
        _tg(msg)
        KILL_FILE.touch()
        return

    log.info(f"Daily loss so far: ${daily_loss:.2f} / ${cfg.MAX_DAILY_LOSS}  |  Open positions: {open_count}")

    # ── Step 1: Scrape OLBG ────────────────────────────────────────────────
    log.info("Scraping OLBG...")
    try:
        all_tips: list[TipSelection] = asyncio.run(olbg_scrape(deep=False))
    except Exception as e:
        log.error(f"OLBG scrape failed: {e}")
        _tg(f"🎾 Tennis bot ERROR\nOLBG scrape failed: {e}")
        return

    log.info(f"  {len(all_tips)} raw tips")

    # ── Step 2: Filter (NO star filter — informational only) ───────────────
    filtered = [
        t for t in all_tips
        if re.search(r'\bwin\b', t.market, re.I)
        and t.odds_decimal is not None
        and t.odds_decimal >= MIN_ODDS
    ]
    log.info(f"  {len(filtered)} pass filters (no star filter, win market, odds≥{MIN_ODDS})")

    # ── Step 3a: Scrape supplementary sources (always, for visibility) ─────
    log.info("Scraping supplementary sources...")
    supplementary = gather_supplementary_picks(verbose=verbose)
    log.info(f"  LastWord: {len(supplementary['lastword'])}  |  "
             f"TennGrand: {len(supplementary['tenngrand'])}  |  "
             f"Tennisnerd: {len(supplementary['tennisnerd'])}")

    # Log cross-references for ALL OLBG tips (even those that didn't pass star filter),
    # so we can see when low-star tips are backed by other sources.
    if verbose or not filtered:
        for t in all_tips[:8]:
            srcs = resolve_sources(t.selection, supplementary)
            if len(srcs) >= 2:
                stars_disp = round((t.star_rating_pct or 0) / 20)
                log.info(f"  cross-ref: {t.selection}  {stars_disp}★  "
                         f"sources={srcs}")

    if not filtered:
        log.info("No tips passed filters — nothing to trade")
        return

    # ── Step 3b: Bulk-load active tier-1 Polymarket outright markets ───────
    log.info("Loading active tier-1 Polymarket markets...")
    session = requests.Session()
    session.headers["Accept"] = "application/json"

    market_index = load_active_outright_markets(session, verbose=verbose)
    log.info(f"  Loaded {len(market_index)} active tier-1 outright markets")
    print(format_market_universe(market_index))

    # ── Step 3c: Match OLBG tips against the pre-loaded market index ───────
    log.info("Matching tips against market index...")

    signals:  list[Signal] = []
    no_match: list[str]    = []

    for tip in filtered:
        srcs = resolve_sources(tip.selection, supplementary)
        if verbose:
            log.info(f"  → {tip.selection}  ({tip.match_name})  "
                     f"odds={tip.odds_decimal}  stars={tip.star_rating_pct}%  "
                     f"sources={srcs}")
        pm = match_market_by_surname(tip, market_index)
        if pm is None:
            no_match.append(f"{tip.selection} ({tip.match_name[:30]})")
            continue
        end_d = (pm.end_date or "?")[:10]
        log.info(f"    matched: {pm.question[:50]}  "
                 f"price={(pm.yes_price or 0)*100:.1f}c  end={end_d}")

        sig = compute_signal(tip, pm, sources=srcs)
        if sig:
            # Optional REQUIRE_MULTI_SOURCE filter
            if getattr(cfg, "REQUIRE_MULTI_SOURCE", False) and len(sig.sources) < 2:
                log.info(f"    skipped {tip.selection}: REQUIRE_MULTI_SOURCE on, "
                         f"only {len(sig.sources)} source")
                continue
            signals.append(sig)
            if verbose:
                log.info(f"    SIGNAL {sig.confidence}  [{sig.label}]: "
                         f"gap={sig.value_gap_pp}pp")

    n_matched = len(filtered) - len(no_match)
    log.info(f"  {n_matched}/{len(filtered)} tips matched  |  {len(signals)} signals")
    if no_match:
        log.info(f"  No PM market: {', '.join(no_match[:3])}")

    # ── Step 4: Execute signals ────────────────────────────────────────────
    placed = 0
    for sig in signals:
        pm = sig.pm

        # Pre-trade checks
        if open_count >= cfg.MAX_OPEN_POSITIONS:
            log.info(f"  Skipping {sig.tip.selection}: max open positions ({cfg.MAX_OPEN_POSITIONS}) reached")
            continue
        if pm.condition_id in open_ids:
            log.info(f"  Skipping {sig.tip.selection}: position already open for {pm.condition_id[:16]}...")
            continue
        if (pm.yes_price or 0) >= MAX_PM_PRICE:
            log.info(f"  Skipping {sig.tip.selection}: PM price {pm.yes_price:.3f} ≥ {MAX_PM_PRICE}")
            continue
        if pm.yes_price is None or pm.yes_price < cfg.MIN_MARKET_PRICE:
            log.info(f"  Skipping {sig.tip.selection} — PM price "
                     f"{(pm.yes_price or 0)*100:.2f}c below minimum "
                     f"{cfg.MIN_MARKET_PRICE*100:.0f}c (resolved/illiquid)")
            continue

        row = _execute_signal(sig, dry_run=dry_run)
        _log_trade(row)

        if row["status"] not in ("FAILED",):
            open_count += 1
            open_ids.add(pm.condition_id)
            placed += 1

    # ── Step 5: Daily summary ──────────────────────────────────────────────
    daily_loss, open_count, total = _daily_pnl()
    summary = (
        f"{'[DRY RUN] ' if dry_run else ''}🎾 Tennis Bot Summary — {today}\n"
        f"Signals: {len(signals)} | Placed: {placed}\n"
        f"Open positions: {open_count} | Total trades: {total}\n"
        f"Daily loss: ${daily_loss:.2f} / ${cfg.MAX_DAILY_LOSS}"
    )
    log.info(summary)
    _tg(summary)

    # Echo a clean table to stdout
    print(f"\n{'='*60}")
    print(f"Tennis Bot  [{mode}]  {today}")
    print(f"{'='*60}")
    print(f"Tips scraped    : {len(all_tips)}")
    print(f"After filters   : {len(filtered)}")
    print(f"PM matched      : {n_matched}")
    print(f"Signals         : {len(signals)}")
    print(f"Orders placed   : {placed}")
    if placed:
        print(f"Live trades CSV : {LIVE_CSV}")


# ── Test-player mode (dry-run pipeline verification) ─────────────────────────

def _run_test_player(player: str, dry_run: bool, verbose: bool):
    """Inject a synthetic tip and run through the full bot pipeline."""
    if " vs " in player.lower():
        parts = re.split(r"\s+vs\.?\s+", player, maxsplit=1, flags=re.I)
        p1, p2 = parts[0].strip(), parts[1].strip()
        match_name, selection, odds = f"{p1} vs {p2}", p2, 2.50
    else:
        match_name, selection, odds = player, player, 1.50

    tip = TipSelection(
        match_name=match_name, match_url="", match_time_utc=None,
        selection=selection, market="Win Match", odds_decimal=odds,
        tips_for=8, tips_total=10, confidence_pct=80.0,
        star_rating_pct=100.0, tipsters=[],
    )

    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"\n[TEST/{mode}] Synthetic tip: {selection}  match={match_name}  "
          f"odds={odds}  stars=100%")

    # Credentials check
    creds_loaded = all([
        cfg.POLYMARKET_PK, cfg.POLYMARKET_FUNDER,
        cfg.POLYMARKET_API_KEY, cfg.POLYMARKET_API_SECRET,
        cfg.POLYMARKET_PASSPHRASE,
    ])
    print(f"[TEST] Wallet credentials loaded: {'YES' if creds_loaded else 'NO (check .env)'}")
    print(f"[TEST] .env path: {cfg.ENV_PATH}")

    # Executor import check
    try:
        client_available = _executor._get_client() is not None
        print(f"[TEST] CLOB client init: {'OK' if client_available else 'FAILED (check .env)'}")
    except Exception as e:
        print(f"[TEST] CLOB client init: FAILED — {e}")
        client_available = False

    # PM matching
    print(f"[TEST] Searching Polymarket for '{selection}'...")
    session = requests.Session()
    session.headers["Accept"] = "application/json"

    from signal_engine import find_pm_market as _fpm
    pm = _fpm(session, tip, verbose=verbose)

    if pm is None:
        print(f"[TEST] No PM market found for '{selection}'")
        return

    print(f"[TEST] Matched  : {pm.question}")
    print(f"[TEST] YES price: {pm.yes_price}  condition_id: {pm.condition_id[:20]}...")

    # Cross-reference with supplementary sources
    print("[TEST] Scraping supplementary sources for cross-reference...")
    supplementary = gather_supplementary_picks(verbose=False)
    srcs = resolve_sources(selection, supplementary)
    print(f"[TEST] Sources backing '{selection}': {srcs}")

    sig = compute_signal(tip, pm, sources=srcs)
    if sig is None:
        # Force MEDIUM for pipeline test even if below threshold
        from signal_engine import Signal as _Sig, source_label as _sl
        implied = 1.0 / odds
        gap_pp  = (implied - (pm.yes_price or 0)) * 100
        sig = _Sig(tip=tip, pm=pm, tipster=None,
                   tipster_implied_prob=round(implied, 4),
                   value_gap_pp=round(gap_pp, 1),
                   confidence="MEDIUM",
                   sources=srcs, label=_sl(srcs))
        print(f"[TEST] Gap {gap_pp:.1f}pp below threshold — forcing MEDIUM for pipeline test")
    else:
        print(f"[TEST] Signal   : {sig.confidence}  [{sig.label}]  gap={sig.value_gap_pp}pp")

    # Position sizing preview
    stake  = cfg.STAKE_HIGH if sig.confidence == "HIGH" else cfg.STAKE_MEDIUM
    stake  = min(stake, cfg.MAX_POSITION_SIZE)
    shares = int(max(round(stake / (pm.yes_price or 0.01)), cfg.MIN_SHARES))
    actual = round(shares * (pm.yes_price or 0), 2)

    print(f"\n[TEST] ORDER PREVIEW:")
    print(f"  Market    : {pm.question[:70]}")
    print(f"  Side      : BUY YES")
    print(f"  Price     : {(pm.yes_price or 0)*100:.1f}c per share")
    print(f"  Shares    : {shares}")
    print(f"  Stake     : ${actual:.2f}")
    print(f"  Stake cfg : STAKE_{sig.confidence} = ${stake}")

    if not dry_run and not client_available:
        print("[TEST] LIVE mode requested but CLOB client unavailable — aborting")
        return

    # Execute
    row = _execute_signal(sig, dry_run=dry_run)
    _log_trade(row)

    print(f"\n[TEST] live_trades.csv row written → {LIVE_CSV}")
    with open(LIVE_CSV, encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines[-2:]:
        print(f"  {line.rstrip()}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Autonomous tennis trading bot")
    ap.add_argument("--once",        action="store_true",
                    help="Run once and exit")
    ap.add_argument("--live",        action="store_true",
                    help="Enable live order placement (REAL MONEY)")
    ap.add_argument("--dry-run",     action="store_true",
                    help="Force dry-run mode (override DRY_RUN in config)")
    ap.add_argument("--schedule",    type=str, default="08:00,14:00",
                    help="Daily run times, comma-separated")
    ap.add_argument("--verbose","-v",action="store_true",
                    help="Show PM search scoring")
    ap.add_argument("--test-player", type=str, default=None, metavar="NAME",
                    help='Inject synthetic tip and test full pipeline')
    args = ap.parse_args()

    # Resolve dry_run flag: --live overrides config; --dry-run forces safe mode
    if args.live:
        dry_run = False
    elif args.dry_run:
        dry_run = True
    else:
        dry_run = cfg.DRY_RUN   # from config_tennis.py (default: True)

    if not dry_run:
        print("\n" + "!"*60)
        print("  LIVE MODE — real orders will be placed")
        print("!"*60 + "\n")

    if args.test_player:
        _run_test_player(args.test_player, dry_run=dry_run, verbose=args.verbose)
        return

    if args.once:
        run_bot(dry_run=dry_run, verbose=args.verbose)
        return

    # Scheduled mode
    try:
        import schedule as _sched
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "schedule"], check=True)
        import schedule as _sched

    import time as _time

    for t in args.schedule.split(","):
        t = t.strip()
        _sched.every().day.at(t).do(run_bot, dry_run=dry_run, verbose=args.verbose)
        log.info(f"Scheduled: {t} daily  [{('DRY RUN' if dry_run else 'LIVE')}]")

    log.info("Running. Press Ctrl+C to stop.")
    while True:
        if _check_kill_switch():
            _tg("🛑 Tennis bot halted by KILL_SWITCH")
            break
        _sched.run_pending()
        _time.sleep(30)


if __name__ == "__main__":
    main()
