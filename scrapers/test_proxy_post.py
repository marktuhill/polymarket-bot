"""
Tests multiple routing strategies for POST to Polymarket CLOB API.
ScraperAPI proxy mode times out; testing API-endpoint mode and ultra_premium.
"""
import httpx

KEY = "f4bf5987abe7be581dccf7dc52998dc7"
TARGET = "https://clob.polymarket.com/order"

# ── 1. ScraperAPI proxy mode (standard / premium / ultra_premium) ─────────────
proxy_variants = {
    "proxy-standard":       f"http://scraperapi:{KEY}@proxy-server.scraperapi.com:8001",
    "proxy-premium":        f"http://scraperapi.premium:{KEY}@proxy-server.scraperapi.com:8001",
    "proxy-ultra_premium":  f"http://scraperapi.ultra_premium:{KEY}@proxy-server.scraperapi.com:8001",
}

for label, proxy in proxy_variants.items():
    print(f"\nTesting [{label}] ...")
    try:
        r = httpx.post(TARGET, proxy=proxy, json={}, verify=False, timeout=60)
        print(f"  Status : {r.status_code}")
        print(f"  Body   : {r.text[:300]}")
    except Exception as e:
        print(f"  Exception: {type(e).__name__}: {e}")

# ── 2. ScraperAPI API-endpoint mode (GET wrapper, premium=true) ───────────────
# Different approach: we POST to ScraperAPI's own endpoint which relays the request.
api_variants = {
    "api-standard":      {"api_key": KEY, "url": TARGET},
    "api-premium":       {"api_key": KEY, "url": TARGET, "premium": "true"},
    "api-ultra_premium": {"api_key": KEY, "url": TARGET, "ultra_premium": "true"},
}

for label, params in api_variants.items():
    print(f"\nTesting [{label}] ...")
    try:
        r = httpx.post(
            "https://api.scraperapi.com/",
            params=params,
            json={},
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        print(f"  Status : {r.status_code}")
        print(f"  Body   : {r.text[:300]}")
    except Exception as e:
        print(f"  Exception: {type(e).__name__}: {e}")

# ── 3. Direct (no proxy) — baseline to confirm VPS IP is actually blocked ─────
print(f"\nTesting [direct-no-proxy] ...")
try:
    r = httpx.post(TARGET, json={}, timeout=15)
    print(f"  Status : {r.status_code}")
    print(f"  Body   : {r.text[:300]}")
except Exception as e:
    print(f"  Exception: {type(e).__name__}: {e}")
