"""
Finds valid NordVPN SOCKS5 servers and tests them against Polymarket CLOB API.
"""
import httpx
import json

TARGET     = "https://clob.polymarket.com/order"
NORD_USER  = "yojfhwXz4Fd9c2udbgpcpQq7"
NORD_PASS  = "HE8s7fB1pe13FrAaLdAEgUnr"

# ── Step 1: Fetch US servers from NordVPN API ─────────────────────────────────
print("Fetching NordVPN US server list...")
hostnames = []

try:
    # Broader query — get top US servers without SOCKS filter (filter changed)
    resp = httpx.get(
        "https://api.nordvpn.com/v1/servers/recommendations",
        params={"filters[country_id]": 228, "limit": 20},
        timeout=15,
    )
    servers = resp.json()
    print(f"  API returned {len(servers)} servers")

    for s in servers:
        name = s.get("hostname", "")
        techs = [t.get("identifier", "") for t in s.get("technologies", [])]
        if any("socks" in t.lower() or "proxy" in t.lower() for t in techs):
            hostnames.append(name)
            print(f"  SOCKS5 server: {name}  technologies: {techs}")

    if not hostnames:
        # None had SOCKS5 explicitly — NordVPN allows SOCKS5 on all standard servers
        # Just take the top 5 US hostnames
        hostnames = [s["hostname"] for s in servers[:5] if "hostname" in s]
        print(f"  No explicit SOCKS5 flag — will try top servers: {hostnames}")

except Exception as e:
    print(f"  API error: {e}")

# ── Step 2: Hardcoded fallbacks if API fails or returns nothing ───────────────
# NordVPN standard servers all support SOCKS5 with service credentials
FALLBACK_SERVERS = [
    "us10098.nordvpn.com",
    "us9461.nordvpn.com",
    "us8844.nordvpn.com",
    "us7291.nordvpn.com",
    "us6678.nordvpn.com",
]

if not hostnames:
    hostnames = FALLBACK_SERVERS
    print(f"  Using hardcoded fallback servers: {hostnames}")
else:
    # Append fallbacks in case API servers don't support SOCKS5
    hostnames = list(dict.fromkeys(hostnames + FALLBACK_SERVERS))

# ── Step 3: Test each server ─────────────────────────────────────────────────
working_server = None

for hostname in hostnames:
    proxy = f"socks5://{NORD_USER}:{NORD_PASS}@{hostname}:1080"
    print(f"\nTesting {hostname}:1080 ...")
    try:
        r = httpx.get("https://ipinfo.io/json", proxy=proxy, timeout=20)
        info = r.json()
        exit_ip = info.get("ip", "?")
        org     = info.get("org", "?")
        city    = info.get("city", "?")
        print(f"  Exit IP: {exit_ip} — {org} — {city}")

        r2 = httpx.post(TARGET, proxy=proxy, json={}, timeout=20)
        print(f"  Poly:    {r2.status_code} {r2.text[:150]}")

        if r2.status_code in (400, 401, 422):
            print(f"  ✓ WORKING — geo-block bypassed!")
            working_server = hostname
            break
        elif r2.status_code == 403:
            print(f"  ✗ Still geo-blocked via {hostname}")
        else:
            print(f"  ? status {r2.status_code}")

    except httpx.ConnectError as e:
        if "getaddrinfo" in str(e):
            print(f"  ✗ Hostname doesn't resolve (server may not exist) — skipping")
        elif "Connection refused" in str(e):
            print(f"  ✗ Port 1080 refused by server — skipping")
        else:
            print(f"  ✗ ConnectError: {e}")
    except httpx.ConnectTimeout:
        print(f"  ✗ Timeout — port 1080 may still be blocked or server is down")
    except Exception as e:
        print(f"  ✗ {type(e).__name__}: {e}")

print()
if working_server:
    print(f"✓ Working server: {working_server}")
    print(f"  Add to your .env:  NORDVPN_SERVER={working_server}")
else:
    print("✗ No working server found.")
    print("  If all timed out: check wf.msc Outbound Rules — rule must show Allow (green).")
    print("  If all ConnectError/refused: NordVPN service credentials may need refresh at nordvpn.com")
