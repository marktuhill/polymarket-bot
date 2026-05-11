#!/usr/bin/env python3
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

"""
signal_engine.py — OLBG tennis tips → Polymarket value gap detector.

Scrapes OLBG for rated tennis tips, finds matching Polymarket markets,
computes the implied-probability gap, and fires Telegram alerts.

Usage:
  python signal_engine.py --once              # single run
  python signal_engine.py --once --verbose    # with debug output
  python signal_engine.py                     # scheduled (08:00 + 14:00 daily)
  python signal_engine.py --schedule 10:00,16:00
"""

import asyncio
import csv
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT     = Path(__file__).parent
DATA_DIR = ROOT / "data"
CSV_PATH = DATA_DIR / "paper_trades.csv"

# .env search order: try each path and use the first that exists
_ENV_CANDIDATES = [
    Path(r"C:\CUsersMarkpolymarket-momentum-bot\.env"),
    Path(r"C:\Users\markt\polymarket-momentum-bot\.env"),
    Path(r"C:\Users\Mark\polymarket-momentum-bot\.env"),
    Path(r"..\.env"),
]
ENV_PATH = next((p for p in _ENV_CANDIDATES if p.exists()), None)
if ENV_PATH is None:
    print(
        "No .env found — Telegram disabled. "
        "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID manually.",
        file=sys.stderr,
    )

# scrapers/ must be on the path so we can import scrape_olbg
sys.path.insert(0, str(ROOT / "scrapers"))
from scrape_olbg import scrape as olbg_scrape, TipSelection, TipsterRecord  # noqa: E402
from scrape_lastword import scrape_lastword           # noqa: E402
from scrape_tenngrand import scrape_tenngrand          # noqa: E402
from scrape_tennisnerd import scrape_tennisnerd        # noqa: E402

# ── APIs ──────────────────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

# ── Filters ───────────────────────────────────────────────────────────────────

MIN_STAR_PCT     = 0.0    # star filter REMOVED — stars are informational only
MIN_ODDS         = 1.30   # ignore heavy favourites (implied > 77%)
MAX_PM_PRICE     = 0.65   # skip if Polymarket already prices it as favourite
MIN_MARKET_PRICE = 0.03   # skip markets below 3c (resolved / illiquid / too far out)

# ── Signal thresholds ─────────────────────────────────────────────────────────
# Source-count + gap is the primary signal logic.  Star rating is purely a
# small (+1pp threshold reduction) tiebreaker boost if present.
#
#   sources ≥ 3 AND gap ≥ 8pp  → HIGH
#   sources ≥ 2 AND gap ≥ 6pp  → HIGH
#   sources ≥ 2 AND gap ≥ 4pp  → MEDIUM
#   sources == 1 AND gap ≥ 8pp → MEDIUM (OLBG-only needs big gap)
#   anything else              → SKIP
#
# Star boost: if star_rating_pct > 0, reduce every gap threshold by 1pp.
#
# Tournament-tier filter (TIER_1_ONLY in config_tennis.py): only consider
# Polymarket markets whose question or event title mentions a Grand Slam /
# Masters 1000 / WTA 1000 tournament.


# ── Tier-1 tournament keywords ────────────────────────────────────────────────

TIER_1 = [
    "australian open", "roland garros", "french open", "wimbledon",
    "us open",
    "indian wells", "miami open", "monte carlo", "madrid open",
    "italian open", "rome", "canadian open", "montreal", "toronto",
    "cincinnati", "shanghai", "paris masters", "paris",
    "beijing", "wuhan", "dubai", "doha", "stuttgart", "guadalajara",
    "wu han",
]


def _is_tier_1_text(text: str) -> bool:
    """True if any tier-1 keyword appears in the supplied text (lowercased)."""
    t = (text or "").lower()
    return any(kw in t for kw in TIER_1)


def is_tier_1(pm) -> bool:
    """True if the market's question or event title mentions a tier-1 event."""
    text = ((pm.question or "") + " " + getattr(pm, "event_title", "")).lower()
    return any(kw in text for kw in TIER_1)


# ── Cross-reference helpers ───────────────────────────────────────────────────

def _normalize_name(name: str) -> str:
    """Lowercase, strip accents, collapse whitespace."""
    if not name:
        return ""
    nfd = unicodedata.normalize("NFD", name)
    no_accents = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", no_accents).strip().lower()


def _surname(name: str) -> str:
    """Return the last whitespace-separated token of a normalized name."""
    parts = _normalize_name(name).split()
    return parts[-1] if parts else ""


def gather_supplementary_picks(verbose: bool = False) -> dict:
    """
    Run the Last Word + TennGrand + Tennisnerd scrapers and return a dict of
    pick lists.  Each scraper failure is non-fatal — others still contribute.
    """
    out = {"lastword": [], "tenngrand": [], "tennisnerd": []}
    try:
        out["lastword"] = scrape_lastword(verbose=verbose)
    except Exception as e:
        print(f"[WARN] LastWord scrape failed: {e}", file=sys.stderr)
    try:
        out["tenngrand"] = scrape_tenngrand(verbose=verbose)
    except Exception as e:
        print(f"[WARN] TennGrand scrape failed: {e}", file=sys.stderr)
    try:
        out["tennisnerd"] = scrape_tennisnerd(verbose=verbose)
    except Exception as e:
        print(f"[WARN] Tennisnerd scrape failed: {e}", file=sys.stderr)
    return out


def resolve_sources(selection: str, supplementary: dict) -> list[str]:
    """
    Return the list of sources backing this selection.  Always starts with
    "OLBG" (the OLBG tip is the trigger that called this).  Adds LastWord,
    TennGrand, or Tennisnerd when their picks include a surname match.
    """
    sources = ["OLBG"]
    target = _surname(selection)
    if not target:
        return sources

    lw = supplementary.get("lastword", []) or []
    if any(_surname(p.get("selection", "")) == target for p in lw):
        sources.append("LastWord")

    tg = supplementary.get("tenngrand", []) or []
    if any(_surname(p.get("selection", "")) == target for p in tg):
        sources.append("TennGrand")

    tn = supplementary.get("tennisnerd", []) or []
    if any(_surname(p.get("selection", "")) == target for p in tn):
        sources.append("Tennisnerd")

    return sources


def source_label(sources: list[str]) -> str:
    n = len(sources)
    if n >= 4:
        return "QUAD-CONFIRMED"
    if n >= 3:
        return "TRIPLE-CONFIRMED"
    if n == 2:
        return "CONFIRMED"
    return "OLBG ONLY"

# ── CSV columns ───────────────────────────────────────────────────────────────

CSV_HEADERS = [
    "date_logged", "tournament", "match_or_market", "direction",
    "olbg_stars", "olbg_odds", "tipster_name", "tipster_profit",
    "tipster_expert", "polymarket_price", "tipster_implied_prob",
    "value_gap_pp", "confidence", "source_count", "sources",
    "polymarket_condition_id",
    "resolved", "outcome", "pnl_paper",
]


# ── Telegram (standalone — no dependency on bot config) ───────────────────────

def _load_env(path: Optional[Path]) -> dict[str, str]:
    env: dict[str, str] = {}
    if path is None or not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _send_telegram(msg: str, token: str, chat_id: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
        return r.ok and r.json().get("ok", False)
    except Exception as e:
        print(f"[WARN] Telegram: {e}", file=sys.stderr)
        return False


# ── Polymarket lookup ─────────────────────────────────────────────────────────

@dataclass
class PMMarket:
    condition_id: str
    question: str
    yes_token_id: str
    yes_price: Optional[float]
    end_date: Optional[str]
    event_title: str = ""


def _search_markets(session: requests.Session, query: str) -> list[dict]:
    """
    Search Gamma API for active tennis markets matching query.

    Uses /events?tag_slug=tennis (same as the existing scraper) because
    /markets ignores tag_slug and returns unrelated results.
    Extracts the nested market list from each event.
    """
    for attempt in range(2):
        try:
            r = session.get(
                f"{GAMMA_API}/events",
                params={
                    "tag_slug": "tennis",
                    "active":   "true",
                    "closed":   "false",
                    "limit":    30,
                    "search":   query,
                },
                timeout=12,
            )
            if r.status_code == 200:
                events = r.json()
                if not isinstance(events, list):
                    events = events.get("data", []) if isinstance(events, dict) else []
                markets: list[dict] = []
                for ev in events:
                    for m in (ev.get("markets") or []):
                        # Carry event title into the market dict for context
                        m["_event_title"] = ev.get("title", "")
                        markets.append(m)
                return markets
        except Exception as e:
            if attempt == 0:
                time.sleep(1)
            else:
                print(f"[WARN] Gamma search '{query}': {e}", file=sys.stderr)
    return []


def _get_price(session: requests.Session, token_id: str) -> Optional[float]:
    """Last-trade price for a CLOB token; falls back to orderbook mid."""
    try:
        r = session.get(
            f"{CLOB_API}/last-trade-price",
            params={"token_id": token_id},
            timeout=10,
        )
        if r.status_code == 200:
            val = r.json().get("price")
            if val is not None:
                return float(val)
    except Exception:
        pass

    try:
        r = session.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        if r.status_code == 200:
            book = r.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids and asks:
                return (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
    except Exception:
        pass

    return None


def _yes_token(market: dict) -> Optional[str]:
    tok = market.get("clobTokenIds")
    if isinstance(tok, str):
        try:
            tok = json.loads(tok)
        except Exception:
            return None
    if isinstance(tok, list) and tok:
        return str(tok[0])
    return None


def _extract_yes_price(market: dict) -> Optional[float]:
    """
    Extract the YES price directly from the Gamma market's outcomePrices field.

    outcomePrices is a JSON-encoded array where index 0 = YES price,
    index 1 = NO price (e.g. '["0.65", "0.35"]').  Available on every
    active market returned by /events — no extra CLOB call needed.
    """
    op = market.get("outcomePrices")
    if isinstance(op, str):
        try:
            op = json.loads(op)
        except Exception:
            return None
    if isinstance(op, list) and op:
        try:
            return float(op[0])
        except (ValueError, TypeError):
            return None
    return None


def _score_market(question: str, selected_player: str,
                  match_name: str, event_title: str = "") -> float:
    """
    Score 0–1 for how well a Polymarket question matches an OLBG tip.

    Rules:
    - Selected player must appear in the question (0.0 if not).
    - For match tips ("X vs Y"): both players should appear.
    - Prefer questions that contain "win" (outright win markets).
    - Penalise if the question looks like a different market type
      (e.g. "reach final", "advance") unless market is "Win".
    """
    q = question.lower()
    ctx = q + " " + event_title.lower()   # question + event title for scoring
    player_lower = selected_player.lower()

    # Player must be present in question or event title
    player_words = player_lower.split()
    surname = player_words[-1] if player_words else ""
    if surname not in ctx:
        return 0.0

    score = 0.5  # surname match baseline

    # Full name match is stronger
    if player_lower in ctx:
        score = 0.75

    # Win-type question boost
    if re.search(r'\bwin\b', q):
        score = min(1.0, score + 0.15)

    # For match tips, reward markets that also contain the opponent
    if " vs " in match_name.lower():
        parts = re.split(r"\s+vs\.?\s+", match_name, maxsplit=1, flags=re.I)
        if len(parts) == 2:
            opponent = parts[0] if parts[1].lower().split()[-1] in ctx else parts[1]
            opponent_surname = opponent.strip().split()[-1].lower()
            if opponent_surname in ctx:
                score = min(1.0, score + 0.1)

    # Penalise reach/advance/final markets — we only want win markets
    if re.search(r'\breach\b|\badvance\b|\bfinal\b|\bsemi\b|\bquarter\b', q):
        score *= 0.4

    return score


# ── Proactive bulk-load of all active tier-1 outright markets ────────────────

# Captures "Will X win/be/reach ..." — strips quotes/diacritics tolerated
_PLAYER_FROM_Q_RE = re.compile(
    r"^Will\s+(.+?)\s+(?:win|be|reach|advance|make)\b",
    re.I,
)


def load_active_outright_markets(session: requests.Session,
                                 verbose: bool = False) -> dict[str, PMMarket]:
    """
    Bulk-load every active tier-1 Polymarket tennis outright market and
    return a dict keyed by player surname (lowercased, accent-stripped).

    Filters applied:
      - active=true, closed=false (live markets only)
      - YES price in [MIN_MARKET_PRICE, MAX_PM_PRICE)
      - end_date is in the future
      - tier-1 tournament keyword in question or event title
      - question matches "Will <Name> win/be/reach …"

    When multiple markets exist for the same surname (duplicate slugs,
    multiple tournaments), the one with the **soonest** future end_date
    wins — that's the most actionable for value-trading right now.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    candidates: dict[str, list[PMMarket]] = {}
    pages_fetched = 0

    for page in range(5):                       # up to 500 events
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
                timeout=15,
            )
            if r.status_code != 200:
                break
            events = r.json()
            if not isinstance(events, list) or not events:
                break
        except Exception as e:
            print(f"[WARN] Gamma bulk-load page {page}: {e}", file=sys.stderr)
            break

        pages_fetched += 1

        for ev in events:
            ev_title = ev.get("title", "")
            for m in (ev.get("markets") or []):
                q = m.get("question") or ""

                # Tier-1 gate (event title or question must mention a tier-1 event)
                if not _is_tier_1_text(q + " " + ev_title):
                    continue

                # Player extraction
                pm_match = _PLAYER_FROM_Q_RE.search(q)
                if not pm_match:
                    continue
                player_name = pm_match.group(1).strip()
                surname_key = _surname(player_name)
                if not surname_key or len(surname_key) < 3:
                    continue

                # Price filter
                yes_p = _extract_yes_price(m)
                if yes_p is None or yes_p < MIN_MARKET_PRICE or yes_p >= MAX_PM_PRICE:
                    continue

                # End-date filter — must be in future
                end_d = m.get("endDate") or ""
                if not end_d or end_d < now_iso:
                    continue

                yes_tok = _yes_token(m)
                if not yes_tok:
                    continue

                pm_obj = PMMarket(
                    condition_id=m.get("conditionId") or m.get("condition_id", ""),
                    question=q,
                    yes_token_id=yes_tok,
                    yes_price=yes_p,
                    end_date=end_d,
                    event_title=ev_title,
                )
                candidates.setdefault(surname_key, []).append(pm_obj)

        time.sleep(0.05)

    # Pick the soonest-ending market per surname
    result: dict[str, PMMarket] = {}
    for surname, mkts in candidates.items():
        mkts.sort(key=lambda x: (x.end_date or "9999"))
        result[surname] = mkts[0]

    if verbose:
        print(f"[Gamma] bulk-load: {pages_fetched} pages → "
              f"{len(result)} tier-1 outright markets", file=sys.stderr)

    return result


def match_market_by_surname(tip: TipSelection,
                            market_index: dict[str, PMMarket]
                            ) -> Optional[PMMarket]:
    """Look up a Polymarket market for an OLBG tip by surname."""
    key = _surname(tip.selection)
    return market_index.get(key) if key else None


def format_market_universe(market_index: dict[str, PMMarket]) -> str:
    """Render the loaded market universe as a printable table."""
    if not market_index:
        return "  (no tier-1 outright markets loaded)"

    rows = sorted(market_index.values(), key=lambda m: m.yes_price or 0)
    lines = [
        f"Active Polymarket tennis outright markets (tier-1, "
        f"{int(MIN_MARKET_PRICE*100)}c-{int(MAX_PM_PRICE*100)}c):",
        "─" * 80,
    ]
    for m in rows:
        mm = _PLAYER_FROM_Q_RE.search(m.question)
        player = mm.group(1).strip() if mm else "?"
        end_d  = (m.end_date or "?")[:10]
        tourney = (m.event_title or m.question or "?")[:32]
        lines.append(
            f"  {(m.yes_price or 0)*100:5.1f}c  {player[:22]:<22}  "
            f"{tourney:<32}  ends {end_d}"
        )
    return "\n".join(lines)


def find_pm_market(session: requests.Session, tip: TipSelection,
                   verbose: bool = False) -> Optional[PMMarket]:
    """Find the best-matching active Polymarket market for an OLBG tip."""
    selection  = tip.selection    # "Iga Swiatek"
    match_name = tip.match_name   # "Cocciaretto vs Iga Swiatek"

    # Build a ranked list of search queries: specific → broad
    queries: list[str] = [selection]
    if " vs " in match_name.lower():
        parts = re.split(r"\s+vs\.?\s+", match_name, maxsplit=1, flags=re.I)
        if len(parts) == 2:
            a, b = parts[0].strip(), parts[1].strip()
            queries += [f"{a} {b}", f"{b} {a}"]
    else:
        # Tournament market: "French Open Women Sabalenka"
        queries.append(f"{match_name} {selection}")

    best: Optional[dict] = None
    best_score = 0.0
    now_iso = datetime.now(timezone.utc).isoformat()

    for query in queries:
        results = _search_markets(session, query)
        time.sleep(0.12)

        for m in results:
            q = m.get("question") or ""
            ev_title = m.get("_event_title", "")

            # Hard filter: skip markets with YES price below MIN_MARKET_PRICE
            # (resolved / illiquid / too far out)
            yes_p = _extract_yes_price(m)
            if yes_p is not None and yes_p < MIN_MARKET_PRICE:
                if verbose:
                    end_d = (m.get("endDate") or "?")[:10]
                    print(f"      skip-price-low  yes={yes_p:.3f}  end={end_d}  "
                          f"{q[:50]}", file=sys.stderr)
                continue

            score = _score_market(q, selection, match_name, ev_title)

            # End-date penalty: if endDate < today, knock the score way down
            end_date = m.get("endDate") or ""
            past = bool(end_date and end_date < now_iso)
            if past:
                score -= 50.0   # ensures any active market beats a resolved one

            if verbose:
                end_d = (end_date or "?")[:10]
                marker = " (PAST)" if past else ""
                yes_disp = f"{yes_p:.3f}" if yes_p is not None else "?"
                print(f"      {score:.2f}  yes={yes_disp}  end={end_d}{marker}  "
                      f"{q[:50]}  [{ev_title[:25]}]", file=sys.stderr)

            if score > best_score:
                best_score = score
                best = m

        if best_score >= 0.75:
            break   # confident enough, stop searching

    if best_score < 0.50 or best is None:
        return None

    yes_tok = _yes_token(best)
    if not yes_tok:
        return None

    # Prefer outcomePrices already in the Gamma market payload (no extra call).
    # Fall back to CLOB last-trade-price only if missing.
    price = _extract_yes_price(best)
    if price is None:
        price = _get_price(session, yes_tok)

    return PMMarket(
        condition_id=best.get("conditionId") or best.get("condition_id", ""),
        question=best.get("question", ""),
        yes_token_id=yes_tok,
        yes_price=price,
        end_date=best.get("endDate"),
        event_title=best.get("_event_title", ""),
    )


# ── Tipster quality filter ────────────────────────────────────────────────────

def _tipster_quality_ok(t: TipsterRecord) -> bool:
    """
    Return False for tipsters whose stats indicate poor value.

    Favourite-chaser pattern: high strike rate but low profit means they
    win often on short-odds picks that don't return value long-term.
    """
    if t.strike_rate is not None and t.annual_profit is not None:
        if t.strike_rate > 65 and t.annual_profit < 20:
            return False
    return True


# ── Signal computation ────────────────────────────────────────────────────────

@dataclass
class Signal:
    tip:                  TipSelection
    pm:                   PMMarket
    tipster:              Optional[TipsterRecord]
    tipster_implied_prob: float
    value_gap_pp:         float
    confidence:           str   # "HIGH" | "MEDIUM"
    sources:              list  = field(default_factory=lambda: ["OLBG"])
    label:                str   = "OLBG ONLY"


def compute_signal(tip: TipSelection, pm: PMMarket,
                   sources: Optional[list[str]] = None) -> Optional[Signal]:
    """
    Compute signal tier given the tip + Polymarket market and (optionally) the
    list of sources backing the selection.  Source count >= 3 upgrades the tier
    to HIGH regardless of stars.
    """
    if sources is None:
        sources = ["OLBG"]

    if pm.yes_price is None:
        return None
    if pm.yes_price < MIN_MARKET_PRICE:
        return None   # resolved or illiquid (no spread to trade)
    if pm.yes_price >= MAX_PM_PRICE:
        return None   # Polymarket already prices it as favourite

    implied = 1.0 / tip.odds_decimal  # tipster's implied probability
    gap_pp  = (implied - pm.yes_price) * 100

    # Pick best tipster (highest annual profit among quality tipsters)
    quality_tipsters = [t for t in tip.tipsters if _tipster_quality_ok(t)]
    tipster = max(
        quality_tipsters,
        key=lambda t: t.annual_profit or 0,
        default=None,
    ) if quality_tipsters else (tip.tipsters[0] if tip.tipsters else None)

    stars  = tip.star_rating_pct or 0.0
    n_src  = len(sources)
    # 1pp easier across the board when OLBG has rated the tip
    boost  = 1.0 if stars > 0 else 0.0

    if   n_src >= 3 and gap_pp >= (8.0 - boost):
        confidence = "HIGH"
    elif n_src >= 2 and gap_pp >= (6.0 - boost):
        confidence = "HIGH"
    elif n_src >= 2 and gap_pp >= (4.0 - boost):
        confidence = "MEDIUM"
    elif n_src == 1 and gap_pp >= (8.0 - boost):
        confidence = "MEDIUM"
    else:
        return None

    return Signal(
        tip=tip,
        pm=pm,
        tipster=tipster,
        tipster_implied_prob=round(implied, 4),
        value_gap_pp=round(gap_pp, 1),
        confidence=confidence,
        sources=sources,
        label=source_label(sources),
    )


# ── Alert formatting ──────────────────────────────────────────────────────────

def _stars(pct: Optional[float]) -> str:
    n = round((pct or 0) / 20)
    return "⭐" * n if n else "☆ (unrated)"


def _match_time_str(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return f"\nKickoff: {dt.strftime('%d %b %H:%M UTC')}"
    except Exception:
        return ""


def format_high_alert(sig: Signal) -> str:
    tip = sig.tip
    pm  = sig.pm
    t   = sig.tipster

    tipster_block = ""
    if t:
        expert = " (Expert ✓)" if t.is_expert else ""
        profit = (f"+{t.annual_profit:.0f}pts" if t.annual_profit and t.annual_profit > 0
                  else f"{t.annual_profit:.0f}pts" if t.annual_profit is not None
                  else "?")
        sr = f"{t.strike_rate:.0f}%" if t.strike_rate is not None else "?%"
        tipster_block = f"\nTipster: {t.name}{expert}\n  Annual profit: {profit} | Strike rate: {sr}"

    tier = "🔴 HIGH VALUE" if sig.confidence == "HIGH" else "🟡 MEDIUM VALUE"

    # Sources line — "OLBG ⭐⭐⭐⭐⭐ + LastWord ✓ + TennGrand ✓"
    parts = []
    for s in sig.sources:
        if s == "OLBG":
            parts.append(f"OLBG {_stars(tip.star_rating_pct)}")
        else:
            parts.append(f"{s} ✓")
    sources_line = " + ".join(parts) + f"  [{sig.label}]"

    return (
        f"🎾 TENNIS VALUE ALERT\n\n"
        f"Match: {tip.match_name}\n"
        f"Tip: {tip.selection} to {tip.market}\n"
        f"Sources: {sources_line}"
        f"{tipster_block}\n\n"
        f"Odds implied: {sig.tipster_implied_prob*100:.0f}% ({tip.odds_decimal:.2f})\n"
        f"Polymarket:   {pm.yes_price:.2f}c\n"
        f"Value gap:    +{sig.value_gap_pp:.1f}pp {tier}"
        f"{_match_time_str(tip.match_time_utc)}\n\n"
        f"Paper trade: YES at {pm.yes_price:.2f}c\n"
        f"PM: {pm.question[:80]}"
    )


def format_medium_summary(sigs: list[Signal]) -> str:
    date_str = datetime.now(timezone.utc).strftime("%b %d")
    lines = [f"📋 MEDIUM CONFIDENCE — {date_str}"]
    for sig in sigs:
        pm_price = f"{sig.pm.yes_price:.2f}c" if sig.pm.yes_price else "?c"
        lines.append(
            f"• {sig.tip.selection} ({sig.tip.match_name[:32]}): "
            f"PM {pm_price} | gap +{sig.value_gap_pp:.1f}pp"
        )
    return "\n".join(lines)


# ── CSV logging ───────────────────────────────────────────────────────────────

def _log_signal(sig: Signal):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_header = not CSV_PATH.exists()
    tip = sig.tip
    pm  = sig.pm
    t   = sig.tipster

    tournament = "" if " vs " in tip.match_name else tip.match_name

    row = {
        "date_logged":             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "tournament":              tournament,
        "match_or_market":         tip.match_name,
        "direction":               f"{tip.selection} {tip.market}",
        "olbg_stars":              round((tip.star_rating_pct or 0) / 20),
        "olbg_odds":               tip.odds_decimal,
        "tipster_name":            t.name if t else "",
        "tipster_profit":          t.annual_profit if t else "",
        "tipster_expert":          t.is_expert if t else "",
        "polymarket_price":        pm.yes_price,
        "tipster_implied_prob":    sig.tipster_implied_prob,
        "value_gap_pp":            sig.value_gap_pp,
        "confidence":              sig.confidence,
        "source_count":            len(sig.sources),
        "sources":                 "+".join(sig.sources),
        "polymarket_condition_id": pm.condition_id,
        "resolved":                "",
        "outcome":                 "",
        "pnl_paper":               "",
    }

    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# ── Main engine ───────────────────────────────────────────────────────────────

def run_signal_engine(verbose: bool = False):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _p = lambda s: print(s, file=sys.stderr)
    _p(f"\n{'='*60}\nSignal engine: {today}\n{'='*60}")

    # ── Telegram setup ─────────────────────────────────────────────────────
    env        = _load_env(ENV_PATH)
    tg_token   = env.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat_id = env.get("TELEGRAM_CHAT_ID", "")
    tg_ok      = bool(tg_token and tg_chat_id)
    if not tg_ok:
        _p(f"[WARN] Telegram not configured — alerts to stdout only")

    def notify(msg: str, stdout: bool = False):
        if stdout:
            print(f"\n{'─'*60}\n{msg}\n{'─'*60}")
        else:
            _p(f"\n[ALERT]\n{msg}")
        if tg_ok:
            if not _send_telegram(msg, tg_token, tg_chat_id):
                _p("[WARN] Telegram send failed")

    # ── Step 1: OLBG scrape ────────────────────────────────────────────────
    _p("Step 1: Scraping OLBG (basic)...")
    try:
        all_tips: list[TipSelection] = asyncio.run(olbg_scrape(deep=False))
    except Exception as e:
        _p(f"[ERROR] OLBG scrape failed: {e}")
        notify(f"🎾 Tennis signal engine ERROR\nOLBG scrape failed: {e}")
        return

    _p(f"  {len(all_tips)} raw tips")

    # ── Step 2: Filter (NO star filter — informational only) ───────────────
    filtered = [
        t for t in all_tips
        if re.search(r'\bwin\b', t.market, re.I)
        and t.odds_decimal is not None
        and t.odds_decimal >= MIN_ODDS
    ]
    _p(f"  {len(filtered)} pass filters "
       f"(no star filter, win market, odds≥{MIN_ODDS})")

    if not filtered:
        notify("🎾 Tennis scan complete — no tips passed filters today")
        return

    # ── Step 3: Supplementary sources (LastWord + TennGrand) ───────────────
    _p("Step 3: Scraping supplementary sources...")
    supplementary = gather_supplementary_picks(verbose=verbose)
    _p(f"  LastWord: {len(supplementary['lastword'])}  |  "
       f"TennGrand: {len(supplementary['tenngrand'])}  |  "
       f"Tennisnerd: {len(supplementary['tennisnerd'])}")

    # ── Step 4: Bulk-load active tier-1 Polymarket outright markets ────────
    _p("Step 4: Loading active tier-1 Polymarket markets...")
    session = requests.Session()
    session.headers["Accept"] = "application/json"

    market_index = load_active_outright_markets(session, verbose=verbose)
    _p(f"  Loaded {len(market_index)} active tier-1 markets")
    print(format_market_universe(market_index))

    # ── Step 5: Match OLBG tips against pre-loaded market index ────────────
    _p("Step 5: Matching tips against market index...")

    signals:   list[Signal] = []
    no_match:  list[str]    = []

    for tip in filtered:
        srcs = resolve_sources(tip.selection, supplementary)
        if verbose:
            _p(f"  → {tip.selection}  ({tip.match_name})  "
               f"odds={tip.odds_decimal}  stars={tip.star_rating_pct}%  "
               f"sources={srcs}")
        pm = match_market_by_surname(tip, market_index)
        if pm is None:
            no_match.append(f"{tip.selection} ({tip.match_name[:30]})")
            continue

        end_d = (pm.end_date or "?")[:10]
        _p(f"    matched: {pm.question[:55]}  "
           f"price={(pm.yes_price or 0)*100:.1f}c  end={end_d}")

        sig = compute_signal(tip, pm, sources=srcs)
        if sig:
            signals.append(sig)
            if verbose:
                _p(f"    SIGNAL {sig.confidence}  [{sig.label}]: "
                   f"gap={sig.value_gap_pp}pp  sources={srcs}")

    n_matched = len(filtered) - len(no_match)
    _p(f"  {n_matched}/{len(filtered)} tips matched to PM markets")
    if no_match:
        sample = ", ".join(no_match[:4]) + (" ..." if len(no_match) > 4 else "")
        _p(f"  No PM market: {sample}")

    # ── Step 4: Alerts + logging ───────────────────────────────────────────
    high_sigs = [s for s in signals if s.confidence == "HIGH"]
    med_sigs  = [s for s in signals if s.confidence == "MEDIUM"]
    _p(f"\nSignals: {len(high_sigs)} HIGH, {len(med_sigs)} MEDIUM")

    for sig in high_sigs:
        msg = format_high_alert(sig)
        notify(msg, stdout=True)
        _log_signal(sig)

    if med_sigs:
        summary = format_medium_summary(med_sigs)
        notify(summary, stdout=True)
        for sig in med_sigs:
            _log_signal(sig)

    if not signals:
        notify("🎾 Tennis scan complete — no value signals today", stdout=True)

    # ── Step 5: Summary to stdout ──────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"OLBG Tennis Signal Engine — {today}")
    print(f"{'='*60}")
    print(f"Tips scraped    : {len(all_tips)}")
    print(f"After filters   : {len(filtered)}")
    print(f"PM matched      : {n_matched}")
    print(f"Signals         : {len(high_sigs)} HIGH, {len(med_sigs)} MEDIUM")

    if signals:
        print(f"CSV             : {CSV_PATH}")
        if CSV_PATH.exists():
            lines = CSV_PATH.read_text(encoding="utf-8").splitlines()
            print(f"\nFirst rows of paper_trades.csv:")
            for line in lines[:4]:
                print(f"  {line}")


# ── Entry point ───────────────────────────────────────────────────────────────

def _run_test_player(player: str, verbose: bool = False):
    """
    Inject a synthetic tip for a named player and run the full pipeline.

    Creates a fake TipSelection with 5-star rating and odds=1.50 so the
    signal thresholds are reachable, then runs PM matching → compute_signal
    → Telegram → CSV.  Useful for verifying the end-to-end flow with a
    high-profile player who is likely to have a Polymarket market.

    Usage:
        python signal_engine.py --test-player "Jannik Sinner"
        python signal_engine.py --test-player "Jannik Sinner vs Carlos Alcaraz"
    """
    # Parse "Player A vs Player B" or just "Player A"
    if " vs " in player.lower():
        parts = re.split(r"\s+vs\.?\s+", player, maxsplit=1, flags=re.I)
        p1, p2 = parts[0].strip(), parts[1].strip()
        match_name = f"{p1} vs {p2}"
        selection  = p2   # tip the second player (underdog)
        odds       = 2.50
    else:
        match_name = player
        selection  = player
        odds       = 1.50

    tip = TipSelection(
        match_name=match_name,
        match_url="",
        match_time_utc=None,
        selection=selection,
        market="Win Match",
        odds_decimal=odds,
        tips_for=8,
        tips_total=10,
        confidence_pct=80.0,
        star_rating_pct=100.0,
        tipsters=[],
    )

    print(f"\n[TEST] Synthetic tip: {selection}  match={match_name}  "
          f"odds={odds}  stars=100%")
    print("[TEST] Searching Polymarket...")

    env        = _load_env(ENV_PATH)
    tg_token   = env.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat_id = env.get("TELEGRAM_CHAT_ID", "")
    tg_ok      = bool(tg_token and tg_chat_id)

    session = requests.Session()
    session.headers["Accept"] = "application/json"

    pm = find_pm_market(session, tip, verbose=verbose)
    if pm is None:
        print(f"[TEST] No PM market found for '{selection}'")
        return

    print(f"[TEST] Matched: {pm.question}")
    print(f"[TEST] Yes price: {pm.yes_price}  condition_id: {pm.condition_id}")

    implied = 1.0 / odds
    gap_pp  = (implied - (pm.yes_price or 0)) * 100
    print(f"[TEST] Tipster implied: {implied*100:.1f}%  gap: {gap_pp:.1f}pp")

    sig = compute_signal(tip, pm)
    if sig is None:
        # Override thresholds so we can still test Telegram + CSV
        print("[TEST] Gap below signal thresholds — forcing MEDIUM for pipeline test")
        sig = Signal(
            tip=tip, pm=pm, tipster=None,
            tipster_implied_prob=round(implied, 4),
            value_gap_pp=round(gap_pp, 1),
            confidence="MEDIUM",
        )

    msg = format_high_alert(sig)
    print(f"\n[TEST] Alert message:\n{'─'*60}\n{msg}\n{'─'*60}")

    if tg_ok:
        ok = _send_telegram(f"[TEST]\n{msg}", tg_token, tg_chat_id)
        print(f"[TEST] Telegram send: {'OK' if ok else 'FAILED (check token in .env)'}")
    else:
        print("[TEST] Telegram not configured — skipping send")

    _log_signal(sig)
    print(f"[TEST] CSV row written → {CSV_PATH}")
    if CSV_PATH.exists():
        lines = CSV_PATH.read_text(encoding="utf-8").splitlines()
        for line in lines[-2:]:
            print(f"  {line}")


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="OLBG → Polymarket tennis value signal engine"
    )
    ap.add_argument("--once", action="store_true",
                    help="Run once and exit")
    ap.add_argument("--schedule", type=str, default="08:00,14:00",
                    help="Daily run times, comma-separated (default: 08:00,14:00)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Debug output: show every PM search result scored")
    ap.add_argument("--test-player", type=str, default=None, metavar="NAME",
                    help='Inject a synthetic tip and test full pipeline. '
                         'E.g. --test-player "Jannik Sinner" or '
                         '"Sinner vs Alcaraz"')
    args = ap.parse_args()

    if args.test_player:
        _run_test_player(args.test_player, verbose=args.verbose)
        return

    if args.once:
        run_signal_engine(verbose=args.verbose)
        return

    try:
        import schedule as _sched
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "schedule"],
                       check=True)
        import schedule as _sched

    import time as _time

    for t in args.schedule.split(","):
        t = t.strip()
        _sched.every().day.at(t).do(run_signal_engine)
        print(f"Scheduled: {t} daily")

    print(f"Running. Press Ctrl+C to stop.")
    while True:
        _sched.run_pending()
        _time.sleep(30)


if __name__ == "__main__":
    main()
