"""
Tests NordVPN SOCKS5 proxy for POST to Polymarket CLOB API.
Run after opening outbound TCP 1080 in Windows Firewall (wf.msc).
"""
import httpx

TARGET = "https://clob.polymarket.com/order"

NORD_USER   = "yojfhwXz4Fd9c2udbgpcpQq7"
NORD_PASS   = "HE8s7fB1pe13FrAaLdAEgUnr"
NORD_SERVER = "us4532.nordvpn.com"   # US server supporting SOCKS5 on port 1080

NORD_PROXY  = f"socks5://{NORD_USER}:{NORD_PASS}@{NORD_SERVER}:1080"

# ── 1. NordVPN SOCKS5 POST to Polymarket ─────────────────────────────────────
print(f"\nTesting [Nord SOCKS5 POST] {TARGET} ...")
print(f"  Proxy: {NORD_SERVER}:1080")
try:
    r = httpx.post(TARGET, proxy=NORD_PROXY, json={}, timeout=30)
    print(f"  Status : {r.status_code}")
    print(f"  Body   : {r.text[:300]}")
    if r.status_code in (400, 401, 422):
        print("  ✓ PROXY WORKS — Polymarket is responding (auth/validation error expected on empty body)")
    elif r.status_code == 403:
        print("  ✗ Still geo-blocked — try a different NordVPN server (see note below)")
    else:
        print(f"  ? Unexpected status — inspect body above")
except Exception as e:
    print(f"  Exception: {type(e).__name__}: {e}")
    if "timed out" in str(e).lower() or "timeout" in str(e).lower():
        print("  ✗ Port 1080 still blocked — check Windows Firewall outbound rule was saved")
    elif "connection refused" in str(e).lower():
        print("  ✗ NordVPN server refused — try a different server hostname")

# ── 2. Check what IP the proxy presents ──────────────────────────────────────
print(f"\nTesting [Nord SOCKS5 GET ipinfo] checking outbound IP ...")
try:
    r = httpx.get("https://ipinfo.io/json", proxy=NORD_PROXY, timeout=15)
    info = r.json()
    print(f"  IP     : {info.get('ip')}")
    print(f"  Org    : {info.get('org')}")
    print(f"  City   : {info.get('city')}, {info.get('region')}, {info.get('country')}")
    if info.get("org", "").lower().startswith("as54854"):
        print("  ✓ Nord residential IP — should bypass Polymarket geo-block")
    else:
        print("  ✓ IP visible above — if org shows M247/datacenter, try a different Nord server")
except Exception as e:
    print(f"  Exception: {type(e).__name__}: {e}")

# ── 3. Direct baseline ────────────────────────────────────────────────────────
print(f"\nTesting [direct no proxy] ...")
try:
    r = httpx.post(TARGET, json={}, timeout=10)
    print(f"  Status : {r.status_code}  (expected 403 — VPS IP geo-blocked)")
except Exception as e:
    print(f"  Exception: {type(e).__name__}: {e}")

print("""
NOTE: If Nord SOCKS5 still returns 403, the server may not have a residential IP.
Try a different server — visit: nordvpn.com/servers/tools/
  → Country: United States  → Protocol: SOCKS5
Copy a hostname (e.g. us6700.nordvpn.com) and set NORDVPN_SERVER=<hostname> in your .env
""")
