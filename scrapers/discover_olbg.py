#!/usr/bin/env python3
"""
discover_olbg.py — Diagnose why scrape_olbg returns 0 tips.

Loads the OLBG tennis page, dumps HTML to scrapers/olbg_page_dump.html,
then reports what it found.

Usage:
    python scrapers/discover_olbg.py
"""

import asyncio
import os
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv(Path(__file__).parent.parent / ".env")

TIPS_URL   = "https://www.olbg.com/betting-tips/Tennis/3"
DUMP_FILE  = Path(__file__).parent / "olbg_page_dump.html"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
WAIT_S = 10
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "")


async def discover():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        proxy = {"server": "http://proxy-server.scraperapi.com:8001",
                 "username": "scraperapi", "password": SCRAPERAPI_KEY
                 } if SCRAPERAPI_KEY else None
        if proxy:
            print(f"Using ScraperAPI proxy ...")
        context = await browser.new_context(user_agent=USER_AGENT, proxy=proxy)
        page    = await context.new_page()

        print(f"Loading {TIPS_URL} ...")
        try:
            await page.goto(TIPS_URL, wait_until="load", timeout=30000)
        except Exception as e:
            print(f"  [WARN] goto raised: {e}")

        print(f"Waiting {WAIT_S}s for JS render ...")
        await asyncio.sleep(WAIT_S)

        html  = await page.content()
        title = await page.title()
        await browser.close()

    # ── Save dump ──────────────────────────────────────────────────────────────
    DUMP_FILE.write_text(html, encoding="utf-8", errors="replace")
    print(f"\nHTML dump saved → {DUMP_FILE}  ({len(html):,} bytes)")

    # ── Parse ─────────────────────────────────────────────────────────────────
    soup = BeautifulSoup(html, "html.parser")

    print(f"\n{'='*60}")
    print(f"Page title : {title!r}")
    print(f"HTML size  : {len(html):,} bytes")

    # Cloudflare / challenge check
    cf_indicators = ["cf-browser-verification", "challenge-form",
                     "just a moment", "checking your browser", "ray id"]
    cf_hit = any(ind in html.lower() for ind in cf_indicators)
    print(f"Cloudflare : {'YES ⚠️' if cf_hit else 'no'}")

    # Login / paywall check
    login_indicators = ["log in to view", "sign in", "create an account",
                        "please log in", "members only"]
    login_hit = any(ind in html.lower() for ind in login_indicators)
    print(f"Login wall : {'YES ⚠️' if login_hit else 'no'}")

    # ── Element counts ─────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("Element counts:")

    selectors = [
        ("div.grd.tip",          "original selector"),
        ("div.grd",              "any div.grd"),
        ("[class*='tip']",       "any element with 'tip' in class"),
        ("[class*='card']",      "any element with 'card' in class"),
        ("[class*='event']",     "any element with 'event' in class"),
        ("article",              "article tags"),
        ("[data-testid]",        "elements with data-testid"),
        (".tip-card",            ".tip-card"),
        (".tips-card",           ".tips-card"),
        (".betting-tip",         ".betting-tip"),
    ]

    for sel, label in selectors:
        try:
            count = len(soup.select(sel))
            marker = " ← FOUND" if count > 0 else ""
            print(f"  {sel:<30} {count:>4}  {label}{marker}")
        except Exception:
            pass

    # ── Sample classes near tip-like content ──────────────────────────────────
    print(f"\n{'='*60}")
    print("Top-level div classes (up to 30 unique):")
    classes_seen = set()
    for div in soup.find_all("div", limit=500):
        cls = " ".join(div.get("class", []))
        if cls and cls not in classes_seen:
            classes_seen.add(cls)
    for cls in sorted(classes_seen)[:30]:
        print(f"  {cls}")

    # ── First 200 lines of dump ────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("First 200 lines of olbg_page_dump.html:")
    print("-"*60)
    lines = html.splitlines()
    for i, line in enumerate(lines[:200], 1):
        print(f"{i:>4}  {line}")

    print(f"\n{'='*60}")
    print("Done. Full HTML is in:", DUMP_FILE)


if __name__ == "__main__":
    asyncio.run(discover())
