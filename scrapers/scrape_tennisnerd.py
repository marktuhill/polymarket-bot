#!/usr/bin/env python3
"""
scrape_tennisnerd.py — Tennisnerd.net predictions scraper.

Scrapes https://www.tennisnerd.net/category/predictions/, follows each
recent article URL, and extracts picks using several regex patterns.

Returns: list of dicts {selection, tournament, source, article_url, confidence}
"""

import re
import sys
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

LISTING_URL = "https://www.tennisnerd.net/category/predictions/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAX_ARTICLES      = 6
RECENT_DAYS       = 2
ARTICLE_TIMEOUT_S = 12


# Patterns to capture a picked player.  Accept single surnames ("Rublev")
# OR full names ("Andrey Rublev").  Cross-reference matching against the
# OLBG selection (by surname) handles dedup and false positives.
NAME_RE = r"[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)?"
PICK_PATTERNS = [
    re.compile(rf"(?:My\s+)?prediction[:\s]+({NAME_RE})\s+to\s+win", re.I),
    re.compile(rf"\bback(?:ing)?\s+({NAME_RE})\b", re.I),
    re.compile(rf"\btip[:\s]+({NAME_RE})\b", re.I),
    re.compile(rf"\b({NAME_RE})\s+to\s+win\b"),
    re.compile(rf"\b({NAME_RE})\s+at\s+\d+\.\d+\s+(?:is|are|seems|looks|offers|gives)"),
]

# Common false-positive captures we should reject
NAME_BLACKLIST = {
    "the", "his", "her", "this", "that", "matchup", "italian", "open",
    "french", "wimbledon", "miami", "madrid", "rome", "paris", "monte",
    "indian", "head", "best", "round", "quarter", "semi", "final",
    "venue", "scheduled", "time", "tomorrow", "today", "yesterday",
    "facebook", "tweet", "pin", "linkedin", "leave", "email", "save",
    "approx", "while", "despite", "however", "interestingly",
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    s.headers["Accept"]     = "text/html,application/xhtml+xml"
    return s


def _published_age_days(soup: BeautifulSoup) -> Optional[int]:
    """Pull the article date from the OpenGraph meta tag."""
    meta = soup.find("meta", property="article:published_time")
    if not meta or not meta.get("content"):
        return None
    raw = meta["content"]
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


def _clean_name(name: str) -> Optional[str]:
    """Strip punctuation, reject blacklisted captures."""
    name = name.strip().rstrip(".,;:!?")
    if not name:
        return None
    first_word = name.split()[0].lower()
    if first_word in NAME_BLACKLIST:
        return None
    if len(name) > 60:
        return None
    return name


def _extract_predictions(article_text: str, title: str, url: str) -> list[dict]:
    """Find all "to win" / "back" / "tip:" picks in the article body."""
    confidence_high = bool(re.search(
        r"\b(best bet|strong pick|safest|surest|locks?\b|easy pick)",
        article_text, re.I,
    ))

    seen: set[str] = set()
    picks: list[dict] = []

    for pat in PICK_PATTERNS:
        for m in pat.finditer(article_text):
            raw = _clean_name(m.group(1))
            if not raw:
                continue
            key = raw.lower()
            if key in seen:
                continue
            seen.add(key)
            picks.append({
                "selection":   raw,
                "tournament":  title[:120],
                "source":      "Tennisnerd",
                "article_url": url,
                "confidence":  "HIGH" if confidence_high else "MEDIUM",
            })

    return picks


def _fetch_article(session: requests.Session, url: str,
                   title: str) -> list[dict]:
    try:
        r = session.get(url, timeout=ARTICLE_TIMEOUT_S)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")

        age = _published_age_days(soup)
        if age is not None and age > RECENT_DAYS:
            return []   # too old

        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        body = soup.select_one("main") or soup.select_one("article") or soup
        text = body.get_text(" ", strip=True)
        return _extract_predictions(text, title, url)
    except Exception as e:
        print(f"[WARN] Tennisnerd article {url}: {e}", file=sys.stderr)
        return []


def scrape_tennisnerd(verbose: bool = False) -> list[dict]:
    """
    Scrape Tennisnerd predictions.

    Returns list of pick dicts:
        {selection, tournament, source, article_url, confidence}
    """
    session = _session()
    try:
        r = session.get(LISTING_URL, timeout=ARTICLE_TIMEOUT_S)
        if r.status_code != 200:
            print(f"[WARN] Tennisnerd listing HTTP {r.status_code}", file=sys.stderr)
            return []
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"[WARN] Tennisnerd listing failed: {e}", file=sys.stderr)
        return []

    seen_urls: set[str] = set()
    articles_meta: list[tuple[str, str]] = []
    for h in soup.select("h2 a, h3 a"):
        url = h.get("href", "")
        if not url or "/betting-blog/" not in url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = h.get_text(strip=True)
        articles_meta.append((url, title))
        if len(articles_meta) >= MAX_ARTICLES:
            break

    if verbose:
        print(f"[Tennisnerd] {len(articles_meta)} candidate articles",
              file=sys.stderr)

    all_picks: list[dict] = []
    for url, title in articles_meta:
        picks = _fetch_article(session, url, title)
        if verbose:
            print(f"[Tennisnerd]   {len(picks)} picks from {title[:60]}",
                  file=sys.stderr)
        all_picks.extend(picks)

    return all_picks


if __name__ == "__main__":
    import sys, json
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    picks = scrape_tennisnerd(verbose=True)
    print(json.dumps(picks, indent=2, ensure_ascii=False))
    print(f"\nTotal picks: {len(picks)}")
