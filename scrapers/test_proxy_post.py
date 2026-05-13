import httpx
import traceback

PROXY = "http://scraperapi:f4bf5987abe7be581dccf7dc52998dc7@proxy-server.scraperapi.com:8001"
URL   = "https://clob.polymarket.com/order"

print(f"Testing POST {URL} via ScraperAPI proxy...")
try:
    r = httpx.post(URL, proxy=PROXY, json={}, verify=False, timeout=30)
    print(f"Status : {r.status_code}")
    print(f"Body   : {r.text[:500]}")
except Exception as e:
    print(f"Exception type : {type(e).__name__}")
    print(f"Exception      : {e}")
    traceback.print_exc()
