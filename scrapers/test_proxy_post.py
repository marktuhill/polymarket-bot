"""
Tests NordVPN proxy variants against Polymarket CLOB API POST /order.
proxy_ssl (port 89) requires TLS — testing both http:// and https:// schemes.
"""
import httpx

TARGET     = "https://clob.polymarket.com/order"
NORD_USER  = "yojfhwXz4Fd9c2udbgpcpQq7"
NORD_PASS  = "HE8s7fB1pe13FrAaLdAEgUnr"

# Best servers from previous API call
SERVERS = [
    "us9337.nordvpn.com",
    "us11704.nordvpn.com",
    "us9352.nordvpn.com",
]

def test_proxy(label, proxy_url, timeout=20):
    print(f"\n  [{label}]  proxy={proxy_url.split('@')[1]}")
    try:
        r = httpx.get("https://ipinfo.io/json", proxy=proxy_url,
                      verify=False, timeout=timeout)
        info = r.json()
        print(f"    ipinfo : {info.get('ip')} — {info.get('org')}")
        r2 = httpx.post(TARGET, proxy=proxy_url, json={},
                        verify=False, timeout=timeout)
        print(f"    Poly   : {r2.status_code} {r2.text[:120]}")
        return r2.status_code in (400, 401, 422)
    except httpx.RemoteProtocolError as e:
        print(f"    ✗ RemoteProtocolError: {e}")
    except httpx.ConnectError as e:
        print(f"    ✗ ConnectError: {e}")
    except httpx.ConnectTimeout:
        print(f"    ✗ Timeout")
    except Exception as e:
        print(f"    ✗ {type(e).__name__}: {e}")
    return False

working = None

for server in SERVERS:
    print(f"\n=== {server} ===")

    # Variant A: https:// on port 89 (TLS to proxy — matches proxy_ssl)
    if test_proxy("https port 89", f"https://{NORD_USER}:{NORD_PASS}@{server}:89"):
        working = (server, 89, "https")
        break

    # Variant B: http:// on port 80 (plain HTTP proxy)
    if test_proxy("http  port 80", f"http://{NORD_USER}:{NORD_PASS}@{server}:80"):
        working = (server, 80, "http")
        break

    # Variant C: http:// on port 443 (some providers use this)
    if test_proxy("http  port 443", f"http://{NORD_USER}:{NORD_PASS}@{server}:443"):
        working = (server, 443, "http")
        break

print()
if working:
    host, port, scheme = working
    print(f"✓ Working: {scheme}://<creds>@{host}:{port}")
else:
    print("✗ NordVPN proxy is not viable for this use case.")
    print()
    print("Recommended next step — Webshare.io residential proxy (free tier available):")
    print("  1. Sign up at webshare.io")
    print("  2. Go to Proxy List -> Download -> HTTP format")
    print("  3. Add to .env:  WEBSHARE_PROXY=http://user:pass@host:port")
    print()
    print("OR — run the bot on your local PC instead of the VPS:")
    print("  Your home internet is a residential IP that Polymarket allows.")
    print("  Connect NordVPN on your PC and run python tennis_bot.py --live")
