import httpx
import traceback

KEY = "f4bf5987abe7be581dccf7dc52998dc7"
URL = "https://clob.polymarket.com/order"

proxies = {
    "standard": f"http://scraperapi:{KEY}@proxy-server.scraperapi.com:8001",
    "premium":  f"http://scraperapi.premium:{KEY}@proxy-server.scraperapi.com:8001",
}

for label, proxy in proxies.items():
    print(f"\nTesting [{label}] POST {URL}...")
    try:
        r = httpx.post(URL, proxy=proxy, json={}, verify=False, timeout=30)
        print(f"  Status : {r.status_code}")
        print(f"  Body   : {r.text[:300]}")
    except Exception as e:
        print(f"  Exception: {type(e).__name__}: {e}")

