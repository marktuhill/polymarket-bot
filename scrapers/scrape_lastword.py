#!/usr/bin/env python3
"""
scrape_lastword.py — Last Word On Tennis predictions scraper.

Scrapes https://lastwordonsports.com/tennis/category/predictions/
and follows each article URL to extract picks formatted as
"Prediction: <Player> in <X>".

Returns: list of dicts {selection, match_or_tournament, source, confidence}
"""

import re
import sys
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

LISTING_URL = "https://lastwordonsports.com/tennis/category/predictions/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAX_ARTICLES        = 6     # only scan the most recent 6
RECENT_DAYS         = 2     # only consider articles published in last N days
ARTICLE_TIMEOUT_S   = 12

# URL date pattern: /YYYY/MM/DD/
URL_DATE_RE = re.compile(r"/(20\d{2})/(\d{2})/(\d{2})/")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    s.headers["Accept"]     = "text/html,application/xhtml+xml"
    return s


def _article_age_days(url: str) -> Optional[int]:
    m = URL_DATE_RE.search(url)
    if not m:
        return None
    try:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                      tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


def _extract_predictions(article_text: str, title: str) -> list[dict]:
    """
    Find every "Prediction: <name> in <X>" in the article body.
    Returns list of {selection, match_or_tournament, confidence}.
    """
    picks = []
    # Greedy capture up to "in <number>" or punctuation
    pred_re = re.compile(
        r"Prediction[:\s]+([A-ZŚĆŁÁÉÍÓÚÑ][\w\s\.'-]+?)\s+in\s+(?:2|3|straight)",
        re.I,
    )
    confidence_high = bool(re.search(
        r"\b(best bet|strong pick|safest bet|surest|easy win|locks?\b)",
        article_text, re.I,
    ))

    seen: set[str] = set()
    for m in pred_re.finditer(article_text):
        raw = m.group(1).strip().rstrip(".,;:")
        if not raw or len(raw) > 60 or raw.lower() in seen:
            continue
        seen.add(raw.lower())
        picks.append({
            "selection":            raw,
            "match_or_tournament":  title[:120],
            "confidence":           "HIGH" if confidence_high else "MEDIUM",
            "source":               "LastWord",
        })
    return picks


def _fetch_article(session: requests.Session, url: str, title: str) -> list[dict]:
    try:
        r = session.get(url, timeout=ARTICLE_TIMEOUT_S)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        article = soup.select_one("article") or soup.select_one("main") or soup
        text = article.get_text(" ", strip=True)
        return _extract_predictions(text, title)
    except Exception as e:
        print(f"[WARN] LastWord article {url}: {e}", file=sys.stderr)
        return []


def scrape_lastword(verbose: bool = False) -> list[dict]:
    """
    Scrape Last Word On Tennis predictions.

    Returns list of pick dicts:
        {selection, match_or_tournament, source, confidence}
    """
    session = _session()
    try:
        r = session.get(LISTING_URL, timeout=ARTICLE_TIMEOUT_S)
        if r.status_code != 200:
            print(f"[WARN] LastWord listing HTTP {r.status_code}", file=sys.stderr)
            return []
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"[WARN] LastWord listing failed: {e}", file=sys.stderr)
        return []

    articles_meta: list[tuple[str, str]] = []
    for a in soup.select("article"):
        h    = a.find(["h1", "h2", "h3"])
        link = a.find("a", href=True)
        if not h or not link:
            continue
        url   = link["href"]
        title = h.get_text(strip=True)

        age = _article_age_days(url)
        if age is None or age > RECENT_DAYS:
            continue

        articles_meta.append((url, title))
        if len(articles_meta) >= MAX_ARTICLES:
            break

    if verbose:
        print(f"[LastWord] {len(articles_meta)} recent articles to fetch",
              file=sys.stderr)

    all_picks: list[dict] = []
    for url, title in articles_meta:
        picks = _fetch_article(session, url, title)
        if verbose:
            print(f"[LastWord]   {len(picks)} picks from {title[:60]}",
                  file=sys.stderr)
        all_picks.extend(picks)

    return all_picks


if __name__ == "__main__":
    import json
    picks = scrape_lastword(verbose=True)
    print(json.dumps(picks, indent=2, ensure_ascii=False))
    print(f"\nTotal picks: {len(picks)}")
