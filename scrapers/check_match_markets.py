import requests

r = requests.get(
    "https://gamma-api.polymarket.com/events",
    params={"tag_slug": "tennis", "active": "true", "closed": "false", "limit": 20},
    timeout=15,
)
markets = [
    m["question"]
    for e in r.json()
    for m in e.get("markets", [])
    if "vs" in m.get("question", "").lower() or " v " in m.get("question", "").lower()
]
print(f"Match-format markets: {len(markets)}")
for m in markets[:10]:
    print(" -", m)
