"""
Tests NordVPN HTTPS proxy (port 89, proxy_ssl) against Polymarket CLOB API.
NordVPN no longer offers SOCKS5 on port 1080 — their current proxy service
is HTTP CONNECT on port 89 using service credentials.
"""
import httpx

TARGET     = "https://clob.polymarket.com/order"
NORD_USER  = "yojfhwXz4Fd9c2udbgpcpQq7"
NORD_PASS  = "HE8s7fB1pe13FrAaLdAEgUnr"
NORD_PORT  = 89  # NordVPN proxy_ssl — HTTP CONNECT on port 89

# ── Step 1: Fetch proxy_ssl servers in the US ────────────────────────────────
print("Fetching NordVPN proxy_ssl (port 89) US servers...")
hostnames = []

try:
    resp = httpx.get(
        "https://api.nordvpn.com/v1/servers/recommendations",
        params={"filters[country_id]": 228, "limit": 20},
        timeout=15,
    )
    servers = resp.json()
    for s in servers:
        name  = s.get("hostname", "")
        techs = [t.get("identifier", "") for t in s.get("technologies", [])]
        if "proxy_ssl" in techs:
            hostnames.append(name)
    print(f"  Found {len(hostnames)} proxy_ssl US servers")
    if hostnames:
        print(f"  First 5: {hostnames[:5]}")
except Exception as e:
    print(f"  API error: {e}")

# Fallback if API fails
if not hostnames:
    hostnames = ["us6946.nordvpn.com", "us9916.nordvpn.com", "us12284.nordvpn.com"]
    print(f"  Using hardcoded fallbacks: {hostnames}")

# ── Step 2: Test each server on port 89 ─────────────────────────────────────
working_server = None

for hostname in hostnames[:5]:
    # HTTP CONNECT proxy (httpx uses http:// scheme for CONNECT tunnel)
    proxy = f"http://{NORD_USER}:{NORD_PASS}@{hostname}:{NORD_PORT}"
    print(f"\nTesting {hostname}:{NORD_PORT} (HTTP CONNECT) ...")
    try:
        # Check exit IP
        r = httpx.get("https://ipinfo.io/json", proxy=proxy, timeout=20)
        info = r.json()
        print(f"  Exit IP : {info.get('ip')} — {info.get('org')} — {info.get('city')}, {info.get('country')}")

        # POST to Polymarket
        r2 = httpx.post(TARGET, proxy=proxy, json={}, timeout=20)
        print(f"  Poly    : {r2.status_code} {r2.text[:150]}")

        if r2.status_code in (400, 401, 422):
            print(f"  ✓ WORKING — geo-block bypassed!")
            working_server = hostname
            break
        elif r2.status_code == 403:
            print(f"  ✗ Still geo-blocked — NordVPN server IP may also be blocked")
        else:
            print(f"  ? status {r2.status_code}")

    except httpx.ProxyError as e:
        print(f"  ✗ ProxyError: {e}")
    except httpx.ConnectError as e:
        print(f"  ✗ ConnectError: {e}")
    except httpx.ConnectTimeout:
        print(f"  ✗ Timeout on port {NORD_PORT}")
    except Exception as e:
        print(f"  ✗ {type(e).__name__}: {e}")

print()
if working_server:
    print(f"✓ Working server: {working_server}:{NORD_PORT}")
    print(f"  Update python_executor.py to use this server on port 89")
else:
    print("✗ No working server found.")
    print("  If all returned 403: NordVPN's servers may also be datacenter IPs blocked by Polymarket.")
    print("  Next step: try a residential proxy service (IPRoyal, Smartproxy) or change VPS provider.")
