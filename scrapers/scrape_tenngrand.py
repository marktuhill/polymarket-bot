#!/usr/bin/env python3
"""
scrape_tenngrand.py — The Grandstand best-bets-of-the-day scraper.

Scrapes https://tenngrand.com/best-bets-of-the-day/ and extracts:
  1. Match picks: "Player A over Player B in 2/3"
  2. Featured value picks: "Player A over Player B (+/-N)"
  3. Weekly futures: "Player to win Tournament (X units at +ODDS)"

Returns: list of dicts
  {selection, opponent, tournament, source, kind}
"""

import re
import sys
from typing import Optional

import requests
from bs4 import BeautifulSoup

URL = "https://tenngrand.com/best-bets-of-the-day/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# "Player A over Player B in 2" or "in 3" — match pick
MATCH_RE  = re.compile(
    r"\b([A-ZŚĆŁÁÉÍÓÚÑ][\w\.'-]+(?:\s+[A-ZŚĆŁÁÉÍÓÚÑ][\w\.'-]+)+)"
    r"\s+over\s+"
    r"([A-ZŚĆŁÁÉÍÓÚÑ][\w\.'-]+(?:\s+[A-ZŚĆŁÁÉÍÓÚÑ][\w\.'-]+)+)"
    r"\s+in\s+[23]\b",
)

# "Player A over Player B (+/-N)" — featured value pick (no "in 2/3")
VALUE_RE  = re.compile(
    r"\b([A-ZŚĆŁÁÉÍÓÚÑ][\w\.'-]+(?:\s+[A-ZŚĆŁÁÉÍÓÚÑ][\w\.'-]+)+)"
    r"\s+over\s+"
    r"([A-ZŚĆŁÁÉÍÓÚÑ][\w\.'-]+(?:\s+[A-ZŚĆŁÁÉÍÓÚÑ][\w\.'-]+)+)"
    r"\s*\([+-]\d+\)",
)

# "Player to win Tournament (X units at +ODDS)"  — futures
FUTURE_RE = re.compile(
    r"\b([A-ZŚĆŁÁÉÍÓÚÑ][\w\.'-]+(?:\s+[A-ZŚĆŁÁÉÍÓÚÑ][\w\.'-]+)+)"
    r"\s+to\s+win\s+"
    r"([A-Z][\w\s'-]+?)"
    r"\s*\(\d+\s*unit",
    re.I,
)


def _normalize_text(text: str) -> str:
    # Replace non-breaking spaces and tighten whitespace
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip()


def scrape_tenngrand(verbose: bool = False) -> list[dict]:
    """
    Scrape The Grandstand best-bets page.

    Returns list of dicts:
      {selection, opponent (str|None), tournament (str|None),
       source, kind: 'match'|'value'|'future'}
    """
    try:
        r = requests.get(URL, timeout=12,
                         headers={"User-Agent": USER_AGENT,
                                  "Accept": "text/html,application/xhtml+xml"})
        if r.status_code != 200:
            print(f"[WARN] TennGrand HTTP {r.status_code}", file=sys.stderr)
            return []
    except Exception as e:
        print(f"[WARN] TennGrand fetch failed: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    body = soup.select_one("article") or soup.select_one("main") or soup
    text = _normalize_text(body.get_text(" "))

    picks: list[dict] = []
    seen: set[tuple] = set()

    # 1) Match picks ("Player A over Player B in 2/3")
    for m in MATCH_RE.finditer(text):
        a = m.group(1).strip()
        b = m.group(2).strip()
        key = (a.lower(), b.lower(), "match")
        if key in seen:
            continue
        seen.add(key)
        picks.append({
            "selection":  a,
            "opponent":   b,
            "tournament": None,
            "source":     "TennGrand",
            "kind":       "match",
        })

    # 2) Value picks ("Player A over Player B (+/-N)")
    for m in VALUE_RE.finditer(text):
        a = m.group(1).strip()
        b = m.group(2).strip()
        key = (a.lower(), b.lower(), "match")
        if key in seen:
            continue   # already captured as a match pick
        seen.add(key)
        picks.append({
            "selection":  a,
            "opponent":   b,
            "tournament": None,
            "source":     "TennGrand",
            "kind":       "value",
        })

    # 3) Futures ("Player to win Tournament (X units at +ODDS)")
    for m in FUTURE_RE.finditer(text):
        player    = m.group(1).strip()
        tourney   = m.group(2).strip().rstrip(".,;:")
        key = (player.lower(), tourney.lower(), "future")
        if key in seen:
            continue
        seen.add(key)
        picks.append({
            "selection":  player,
            "opponent":   None,
            "tournament": tourney,
            "source":     "TennGrand",
            "kind":       "future",
        })

    if verbose:
        print(f"[TennGrand] {len(picks)} picks "
              f"({sum(1 for p in picks if p['kind']=='match')} match, "
              f"{sum(1 for p in picks if p['kind']=='value')} value, "
              f"{sum(1 for p in picks if p['kind']=='future')} futures)",
              file=sys.stderr)

    return picks


if __name__ == "__main__":
    import json
    picks = scrape_tenngrand(verbose=True)
    print(json.dumps(picks, indent=2, ensure_ascii=False))
    print(f"\nTotal picks: {len(picks)}")
