# MATCH SCANNER — alerts only, no order placement
# Orders are placed manually by user on polymarket.com
# Reason: API geo-blocked, match markets need fast human judgment

"""
match_scanner.py
================
Scans Polymarket for same-day/next-day match winner markets, cross-references
against three tipster sources, and fires Telegram alerts for high-confidence
signals. Complements tennis_bot.py (which trades outright winner markets).

Run alongside tennis_bot.py:
  Window 1: python tennis_bot.py --live --schedule 08:00,14:00
  Window 2: python match_scanner.py --schedule 06:00,08:00,10:00,12:00,14:00,16:00,18:00,20:00,22:00
"""

import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# ── .env discovery ────────────────────────────────────────────────────────────
_ROOT = Path(__file__).parent
for _p in [
    Path(r"C:\Users\markt\polymarket-tennis-bot\.env"),
    Path(r"C:\Users\Mark\polymarket-tennis-bot\.env"),
    _ROOT / ".env",
]:
    if _p.exists():
        load_dotenv(_p)
        break

# ── Directories ───────────────────────────────────────────────────────────────
(_ROOT / "logs").mkdir(exist_ok=True)
(_ROOT / "data").mkdir(exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger().addHandler(
    logging.FileHandler(_ROOT / "logs" / "match_scanner.log",
                        encoding="utf-8", errors="replace")
)
log = logging.getLogger("match_scanner")

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")
GAMMA_API      = "https://gamma-api.polymarket.com"
MATCH_CSV      = _ROOT / "data" / "match_trades.csv"

MATCH_CSV_HEADERS = [
    "date_logged", "tournament", "match", "player_picked", "opponent",
    "tip_odds", "tipster_implied", "polymarket_price", "value_gap_pp",
    "confidence", "sources", "condition_id", "resolved", "outcome", "pnl_paper",
]

MIN_PM_PRICE  = 0.03
MAX_PM_PRICE  = 0.80
SCAN_WINDOW_H = 48   # only markets resolving within this many hours

# Scrapers path
sys.path.insert(0, str(_ROOT / "scrapers"))


# ─────────────────────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────────────────────

def _tg(msg: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg},
            timeout=10,
        )
        return r.ok
    except Exception as e:
        log.warning(f"Telegram: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Surname matching
# ─────────────────────────────────────────────────────────────────────────────

def _surname(name: str) -> str:
    parts = name.strip().split()
    return parts[-1].lower() if parts else name.lower()


def _surnames_match(a: str, b: str) -> bool:
    sa, sb = _surname(a), _surname(b)
    if sa == sb:
        return True
    # Prefix match (e.g. "Svitolina" vs "Svitolyna")
    n = min(len(sa), len(sb))
    if n >= 5 and sa[:n] == sb[:n]:
        return True
    if n >= 4 and (sa.startswith(sb[:4]) or sb.startswith(sa[:4])):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Parse player names from market question
# ─────────────────────────────────────────────────────────────────────────────

def _parse_players(question: str) -> Optional[tuple[str, str]]:
    """
    Extract (player_a, player_b) from a market question.
    player_a is the YES side (who the market thinks will win).
    """
    # "Will X beat/defeat Y?" form
    m = re.search(
        r"Will\s+(.+?)\s+(?:beat|defeat|win\s+against)\s+(.+?)\??$",
        question, re.I,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # "X vs Y" / "X v Y" form (with optional trailing context)
    m = re.search(r"(.+?)\s+vs?\.?\s+(.+?)(?:\s*[-—(]|\?|$)", question, re.I)
    if m:
        a = re.sub(r"^[Ww]ill\s+", "", m.group(1).strip())
        b = m.group(2).strip().rstrip("?")
        return a, b

    return None


def _is_match_question(question: str) -> bool:
    return bool(re.search(r"\bvs?\.?\b", question, re.I))


# ─────────────────────────────────────────────────────────────────────────────
# Load active Polymarket match markets (resolving within 48h)
# ─────────────────────────────────────────────────────────────────────────────

def load_match_markets() -> list[dict]:
    now     = datetime.now(timezone.utc)
    cutoff  = now + timedelta(hours=SCAN_WINDOW_H)
    session = requests.Session()
    session.headers["User-Agent"] = "tennis-match-scanner/1.0"
    markets = []

    for page in range(5):
        for attempt in range(3):
            try:
                r = session.get(
                    f"{GAMMA_API}/events",
                    params={
                        "tag_slug": "tennis",
                        "active":   "true",
                        "closed":   "false",
                        "limit":    100,
                        "offset":   page * 100,
                    },
                    timeout=30,
                )
                if r.status_code != 200:
                    log.warning(f"Gamma page {page}: HTTP {r.status_code}")
                    break
                events = r.json()
                break
            except Exception as e:
                log.warning(f"Gamma page {page} attempt {attempt}: {e}")
                time.sleep(2 ** attempt)
                events = []

        if not events:
            break

        for event in events:
            tournament = event.get("title", "")
            for mkt in event.get("markets", []):
                question = mkt.get("question", "")
                if not _is_match_question(question):
                    continue

                # End date
                end_str = mkt.get("endDate") or mkt.get("end_date_iso") or ""
                if not end_str:
                    continue
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                except Exception:
                    continue

                if end_dt <= now or end_dt > cutoff:
                    continue

                # YES price
                raw_prices = mkt.get("outcomePrices") or mkt.get("outcome_prices", "[]")
                if isinstance(raw_prices, str):
                    try:
                        raw_prices = json.loads(raw_prices)
                    except Exception:
                        continue
                try:
                    yes_price = float(raw_prices[0])
                except (IndexError, ValueError, TypeError):
                    continue

                if not (MIN_PM_PRICE <= yes_price <= MAX_PM_PRICE):
                    continue

                players = _parse_players(question)
                if not players:
                    continue
                player_a, player_b = players

                # Token IDs
                raw_tokens = mkt.get("clobTokenIds") or mkt.get("clob_token_ids", "[]")
                if isinstance(raw_tokens, str):
                    try:
                        raw_tokens = json.loads(raw_tokens)
                    except Exception:
                        raw_tokens = []

                markets.append({
                    "question":     question,
                    "player_a":     player_a,
                    "player_b":     player_b,
                    "yes_price":    round(yes_price, 3),
                    "no_price":     round(1.0 - yes_price, 3),
                    "condition_id": mkt.get("conditionId") or mkt.get("condition_id", ""),
                    "yes_token_id": raw_tokens[0] if raw_tokens else None,
                    "no_token_id":  raw_tokens[1] if len(raw_tokens) > 1 else None,
                    "end_date":     end_dt,
                    "tournament":   tournament,
                })

        if len(events) < 100:
            break

    return markets


# ─────────────────────────────────────────────────────────────────────────────
# Collect tips from all sources
# ─────────────────────────────────────────────────────────────────────────────

def collect_tips(verbose: bool = False) -> list[dict]:
    """
    Returns normalised list of:
      {player_picked, opponent, source, confidence, tip_odds}
    tip_odds is None for sources that don't publish odds.
    """
    tips: list[dict] = []

    # LastWord
    try:
        from scrape_lastword import scrape_lastword
        picks = scrape_lastword(verbose=verbose)
        for p in picks:
            tips.append({
                "player_picked": p["selection"],
                "opponent":      None,
                "source":        "LastWord",
                "confidence":    p.get("confidence", "MEDIUM"),
                "tip_odds":      None,
            })
        log.info(f"  LastWord   : {len(picks)} picks")
    except Exception as e:
        log.warning(f"LastWord failed: {e}")

    # TennGrand — match and value kinds only
    try:
        from scrape_tenngrand import scrape_tenngrand
        picks = scrape_tenngrand(verbose=verbose)
        count = 0
        for p in picks:
            if p.get("kind") in ("match", "value"):
                tips.append({
                    "player_picked": p["selection"],
                    "opponent":      p.get("opponent"),
                    "source":        "TennGrand",
                    "confidence":    "HIGH",
                    "tip_odds":      None,
                })
                count += 1
        log.info(f"  TennGrand  : {count} match/value picks")
    except Exception as e:
        log.warning(f"TennGrand failed: {e}")

    # Tennisnerd
    try:
        from scrape_tennisnerd import scrape_tennisnerd
        picks = scrape_tennisnerd(verbose=verbose)
        for p in picks:
            tips.append({
                "player_picked": p["selection"],
                "opponent":      None,
                "source":        "Tennisnerd",
                "confidence":    p.get("confidence", "MEDIUM"),
                "tip_odds":      None,
            })
        log.info(f"  Tennisnerd : {len(picks)} picks")
    except Exception as e:
        log.warning(f"Tennisnerd failed: {e}")

    return tips


# ─────────────────────────────────────────────────────────────────────────────
# Cross-reference tips against match markets → produce signals
# ─────────────────────────────────────────────────────────────────────────────

def cross_reference(tips: list[dict], markets: list[dict]) -> list[dict]:
    """
    For each market, find which tips back player_a (YES) or player_b (NO).
    Compute source agreement and signal tier.
    """
    # Index tips by player surname
    by_surname: dict[str, list[dict]] = {}
    for tip in tips:
        sn = _surname(tip["player_picked"])
        by_surname.setdefault(sn, []).append(tip)

    signals: list[dict] = []

    for mkt in markets:
        a, b = mkt["player_a"], mkt["player_b"]

        # Find backing tips for each side
        tips_a: list[dict] = []
        tips_b: list[dict] = []
        for sn, tip_list in by_surname.items():
            if _surnames_match(sn, a):
                tips_a.extend(tip_list)
            elif _surnames_match(sn, b):
                tips_b.extend(tip_list)

        for player_picked, opponent, tips_backing, pm_price in [
            (a, b, tips_a, mkt["yes_price"]),
            (b, a, tips_b, mkt["no_price"]),
        ]:
            if not tips_backing:
                continue

            sources      = sorted({t["source"] for t in tips_backing})
            src_count    = len(sources)
            any_high     = any(t.get("confidence") == "HIGH" for t in tips_backing)

            # Tipster implied: we don't have actual odds from these scrapers.
            # Estimate based on source consensus — conservative assumption:
            # 1 source: tipster implied ~55%, 2 sources: ~60%, 3+: ~65%
            tipster_implied = {1: 0.55, 2: 0.60, 3: 0.65}.get(min(src_count, 3), 0.65)
            gap_pp = round((tipster_implied - pm_price) * 100, 1)

            # Signal tier
            if src_count >= 3 and gap_pp >= 5:
                tier = "HIGH"
            elif src_count >= 2 and gap_pp >= 3:
                tier = "HIGH"
            elif src_count >= 2 and gap_pp >= 2:
                tier = "MEDIUM"
            elif src_count == 1 and gap_pp >= 5:
                tier = "MEDIUM"
            else:
                continue   # below all thresholds

            # Override tier down if PM price is too close to fair (>75c)
            if pm_price > MAX_PM_PRICE:
                continue

            signals.append({
                "player_picked":   player_picked,
                "opponent":        opponent,
                "pm_price":        pm_price,
                "tipster_implied": tipster_implied,
                "gap_pp":          gap_pp,
                "src_count":       src_count,
                "sources":         sources,
                "tier":            tier,
                "market":          mkt,
            })

    # Deduplicate: if same player appears as signal from multiple source combos,
    # keep highest confidence
    seen: dict[str, dict] = {}
    for sig in signals:
        key = sig["market"]["condition_id"] + "|" + _surname(sig["player_picked"])
        existing = seen.get(key)
        if not existing:
            seen[key] = sig
        elif sig["tier"] == "HIGH" and existing["tier"] != "HIGH":
            seen[key] = sig
        elif sig["src_count"] > existing["src_count"]:
            seen[key] = sig

    return list(seen.values())


# ─────────────────────────────────────────────────────────────────────────────
# CSV logging
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_csv():
    if not MATCH_CSV.exists():
        with open(MATCH_CSV, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=MATCH_CSV_HEADERS).writeheader()


def log_to_csv(signals: list[dict]):
    _ensure_csv()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(MATCH_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MATCH_CSV_HEADERS)
        for sig in signals:
            mkt = sig["market"]
            writer.writerow({
                "date_logged":      now_str,
                "tournament":       mkt["tournament"],
                "match":            mkt["question"],
                "player_picked":    sig["player_picked"],
                "opponent":         sig["opponent"],
                "tip_odds":         "",
                "tipster_implied":  f"{sig['tipster_implied']:.0%}",
                "polymarket_price": sig["pm_price"],
                "value_gap_pp":     sig["gap_pp"],
                "confidence":       sig["tier"],
                "sources":          "+".join(sig["sources"]),
                "condition_id":     mkt["condition_id"],
                "resolved":         "",
                "outcome":          "",
                "pnl_paper":        "",
            })


# ─────────────────────────────────────────────────────────────────────────────
# Format Telegram alerts
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_price(p: float) -> str:
    return f"{round(p * 100)}¢"


def _fmt_end(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    delta = dt - now
    if delta.total_seconds() < 3600 * 3:
        return f"~{int(delta.total_seconds() / 60)}min"
    return dt.strftime("%H:%M UTC")


def _source_line(sources: list[str]) -> str:
    icons = {"LastWord": "LastWord ✓", "TennGrand": "TennGrand ✓", "Tennisnerd": "Tennisnerd ✓"}
    return "  ".join(icons.get(s, s + " ✓") for s in sources)


def send_alerts(signals: list[dict]) -> int:
    if not signals:
        return 0

    today = datetime.now(timezone.utc).strftime("%b %d")
    high   = [s for s in signals if s["tier"] == "HIGH"]
    medium = [s for s in signals if s["tier"] == "MEDIUM"]
    sent   = 0

    # HIGH signals — one message each
    for sig in high:
        mkt     = sig["market"]
        gap_sym = "🔴" if sig["gap_pp"] >= 5 else "🟡"
        msg = (
            f"🎾 MATCH ALERT — {today}\n\n"
            f"{mkt['player_a']} vs {mkt['player_b']}"
            + (f" — {mkt['tournament']}" if mkt['tournament'] else "") + "\n"
            f"✅ Tip: {sig['player_picked']} to WIN\n"
            f"Sources: {_source_line(sig['sources'])}\n"
            f"{'[CONFIRMED]' if sig['src_count'] >= 2 else ''}\n\n"
            f"{sig['player_picked']} price: {_fmt_price(sig['pm_price'])}  "
            f"(tipster implied: {sig['tipster_implied']:.0%})\n"
            f"Value gap: +{sig['gap_pp']}pp {gap_sym}\n\n"
            f"Resolves: {_fmt_end(mkt['end_date'])}\n"
            f"⚡ Act quickly — match market"
        )
        if _tg(msg):
            sent += 1
            log.info(f"  HIGH alert sent: {sig['player_picked']} in {mkt['question'][:60]}")
        else:
            log.info(f"  HIGH signal (no Telegram): {sig['player_picked']} — {mkt['question'][:60]}")

    # MEDIUM signals — batched
    if medium:
        lines = [f"📋 MATCH TIPS — {today}"]
        for sig in medium:
            mkt = sig["market"]
            src_str = "+".join(s[:4] for s in sig["sources"])
            lines.append(
                f"- {sig['player_picked']} over {sig['opponent'] or '?'}: "
                f"PM {_fmt_price(sig['pm_price'])} | "
                f"gap +{sig['gap_pp']}pp | {src_str}"
            )
        msg = "\n".join(lines)
        if _tg(msg):
            sent += 1
            log.info(f"  MEDIUM batch sent ({len(medium)} signals)")
        else:
            log.info(f"  MEDIUM signals (no Telegram): {len(medium)}")

    return sent


# ─────────────────────────────────────────────────────────────────────────────
# Main scan
# ─────────────────────────────────────────────────────────────────────────────

def run_match_scanner(verbose: bool = False):
    log.info("=" * 60)
    log.info(f"Match scanner  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 60)

    # 1. Collect tips
    log.info("Scraping tipster sources...")
    tips = collect_tips(verbose=verbose)
    log.info(f"  Total tips: {len(tips)}")

    if not tips:
        log.warning("No tips collected — check scraper connectivity")
        return

    # 2. Load match markets
    log.info("Loading Polymarket match markets (next 48h)...")
    markets = load_match_markets()
    log.info(f"  Found {len(markets)} active match markets resolving within 48h")

    if not markets:
        log.info("No match markets found — nothing to cross-reference")
        return

    # 3. Cross-reference
    signals = cross_reference(tips, markets)
    high    = sum(1 for s in signals if s["tier"] == "HIGH")
    medium  = sum(1 for s in signals if s["tier"] == "MEDIUM")
    log.info(f"  Signals: {high} HIGH, {medium} MEDIUM")

    if not signals:
        log.info("No signals this scan")
        return

    # 4. Log to CSV
    log_to_csv(signals)
    log.info(f"  Logged {len(signals)} signals to {MATCH_CSV.name}")

    # 5. Send Telegram alerts
    sent = send_alerts(signals)
    log.info(f"  Telegram: {sent} message(s) sent")

    # 6. Summary
    log.info("")
    log.info("Match Scanner Summary")
    log.info(f"  Tips scraped   : {len(tips)}")
    log.info(f"  Markets (48h)  : {len(markets)}")
    log.info(f"  Signals        : {high} HIGH  {medium} MEDIUM")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import schedule

    ap = argparse.ArgumentParser(description="Polymarket match market scanner — alerts only")
    ap.add_argument("--once",     action="store_true", help="Run once and exit")
    ap.add_argument("--verbose",  action="store_true", help="Verbose scraper output")
    ap.add_argument(
        "--schedule",
        type=str,
        default="06:00,08:00,10:00,12:00,14:00,16:00,18:00,20:00,22:00",
        help="Comma-separated UTC run times (default: every 2h 06:00-22:00)",
    )
    args = ap.parse_args()

    if args.once:
        run_match_scanner(verbose=args.verbose)
    else:
        times = [t.strip() for t in args.schedule.split(",")]
        for t in times:
            schedule.every().day.at(t).do(run_match_scanner, verbose=args.verbose)
        log.info(f"Match scanner scheduled: {', '.join(times)} UTC")
        log.info("Alerts only — place bets manually on polymarket.com")
        while True:
            schedule.run_pending()
            time.sleep(30)
