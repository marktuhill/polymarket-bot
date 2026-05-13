# Running the Tennis Bot System

Two complementary bots — run simultaneously in separate windows.

---

## Window 1 — Outright winner bot (places orders automatically)

Trades Grand Slam / Masters 1000 winner markets that resolve weeks away.
Requires a working `CLOB_PROXY` in `.env` (US residential IP).

```
python tennis_bot.py --live --schedule 08:00,14:00
```

To trigger immediately (test run):
```
python tennis_bot.py --live --once
```

---

## Window 2 — Match market scanner (alerts only, place bets manually)

Scans for same-day match winner markets every 2 hours.
Sends Telegram alerts — you place bets manually on polymarket.com.
No API order placement (match markets need fast human judgment).

```
python match_scanner.py --schedule 06:00,08:00,10:00,12:00,14:00,16:00,18:00,20:00,22:00
```

To trigger immediately:
```
python match_scanner.py --once
```

---

## Window 3 — Daily resolver (tracks outcomes)

Updates `data/live_trades.csv` with match results once markets resolve.

```
python resolve_trades.py
```

---

## Data files

| File | Contents |
|------|----------|
| `data/live_trades.csv` | Outright winner trades (tennis_bot.py) |
| `data/match_trades.csv` | Match tip alerts (match_scanner.py, paper log) |
| `logs/tennis_bot.log` | Outright bot log |
| `logs/match_scanner.log` | Match scanner log |

---

## .env keys

```
POLYMARKET_PK=...
POLYMARKET_FUNDER=...
POLYMARKET_SIGNATURE_TYPE=2
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_PASSPHRASE=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
SCRAPERAPI_KEY=...
DRY_RUN=false
CLOB_PROXY=http://user:pass@p.webshare.io:80   # required for outright bot on VPS
```
