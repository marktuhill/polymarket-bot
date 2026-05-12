#!/usr/bin/env python3
"""
scrape_olbg.py — OLBG tennis tips scraper.

Loads the OLBG tennis tips page in a real Chromium browser (via Playwright)
so JavaScript renders fully, then parses the HTML with BeautifulSoup.

Usage:
    python scrapers/scrape_olbg.py                  # JSON to stdout
    python scrapers/scrape_olbg.py --out tips.json  # save to file
    python scrapers/scrape_olbg.py --deep           # also fetch per-tipster data

Output fields (per tip selection):
    match_name        str   "Matteo Arnaldi vs Rafael Jodar"
    match_url         str   full URL of the match tips page
    match_time_utc    str   ISO-8601 UTC, e.g. "2026-05-10T18:30:00.000Z"
    selection         str   "Rafael Jodar"
    market            str   "Win Match" | "Total Games" | ...
    odds_decimal      float decimal odds, e.g. 3.25
    tips_for          int   numerator   of "4/5 Win Tips"
    tips_total        int   denominator of "4/5 Win Tips"
    confidence_pct    float community agreement %, e.g. 80.0
    star_rating_pct   float OLBG value rating 0-100 (100 = 5 stars)
    tipsters          list  populated only with --deep:
        name          str
        is_expert     bool
        annual_profit float | null  units of stake
        strike_rate   float | null  0-100 %
        selection     str
        odds          float | null
        profile_url   str
"""

import asyncio
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright

# ── config ────────────────────────────────────────────────────────────────────

# Load .env from parent dir (tennis bot root)
load_dotenv(Path(__file__).parent.parent / ".env")

TIPS_URL = "https://www.olbg.com/betting-tips/Tennis/3"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
MAIN_PAGE_WAIT_S = 8   # seconds after page load for JS to fully render
MATCH_PAGE_WAIT_S = 4  # seconds for individual match pages

SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "")

def _proxy_config() -> Optional[dict]:
    if not SCRAPERAPI_KEY:
        return None
    return {
        "server":   "http://proxy-server.scraperapi.com:8001",
        "username": "scraperapi",
        "password": SCRAPERAPI_KEY,
    }


# ── data models ───────────────────────────────────────────────────────────────

@dataclass
class TipsterRecord:
    name: str
    is_expert: bool
    annual_profit: Optional[float]
    strike_rate: Optional[float]
    selection: str
    odds: Optional[float]
    profile_url: Optional[str]


@dataclass
class TipSelection:
    match_name: str
    match_url: str
    match_time_utc: Optional[str]
    selection: str
    market: str
    odds_decimal: Optional[float]
    tips_for: int
    tips_total: int
    confidence_pct: Optional[float]
    star_rating_pct: Optional[float]
    tipsters: list = field(default_factory=list)


# ── HTML parsing helpers ──────────────────────────────────────────────────────

def _star_pct(card) -> Optional[float]:
    """Width % from the desktop star rating div (div.rw.rating .stars .filled)."""
    rating_div = card.select_one('div.rw.rating')
    if rating_div:
        filled = rating_div.select_one('.stars .filled')
        if filled:
            m = re.search(r'width:\s*([\d.]+)%', filled.get('style', ''))
            if m:
                return float(m.group(1))
    # Fallback: any .stars .filled in the card
    filled = card.select_one('.stars .filled')
    if filled:
        m = re.search(r'width:\s*([\d.]+)%', filled.get('style', ''))
        if m:
            return float(m.group(1))
    return None


def _confidence_pct(tips_div) -> Optional[float]:
    """Extract confidence % from the rw.tips div."""
    if not tips_div:
        return None
    # CSS var approach: style="--confidence: 86%"
    conf_el = tips_div.find(style=re.compile(r'--confidence'))
    if conf_el:
        m = re.search(r'--confidence:\s*([\d.]+)%', conf_el.get('style', ''))
        if m:
            return float(m.group(1))
    # Fallback: span text that looks like "86%"
    for span in tips_div.find_all('span'):
        txt = span.get_text(strip=True)
        m = re.fullmatch(r'(\d+)%', txt)
        if m:
            return float(m.group(1))
    return None


def _parse_tips_label(text: str) -> tuple[int, int]:
    """'12/14 Win Tips' → (12, 14).  Returns (0, 0) on failure."""
    m = re.search(r'(\d+)/(\d+)', text)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


# ── main page parser ──────────────────────────────────────────────────────────

def parse_main_page(html: str) -> list[TipSelection]:
    soup = BeautifulSoup(html, 'html.parser')
    results: list[TipSelection] = []

    # Each tip card: <div class="grd tip content-visibility-auto">
    cards = soup.select('div.grd.tip')

    for card in cards:
        try:
            # ── event row ──────────────────────────────────────────────────
            ev = card.select_one('div.rw.ev')
            if not ev:
                continue

            h5 = ev.find('h5', itemprop='name')
            match_name = h5.get_text(strip=True) if h5 else ''

            url_tag = ev.find('a', itemprop='url')
            match_url = url_tag['href'] if url_tag and url_tag.get('href') else ''

            time_tag = ev.find('time', itemprop='startDate')
            match_time = time_tag.get('datetime') if time_tag else None

            # ── selection row ─────────────────────────────────────────────
            sel_div = card.select_one('div.rw.sel')
            if not sel_div:
                continue

            h4 = sel_div.find('h4')
            selection = h4.get_text(strip=True) if h4 else ''

            # market: first <p> with class "truncate" inside sel_div
            market_p = sel_div.find('p', class_='truncate')
            market = market_p.get_text(strip=True) if market_p else ''

            # ── odds row ──────────────────────────────────────────────────
            odds_span = card.select_one('span.ui-odds')
            odds_decimal = None
            if odds_span and odds_span.get('data-decimal'):
                try:
                    odds_decimal = float(odds_span['data-decimal'])
                except ValueError:
                    pass

            # ── tips/consensus row ────────────────────────────────────────
            tips_div = card.select_one('div.rw.tips')
            tips_for, tips_total = 0, 0
            if tips_div:
                b_tag = tips_div.find('b')
                if b_tag:
                    tips_for, tips_total = _parse_tips_label(b_tag.get_text())

            confidence = _confidence_pct(tips_div)

            # ── star rating ───────────────────────────────────────────────
            star_pct = _star_pct(card)

            results.append(TipSelection(
                match_name=match_name,
                match_url=match_url,
                match_time_utc=match_time,
                selection=selection,
                market=market,
                odds_decimal=odds_decimal,
                tips_for=tips_for,
                tips_total=tips_total,
                confidence_pct=confidence,
                star_rating_pct=star_pct,
            ))

        except Exception as exc:
            print(f"[WARN] Skipping card: {exc}", file=sys.stderr)

    return results


# ── match detail page parser ──────────────────────────────────────────────────

def parse_match_page(html: str, consensus_selection: str) -> list[TipsterRecord]:
    """
    Parse an individual match tips page for per-tipster records.

    OLBG match pages show individual tipster "cards" for the featured selection.
    Each card has:
    - A link  /best-tipsters/{sport}/{id}  → the tipster's profile
    - An <h4> inside the link with the tipster's display name
    - A star icon + "In profit on {sport} for N of the previous 6 months" → Expert indicator
    - <dl><dt>Annual Profit</dt><dd>43</dd></dl> etc. for stats
    - <span class="ui-odds" data-decimal="1.30"> for the odds they tipped

    NOTE: Only the "featured" tipsters for the top consensus selection are shown
    on page load.  Clicking "View All Tips" would reveal more (requires JS interaction).
    """
    soup = BeautifulSoup(html, 'html.parser')
    records: list[TipsterRecord] = []
    seen: set[str] = set()

    # Tipster profile links: href="/best-tipsters/{sport}/{id}"
    tipster_links = soup.find_all('a', href=re.compile(r'/best-tipsters/', re.I))

    for link in tipster_links:
        # Name is in <h4> inside the <a> tag
        h4 = link.find('h4')
        if not h4:
            continue
        name = h4.get_text(strip=True)
        if not name or name in seen or len(name) > 100:
            continue
        seen.add(name)

        href = link.get('href', '')
        profile_url = href if href.startswith('http') else f'https://www.olbg.com{href}'

        # Walk up to the tipster card container (has class "cmt")
        parent = link
        for _ in range(8):
            p = parent.parent
            if p is None:
                break
            parent = p
            if 'cmt' in (parent.get('class') or []):
                break

        # Expert: card contains "In profit on" text (orange star indicator)
        is_expert = bool(
            parent.find(string=re.compile(r'In profit on', re.I))
        )

        # Annual Profit and Annual Strike Rate from <dl><dt>/<dd> pairs
        annual_profit = None
        strike_rate = None
        for dl in parent.find_all('dl'):
            dt = dl.find('dt')
            dd = dl.find('dd')
            if not dt or not dd:
                continue
            label = dt.get_text(strip=True)
            value_txt = dd.get_text(strip=True)
            m = re.search(r'([+-]?\d+\.?\d*)', value_txt)
            if not m:
                continue
            val = float(m.group(1))
            if 'Annual Profit' in label:
                annual_profit = val
            elif 'Annual Strike Rate' in label:
                strike_rate = val

        # Odds this tipster tipped
        odds = None
        odds_span = parent.select_one('span.ui-odds')
        if odds_span and odds_span.get('data-decimal'):
            try:
                odds = float(odds_span['data-decimal'])
            except ValueError:
                pass

        records.append(TipsterRecord(
            name=name,
            is_expert=is_expert,
            annual_profit=annual_profit,
            strike_rate=strike_rate,
            selection=consensus_selection,
            odds=odds,
            profile_url=profile_url,
        ))

    return records


# ── Playwright orchestrator ───────────────────────────────────────────────────

async def scrape(deep: bool = False) -> list[TipSelection]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        proxy = _proxy_config()
        if proxy:
            print(f"Using ScraperAPI proxy ...", file=sys.stderr)
        context = await browser.new_context(user_agent=USER_AGENT, proxy=proxy)

        # ── main page ──────────────────────────────────────────────────────
        page = await context.new_page()
        print(f"Loading {TIPS_URL} ...", file=sys.stderr)
        try:
            await page.goto(TIPS_URL, wait_until="load", timeout=20000)
        except Exception:
            pass
        print(f"Waiting {MAIN_PAGE_WAIT_S}s for JS render ...", file=sys.stderr)
        await asyncio.sleep(MAIN_PAGE_WAIT_S)
        html = await page.content()
        await page.close()

        selections = parse_main_page(html)
        print(f"Parsed {len(selections)} tip selections.", file=sys.stderr)

        if not deep:
            await browser.close()
            return selections

        # ── per-match tipster pages ────────────────────────────────────────
        seen_urls: set[str] = set()
        for sel in selections:
            if not sel.match_url or sel.match_url in seen_urls:
                continue
            seen_urls.add(sel.match_url)

            try:
                mp = await context.new_page()
                print(f"  Loading match: {sel.match_url[:90]} ...", file=sys.stderr)
                try:
                    await mp.goto(sel.match_url, wait_until="load", timeout=20000)
                except Exception:
                    pass
                await asyncio.sleep(MATCH_PAGE_WAIT_S)
                mhtml = await mp.content()
                await mp.close()

                # Use the most-tipped selection for this match as the consensus
                # (the featured tipster cards on the match page show tipsters for
                # the consensus pick, so we pass that selection name)
                match_sels = [s for s in selections if s.match_url == sel.match_url]
                best = max(match_sels, key=lambda s: s.tips_for)
                tipsters = parse_match_page(mhtml, best.selection)
                print(f"    → {len(tipsters)} tipsters for '{best.selection}'",
                      file=sys.stderr)

                # Attach only to the consensus selection; others get empty list
                best.tipsters = tipsters

            except Exception as e:
                print(f"  [WARN] {sel.match_url}: {e}", file=sys.stderr)

        await browser.close()

    return selections


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Scrape OLBG tennis tips into JSON."
    )
    ap.add_argument(
        '--out', '-o', type=Path, default=None,
        help="Write JSON to this file instead of stdout"
    )
    ap.add_argument(
        '--deep', action='store_true',
        help="Also visit each match tips page for per-tipster data"
    )
    args = ap.parse_args()

    selections = asyncio.run(scrape(deep=args.deep))
    data = [asdict(s) for s in selections]

    output = json.dumps(data, indent=2, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding='utf-8')
        print(f"Saved {len(data)} tips to {args.out}", file=sys.stderr)
    else:
        print(output)


if __name__ == '__main__':
    main()
