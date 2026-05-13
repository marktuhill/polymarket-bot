"""
Finds valid NordVPN SOCKS5 servers via NordVPN API, then tests them
against Polymarket CLOB API POST /order.
"""
import httpx
import json

TARGET     = "https://clob.polymarket.com/order"
NORD_USER  = "yojfhwXz4Fd9c2udbgpcpQq7"
NORD_PASS  = "HE8s7fB1pe13FrAaLdAEgUnr"

# ── Step 1: Fetch valid NordVPN SOCKS5 servers in the US ─────────────────────
print("Fetching NordVPN SOCKS5 server list (US) from NordVPN API...")
NORD_API = (
    "https://api.nordvpn.com/v1/servers/recommendations"
    "?filters[servers_technologies][identifier]=socks"
    "&filters[country_id]=228"   # 228 = United States
    "&limit=5"
)
try:
    resp = httpx.get(NORD_API, timeout=15)
    servers = resp.json()
    hostnames = [s["hostname"] for s in servers if "hostname" in s]
    if not hostnames:
        print("  No servers returned — printing raw response:")
        print(resp.text[:500])
    else:
        print(f"  Found {len(hostnames)} server(s): {hostnames}")
except Exception as e:
    print(f"  API error: {e}")
    hostnames = []

# ── Step 2: Test each server ─────────────────────────────────────────────────
working_server = None

for hostname in hostnames:
    proxy = f"socks5://{NORD_USER}:{NORD_PASS}@{hostname}:1080"
    print(f"\nTesting [Nord SOCKS5] {hostname}:1080 ...")
    try:
        # Check what IP we exit from
        r = httpx.get("https://ipinfo.io/json", proxy=proxy, timeout=20)
        info = r.json()
        print(f"  Exit IP  : {info.get('ip')} — {info.get('org')} — {info.get('city')}, {info.get('region')}")

        # Now test POST to Polymarket
        r2 = httpx.post(TARGET, proxy=proxy, json={}, timeout=20)
        print(f"  Poly POST: {r2.status_code} {r2.text[:200]}")

        if r2.status_code in (400, 401, 422):
            print(f"  ✓ WORKING — Polymarket responded (auth/validation error on empty body is expected)")
            working_server = hostname
            break
        elif r2.status_code == 403:
            print(f"  ✗ Still geo-blocked via this server — trying next...")
        else:
            print(f"  ? Unexpected status")

    except Exception as e:
        print(f"  Exception: {type(e).__name__}: {e}")

# ── Summary ───────────────────────────────────────────────────────────────────
print()
if working_server:
    print(f"✓ Working NordVPN SOCKS5 server: {working_server}")
    print(f"  Add this to your .env:")
    print(f"  NORDVPN_SERVER={working_server}")
else:
    print("✗ No working server found in the top 5 recommendations.")
    print("  Check that the Windows Firewall outbound rule for TCP 1080 was saved.")
    print("  You can verify: wf.msc -> Outbound Rules -> look for your new rule -> ensure it says 'Allow'")
