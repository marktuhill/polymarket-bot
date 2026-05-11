import os
from pathlib import Path
from dotenv import load_dotenv

# Load from BTC bot .env (same wallet)
_ENV_CANDIDATES = [
    Path(r"C:\CUsersMarkpolymarket-momentum-bot\.env"),
    Path(r"C:\Users\markt\polymarket-momentum-bot\.env"),
    Path(r"C:\Users\Mark\polymarket-momentum-bot\.env"),
    Path(__file__).parent / ".env",
]
ENV_PATH = next((p for p in _ENV_CANDIDATES if p.exists()), None)
if ENV_PATH:
    load_dotenv(ENV_PATH)

# Wallet (same as BTC bot — shared Polymarket account)
POLYMARKET_PK             = os.getenv("POLYMARKET_PK", "")
POLYMARKET_FUNDER         = os.getenv("POLYMARKET_FUNDER", "")
POLYMARKET_API_KEY        = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET     = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_PASSPHRASE     = os.getenv("POLYMARKET_PASSPHRASE", "")
POLYMARKET_SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))

# Telegram (same bot)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Position sizing
STAKE_HIGH   = float(os.getenv("STAKE_HIGH",   "10"))   # $ per HIGH signal
STAKE_MEDIUM = float(os.getenv("STAKE_MEDIUM", "5"))    # $ per MEDIUM signal

# Risk controls
MAX_DAILY_LOSS     = float(os.getenv("MAX_DAILY_LOSS",      "30"))
MAX_POSITION_SIZE  = float(os.getenv("MAX_POSITION_SIZE",   "15"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS",    "6"))
MIN_SHARES         = 5

# Bot behaviour — DRY_RUN=true is the safe default
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# Tip sources used by the cross-reference logic (informational — actual
# scraping is wired into signal_engine.gather_supplementary_picks).
SOURCES = ["OLBG", "LastWord", "TennGrand", "Tennisnerd"]

# Multi-source confirmation
# When True, signals require ≥2 sources (OLBG + LastWord and/or TennGrand) to fire.
# When False (default), OLBG alone can fire — but every signal logs which sources
# backed it so we can compare single-source vs multi-source performance.
REQUIRE_MULTI_SOURCE = os.getenv("REQUIRE_MULTI_SOURCE", "false").lower() == "true"

# Tier-1 tournament gate
# When True (default), only consider Polymarket markets for Grand Slam / Masters
# 1000 / WTA 1000 tournaments.  When False, all tennis markets are eligible.
TIER_1_ONLY = os.getenv("TIER_1_ONLY", "true").lower() == "true"

# Minimum Polymarket YES price to consider (cents/100)
# Markets below this are resolved or illiquid — no spread to trade.
MIN_MARKET_PRICE = float(os.getenv("MIN_MARKET_PRICE", "0.03"))
