#!/usr/bin/env python3
"""Crypto range monitor & alert scheduler.

Scans Binance USDT-M perpetual futures (``*USDT`` perps) for established price
*ranges* on the 4H timeframe (swing-based support/resistance zones) and sends a
Telegram alert when the 1H close approaches a zone boundary, including
ready-to-use order levels.

What it does
------------
* Pair universe: the top 50 USDT-margined perpetual contracts by 24h quote
  volume with stablecoin pairs removed, plus PAXGUSDT which is always included
  regardless of volume. Refreshed every 24h (along with per-pair tick sizes).
  Note: futures uses 1000x multiplier symbols (e.g. 1000PEPEUSDT), so the levels
  match the exact contract you trade.
* Range detection (every 4h, aligned to Binance 4H bar close): pulls the last
  30 closed 4H candles per pair, computes ATR(14) from scratch, finds swing
  highs/lows, clusters them into support/resistance zones (members within
  +/-0.5*ATR, zone level = mean). A valid range needs >= 2 touches of each zone
  and a width between 0.5*ATR and MAX_RANGE_ATR_MULT*ATR (default 3) that is
  also <= MAX_RANGE_PCT (default 8%) of price, so it stays tight enough to
  round-trip intraday.
* Alert check (every 30 min): pulls the last closed 1H candle per pair and fires
  a buy only when the close sits just above support (a bounce, not a breakdown)
  and a sell only just below resistance, while skipping counter-trend setups
  (TREND_FILTER) and levels a recent 4H bar closed decisively beyond, i.e. a
  reclaim of a broken level (BREAK_FILTER). Each zone alerts once per approach
  (de-dup with hysteresis until price leaves and re-approaches).
* Every alert includes exact order levels (entry / stop / target / R:R) printed
  to the pair's native Binance tick size.

Dependencies: ``requests`` plus the Python standard library only. No pandas,
no numpy.

Environment variables
---------------------
Required (never hardcoded):
    TELEGRAM_BOT_TOKEN   Telegram bot token (from @BotFather)
    TELEGRAM_CHAT_ID     Chat / channel id to send alerts to
Optional (defaults in parentheses):
    TOP_PAIRS            (50)        number of top pairs by 24h quote volume
    ALWAYS_INCLUDE       (PAXGUSDT)  comma-separated symbols always included
    ATR_PERIOD           (14)        ATR period
    RANGE_CANDLES        (30)        number of closed 4H candles to analyse
    SWING_STRENGTH       (2)         bars on each side that define a swing point
    ZONE_ATR_MULT        (0.5)       cluster swings within this * ATR into a zone
    ALERT_ATR_MULT       (0.25)      alert when within this * ATR of a boundary
    STOP_ATR_MULT        (1.0)       stop distance beyond the zone, in ATR
    MAX_RANGE_ATR_MULT   (3.0)       reject ranges wider than this * ATR (keeps
                                     them tight enough to round-trip intraday)
    MAX_RANGE_PCT        (8.0)       reject ranges wider than this % of price
                                     (filters hyper-volatile fresh listings)
    MIN_TOUCHES          (2)         min touches required per zone
    TREND_FILTER         (true)      only long in up/flat trends and short in
                                     down/flat (skip counter-trend setups)
    TREND_FLAT_ATR       (1.0)       window drift > this * ATR counts as a trend
    BREAK_FILTER         (true)      skip a boundary that a recent 4H bar closed
                                     decisively beyond (a reclaim, not a bounce)
    BREAK_ATR            (0.5)       a close > this * ATR beyond a level = break
    BREAK_LOOKBACK       (8)         how many recent 4H bars to scan for a break
    BINANCE_BASE_URL     (https://fapi.binance.com)  USDT-M futures API host
    BINANCE_PROXY        ()          optional http(s) proxy for Binance only,
                                     e.g. http://user:pass@host:port (routes
                                     around region blocks; Telegram stays direct)
These may also be placed in a ``.env`` file next to this script.

Command-line flags
------------------
    --test    Run one full scan cycle, print detected ranges and any alerts to
              the console, and exit WITHOUT sending Telegram messages. Run this
              first to sanity-check before deploying.
    --status  Print all currently detected ranges (levels, touches, age) from the
              saved state file and exit. No Telegram, no network.

Deploying on a Linux VPS (survive reboots)
-----------------------------------------
Runs its own scheduling loop forever. Quick start from the repo folder:
    ./setup.sh                  # venv + deps, captures Telegram creds, runs --test
    sudo ./install_service.sh   # installs+enables a systemd service (auto-restart)
Manage it with:
    systemctl status crypto-range-monitor
    journalctl -u crypto-range-monitor -f
Logs: crypto_range_monitor.log; fired alerts also append to
crypto_range_alerts.csv (both in this script's directory).
"""

import argparse
import csv
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import requests

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "crypto_range_monitor.log")
ALERTS_CSV = os.path.join(SCRIPT_DIR, "crypto_range_alerts.csv")
STATE_FILE = os.path.join(SCRIPT_DIR, "crypto_range_monitor_state.json")
ENV_FILE = os.path.join(SCRIPT_DIR, ".env")

# --------------------------------------------------------------------------- #
# Scheduling constants (seconds)
# --------------------------------------------------------------------------- #
RANGE_INTERVAL = 4 * 60 * 60      # 4h, aligned to UTC (== Binance 4H bar close)
ALERT_INTERVAL = 30 * 60          # 30 minutes
PAIR_REFRESH_INTERVAL = 24 * 60 * 60  # 24 hours
DETECT_DELAY = 30                 # wait this long after bar close before pulling
MAX_SLEEP = 300                   # never sleep longer than this between wakes

CSV_HEADER = ["timestamp", "pair", "direction", "entry", "stop", "target",
              "rr", "zone_high", "zone_low", "atr"]

# Trading pairs whose base asset matches these are skipped (stablecoins).
STABLE_BASES = {
    "USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "UST", "USDD",
    "GUSD", "PAX", "USTC", "EUR", "AEUR", "USD1", "USDe", "RLUSD",
}

logger = logging.getLogger("crypto_range_monitor")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_env_file(path):
    """Populate os.environ from a simple KEY=VALUE .env file (no overrides)."""
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError as exc:
        logger.warning("Could not read .env file %s: %s", path, exc)


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, using default %s", name, os.environ.get(name), default)
        return float(default)


def _env_int(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, using default %s", name, os.environ.get(name), default)
        return int(default)


def _env_bool(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def get_config():
    return {
        "telegram_token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        "top_n": _env_int("TOP_PAIRS", 50),
        "always_include": [s.strip().upper() for s in
                           os.environ.get("ALWAYS_INCLUDE", "PAXGUSDT").split(",") if s.strip()],
        "atr_period": _env_int("ATR_PERIOD", 14),
        "range_candles": _env_int("RANGE_CANDLES", 30),
        "swing_strength": _env_int("SWING_STRENGTH", 2),
        "zone_atr_mult": _env_float("ZONE_ATR_MULT", 0.5),
        "alert_atr_mult": _env_float("ALERT_ATR_MULT", 0.25),
        "stop_atr_mult": _env_float("STOP_ATR_MULT", 1.0),
        "max_range_atr_mult": _env_float("MAX_RANGE_ATR_MULT", 3.0),
        "max_range_pct": _env_float("MAX_RANGE_PCT", 8.0),
        "min_touches": _env_int("MIN_TOUCHES", 2),
        "trend_filter": _env_bool("TREND_FILTER", True),
        "trend_flat_atr": _env_float("TREND_FLAT_ATR", 1.0),
        "break_filter": _env_bool("BREAK_FILTER", True),
        "break_atr": _env_float("BREAK_ATR", 0.5),
        "break_lookback": _env_int("BREAK_LOOKBACK", 8),
        "binance_base_url": os.environ.get("BINANCE_BASE_URL", "https://fapi.binance.com").rstrip("/"),
        "proxy": os.environ.get("BINANCE_PROXY", "").strip(),
    }


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging():
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S UTC"
    )
    fmt.converter = time.gmtime

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


# --------------------------------------------------------------------------- #
# Binance client with rate-limit / network resilience
# --------------------------------------------------------------------------- #
class BinanceClient:
    """Thin Binance REST client with exponential backoff and host failover."""

    def __init__(self, base_url, proxy=None):
        # USDT-M futures (fapi) has no public mirror set, so use the single
        # configured host and rely on backoff/retry instead of host failover.
        self.hosts = [base_url] if base_url else ["https://fapi.binance.com"]
        self.host_index = 0
        self.timeout = 15
        self.max_retries = 6
        self.initial_backoff = 2.0
        self.max_backoff = 64.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "crypto-range-monitor/2.0"})
        # Route only Binance traffic through the proxy (Telegram stays direct), so
        # a geo-blocked host can still reach fapi via an allowed-region proxy.
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    def _rotate_host(self):
        self.host_index = (self.host_index + 1) % len(self.hosts)
        logger.info("Switching Binance host to %s", self.hosts[self.host_index])

    def _get(self, path, params=None):
        """GET with backoff on 429/418/5xx and network errors. Returns parsed
        JSON or None if every retry was exhausted."""
        backoff = self.initial_backoff
        for attempt in range(1, self.max_retries + 1):
            url = self.hosts[self.host_index] + path
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout,
                                        proxies=self.proxies)
            except requests.RequestException as exc:
                logger.warning("Network error (%s) on %s [attempt %d/%d]; retry in %.0fs",
                               exc, path, attempt, self.max_retries, backoff)
                self._rotate_host()
                time.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    logger.warning("Bad JSON from %s: %s", path, exc)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, self.max_backoff)
                    continue

            if resp.status_code in (429, 418):
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                logger.warning("Rate limited (HTTP %d) on %s; backing off %.0fs",
                               resp.status_code, path, wait)
                time.sleep(wait)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            if 500 <= resp.status_code < 600:
                logger.warning("Server error HTTP %d on %s [attempt %d/%d]; retry in %.0fs",
                               resp.status_code, path, attempt, self.max_retries, backoff)
                self._rotate_host()
                time.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            logger.error("HTTP %d on %s: %s", resp.status_code, path, resp.text[:200])
            return None

        logger.error("Giving up on %s after %d attempts", path, self.max_retries)
        return None

    def get_24hr(self):
        """All-symbol 24h ticker stats (list of dicts)."""
        return self._get("/fapi/v1/ticker/24hr")

    def get_exchange_info(self):
        """Full exchange metadata (symbols + filters)."""
        return self._get("/fapi/v1/exchangeInfo")

    def get_klines(self, symbol, interval, limit):
        """Return a list of (high, low, close) tuples for *closed* candles only.

        Binance includes the in-progress candle as the last element, so we
        request one extra and drop it."""
        data = self._get("/fapi/v1/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit + 1})
        if not isinstance(data, list) or len(data) < 2:
            return None
        bars = []
        for kline in data[:-1]:  # drop the still-forming candle
            try:
                bars.append((float(kline[2]), float(kline[3]), float(kline[4])))
            except (IndexError, TypeError, ValueError):
                continue
        return bars


# --------------------------------------------------------------------------- #
# Telegram notifier
# --------------------------------------------------------------------------- #
class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.session = requests.Session()

    def send(self, text):
        """Send a plain-text message; retries with backoff. Returns True/False."""
        backoff = 2.0
        for attempt in range(1, 5):
            try:
                resp = self.session.post(
                    self.url,
                    data={"chat_id": self.chat_id, "text": text,
                          "disable_web_page_preview": True},
                    timeout=15,
                )
            except requests.RequestException as exc:
                logger.warning("Telegram network error: %s [attempt %d]; retry in %.0fs",
                               exc, attempt, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                try:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", backoff)
                except ValueError:
                    retry_after = backoff
                logger.warning("Telegram rate limited; waiting %ss", retry_after)
                time.sleep(float(retry_after))
                continue
            logger.error("Telegram send failed HTTP %d: %s", resp.status_code, resp.text[:200])
            return False
        logger.error("Telegram send failed after retries")
        return False


# --------------------------------------------------------------------------- #
# Price formatting (native Binance tick size)
# --------------------------------------------------------------------------- #
def decimals_from_tick(tick_str):
    """Decimal places implied by a tickSize string, e.g. '0.00010000' -> 4."""
    trimmed = str(tick_str).rstrip("0")
    if "." in trimmed:
        return len(trimmed.split(".", 1)[1])
    return 0


def _fmt_generic(value):
    """Fallback formatting when tick size is unknown."""
    if value == 0:
        return "0"
    if abs(value) >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{value:.6g}"


def to_tick(price, symbol, ticks):
    """Round a price to the pair's tick size (no-op if tick unknown)."""
    info = ticks.get(symbol) if ticks else None
    if info and info.get("tick", 0) > 0:
        return round(price / info["tick"]) * info["tick"]
    return price


def fmt_price(price, symbol, ticks):
    """Format a price to the pair's native tick precision, tick-aligned."""
    info = ticks.get(symbol) if ticks else None
    if info:
        return f"{to_tick(price, symbol, ticks):.{info['dec']}f}"
    return _fmt_generic(price)


# --------------------------------------------------------------------------- #
# ATR(14) from scratch
# --------------------------------------------------------------------------- #
def compute_atr(bars, period):
    """ATR over (high, low, close) bars.

    True range uses the previous close. The first ATR is a simple average of the
    first `period` true ranges; subsequent values use Wilder smoothing:
        ATR_i = (ATR_{i-1} * (period - 1) + TR_i) / period
    Returns None if there is not enough data.
    """
    if not bars or len(bars) < period + 1:
        return None

    true_ranges = []
    for i in range(1, len(bars)):
        high, low, _ = bars[i]
        prev_close = bars[i - 1][2]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    atr = sum(true_ranges[:period]) / period  # simple average seed
    for tr in true_ranges[period:]:           # Wilder smoothing thereafter
        atr = (atr * (period - 1) + tr) / period
    return atr


# --------------------------------------------------------------------------- #
# Swing detection & zone clustering
# --------------------------------------------------------------------------- #
def find_swings(bars, strength, kind):
    """Return [(index, price), ...] of swing highs or lows.

    A swing high at i is a high strictly greater than the `strength` highs on each
    side; a swing low is strictly lower than the lows on each side."""
    out = []
    n = len(bars)
    for i in range(strength, n - strength):
        if kind == "high":
            value = bars[i][0]
            if (all(value > bars[i - j][0] for j in range(1, strength + 1)) and
                    all(value > bars[i + j][0] for j in range(1, strength + 1))):
                out.append((i, value))
        else:
            value = bars[i][1]
            if (all(value < bars[i - j][1] for j in range(1, strength + 1)) and
                    all(value < bars[i + j][1] for j in range(1, strength + 1))):
                out.append((i, value))
    return out


def cluster_levels(points, tol):
    """Greedily group price points whose price is within `tol` of the running
    cluster mean. Returns a list of clusters (each a list of (index, price))."""
    if not points:
        return []
    ordered = sorted(points, key=lambda p: p[1])
    clusters = [[ordered[0]]]
    mean = ordered[0][1]
    for idx, price in ordered[1:]:
        if abs(price - mean) <= tol:
            clusters[-1].append((idx, price))
            mean = sum(p[1] for p in clusters[-1]) / len(clusters[-1])
        else:
            clusters.append([(idx, price)])
            mean = price
    return clusters


def _cluster_mean(cluster):
    return sum(p[1] for p in cluster) / len(cluster)


def _cluster_score(cluster):
    """Rank clusters: more touches first, then more recent (higher max index)."""
    return (len(cluster), max(idx for idx, _ in cluster))


def classify_trend(bars, atr, config):
    """Label the window 'up'/'down'/'flat' by comparing the oldest third of
    closes to the newest third. A drift bigger than TREND_FLAT_ATR*ATR counts as
    a trend; otherwise it's a flat (genuine range)."""
    closes = [b[2] for b in bars]
    n = len(closes)
    if n < 6 or atr <= 0:
        return "flat"
    k = max(1, n // 3)
    early = sum(closes[:k]) / k
    late = sum(closes[-k:]) / k
    band = config["trend_flat_atr"] * atr
    if late - early > band:
        return "up"
    if late - early < -band:
        return "down"
    return "flat"


def detect_range(symbol, bars, config):
    """Build a swing-based range record from 4H bars, or None if not valid."""
    atr = compute_atr(bars, config["atr_period"])
    if not atr or atr <= 0:
        return None

    strength = config["swing_strength"]
    tol = config["zone_atr_mult"] * atr
    min_touches = config["min_touches"]

    highs = cluster_levels(find_swings(bars, strength, "high"), tol)
    lows = cluster_levels(find_swings(bars, strength, "low"), tol)
    res_clusters = [c for c in highs if len(c) >= min_touches]
    sup_clusters = [c for c in lows if len(c) >= min_touches]
    if not res_clusters or not sup_clusters:
        return None

    resistance = max(res_clusters, key=_cluster_score)
    res_level = _cluster_mean(resistance)

    # Support must sit below resistance to form a real range.
    below = [c for c in sup_clusters if _cluster_mean(c) < res_level]
    if not below:
        return None
    support = max(below, key=_cluster_score)
    sup_level = _cluster_mean(support)

    # Zones must be far enough apart that the alert bands stay distinct, but not
    # so wide the range can't round-trip intraday (max width capped in ATR).
    width = res_level - sup_level
    if width <= 2 * config["alert_atr_mult"] * atr:
        return None
    if width > config["max_range_atr_mult"] * atr:
        return None
    # Absolute tightness cap: skip ranges too wide (in %) to round-trip in a day,
    # e.g. freshly listed coins whose ATR is huge but the box still spans 50%+.
    if sup_level > 0 and width / sup_level > config["max_range_pct"] / 100.0:
        return None

    # Recent-break flags: did a recent 4H bar CLOSE decisively beyond a boundary?
    # If so the level was broken and is now being retested (a reclaim), not a
    # clean bounce -- the alert filter uses these to suppress such setups.
    recent = bars[-max(1, config["break_lookback"]):]
    break_band = config["break_atr"] * atr
    support_broken = any(b[2] < sup_level - break_band for b in recent)
    resistance_broken = any(b[2] > res_level + break_band for b in recent)

    return {
        "pair": symbol,
        "support": sup_level,
        "resistance": res_level,
        "atr": atr,
        "support_touches": len(support),
        "resistance_touches": len(resistance),
        "trend": classify_trend(bars, atr, config),
        "support_broken": support_broken,
        "resistance_broken": resistance_broken,
        "detected_at": int(time.time()),
        "alerted_support": False,
        "alerted_resistance": False,
    }


# --------------------------------------------------------------------------- #
# Pair universe
# --------------------------------------------------------------------------- #
def _futures_perp_universe(client):
    """Return {symbol: {tick, dec}} for tradable USDT-margined perpetuals.

    Built from futures ``exchangeInfo`` so the universe excludes delivery
    (quarterly) contracts, halted symbols, and non-USDT-margined perps, and so
    entry/stop/target round to each contract's real tick size. Returns ``{}``
    if exchangeInfo is unavailable (caller falls back gracefully)."""
    info = client.get_exchange_info()
    if not isinstance(info, dict):
        logger.warning("Could not load futures exchangeInfo; "
                       "universe filter and tick sizes unavailable this cycle")
        return {}
    universe = {}
    for sym_info in info.get("symbols", []):
        if sym_info.get("quoteAsset") != "USDT":
            continue
        if sym_info.get("status") != "TRADING":
            continue
        if sym_info.get("contractType") != "PERPETUAL":
            continue
        symbol = sym_info.get("symbol")
        for flt in sym_info.get("filters", []):
            if flt.get("filterType") == "PRICE_FILTER":
                tick = flt.get("tickSize", "0")
                universe[symbol] = {"tick": float(tick), "dec": decimals_from_tick(tick)}
                break
    return universe


def refresh_pairs(state, client, config):
    """Refresh the pair universe and per-pair tick sizes.

    Universe = top N USDT-margined perpetual contracts by 24h quote volume with
    stablecoin pairs removed, plus any ``always_include`` symbols regardless of
    volume.
    """
    data = client.get_24hr()
    if not isinstance(data, list):
        logger.warning("Pair refresh failed; keeping %d existing pair(s)",
                       len(state.get("pairs", [])))
        return state.get("pairs", [])

    perp_ticks = _futures_perp_universe(client)

    available = set()
    candidates = []
    for row in data:
        symbol = row.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        # Restrict to live perpetuals when we have the universe; otherwise the
        # endswith check alone still excludes delivery contracts (they carry an
        # underscore, e.g. BTCUSDT_240927).
        if perp_ticks and symbol not in perp_ticks:
            continue
        available.add(symbol)
        if symbol[:-4] in STABLE_BASES:  # remove stablecoin pairs only
            continue
        try:
            quote_volume = float(row.get("quoteVolume", 0))
        except (TypeError, ValueError):
            continue
        candidates.append((symbol, quote_volume))

    candidates.sort(key=lambda x: x[1], reverse=True)
    pairs = [sym for sym, _ in candidates[:config["top_n"]]]

    forced = [sym for sym in config["always_include"]
              if sym in available and sym not in pairs]
    pairs.extend(forced)

    state["pairs"] = pairs
    state["last_pair_refresh"] = int(time.time())
    if perp_ticks:
        state["ticks"] = {sym: perp_ticks[sym] for sym in pairs if sym in perp_ticks}
    logger.info("Pair universe refreshed: %d perpetual(s) (top %d by 24h quote volume%s)",
                len(pairs), config["top_n"],
                "; forced: " + ", ".join(forced) if forced else "")
    return pairs


# --------------------------------------------------------------------------- #
# Detection & alerting
# --------------------------------------------------------------------------- #
def run_range_detection(state, client, config):
    """Recompute swing-based ranges for all monitored pairs (every 4h)."""
    if not state.get("pairs"):
        refresh_pairs(state, client, config)

    pairs = state.get("pairs", [])
    ranges = state.setdefault("ranges", {})
    logger.info("Range detection starting for %d pair(s)", len(pairs))
    detected = 0

    for symbol in pairs:
        bars = client.get_klines(symbol, "4h", config["range_candles"])
        if not bars or len(bars) < config["atr_period"] + 1:
            ranges.pop(symbol, None)
            continue
        new_range = detect_range(symbol, bars, config)
        if new_range is None:
            ranges.pop(symbol, None)
            continue

        # Preserve de-dup flags when the zones barely moved, so a pair parked at
        # a boundary doesn't re-alert every 4h.
        old = ranges.get(symbol)
        if old:
            tol = 0.1 * new_range["atr"]
            if (abs(new_range["resistance"] - old["resistance"]) < tol and
                    abs(new_range["support"] - old["support"]) < tol):
                new_range["alerted_support"] = old.get("alerted_support", False)
                new_range["alerted_resistance"] = old.get("alerted_resistance", False)
        ranges[symbol] = new_range
        detected += 1

    for stale in [s for s in ranges if s not in pairs]:
        ranges.pop(stale, None)

    logger.info("Range detection complete: %d valid range(s)", detected)
    return detected


def _order_levels(side, rng, config):
    """Return (direction, entry, stop, target, rr) for a buy or sell setup."""
    atr = rng["atr"]
    sup = rng["support"]
    res = rng["resistance"]
    stop_dist = config["stop_atr_mult"] * atr
    if side == "support":
        direction = "BUY AT SUPPORT"
        entry, stop, target = sup, sup - stop_dist, res
        rr = (target - entry) / (entry - stop) if entry != stop else 0.0
    else:
        direction = "SELL AT RESISTANCE"
        entry, stop, target = res, res + stop_dist, sup
        rr = (entry - target) / (stop - entry) if stop != entry else 0.0
    return direction, entry, stop, target, rr


def build_alert_message(symbol, side, rng, current, config, ticks):
    direction, entry, stop, target, rr = _order_levels(side, rng, config)

    def disp(price):
        return fmt_price(price, symbol, ticks)

    divider = "─" * 21  # box-drawing horizontal line
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return "\n".join([
        f"\U0001F514 RANGE ALERT — {symbol}",
        f"Direction: {direction}",
        divider,
        f"Zone High:  {disp(rng['resistance'])}",
        f"Zone Low:   {disp(rng['support'])}",
        f"Current:    {disp(current)}",
        divider,
        f"ENTRY:      {disp(entry)}",
        f"STOP:       {disp(stop)}",
        f"TARGET:     {disp(target)}",
        f"R:R:        1:{rr:.2f}",
        divider,
        f"ATR(14):    {disp(rng['atr'])}",
        f"Touches:    {rng['support_touches']}/{rng['resistance_touches']}",
        f"Time:       {when}",
    ])


def _record_alert_csv(symbol, side, rng, config, ticks):
    direction, entry, stop, target, rr = _order_levels(side, rng, config)

    def disp(price):
        return fmt_price(price, symbol, ticks)

    row = [
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        symbol, direction, disp(entry), disp(stop), disp(target),
        f"{rr:.2f}", disp(rng["resistance"]), disp(rng["support"]), disp(rng["atr"]),
    ]
    try:
        new_file = not os.path.isfile(ALERTS_CSV)
        with open(ALERTS_CSV, "a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(CSV_HEADER)
            writer.writerow(row)
    except OSError as exc:
        logger.warning("Could not write alert CSV: %s", exc)


def _emit_alert(symbol, side, rng, current, config, ticks, notifier, dry_run):
    message = build_alert_message(symbol, side, rng, current, config, ticks)
    direction = "BUY AT SUPPORT" if side == "support" else "SELL AT RESISTANCE"
    logger.info("ALERT %s %s (1H close=%s)", symbol, direction, fmt_price(current, symbol, ticks))
    if dry_run:
        print("\n[TEST] Would send Telegram alert:\n" + message)
        return
    if notifier.send(message):
        _record_alert_csv(symbol, side, rng, config, ticks)


def evaluate_alert(close, rng, config):
    """The single source of truth for firing: return 'support' (buy),
    'resistance' (sell), or None for a given 1H close, applying the proximity
    band, bounce-only direction, and trend filter."""
    atr = rng["atr"]
    prox = config["alert_atr_mult"] * atr
    sup = rng["support"]
    res = rng["resistance"]
    trend = rng.get("trend", "flat")
    tf = config["trend_filter"]
    bf = config["break_filter"]
    if (sup <= close <= sup + prox and not (tf and trend == "down")
            and not (bf and rng.get("support_broken"))):
        return "support"
    if (res - prox <= close <= res and not (tf and trend == "up")
            and not (bf and rng.get("resistance_broken"))):
        return "resistance"
    return None


def run_alert_check(state, client, config, notifier, dry_run=False):
    """Compare the latest 1H close to each zone and alert near boundaries (30m)."""
    ranges = state.get("ranges", {})
    ticks = state.get("ticks", {})
    if not ranges:
        logger.info("Alert check: no active ranges")
        return 0

    checked = 0
    fired = 0
    for symbol, rng in ranges.items():
        bars = client.get_klines(symbol, "1h", 2)
        if not bars:
            continue
        close = bars[-1][2]  # latest closed 1H close
        checked += 1
        proximity = config["alert_atr_mult"] * rng["atr"]
        sup = rng["support"]
        res = rng["resistance"]
        side = evaluate_alert(close, rng, config)

        # Bounce off support -> buy. Re-arm only once price leaves the band.
        if side == "support":
            if not rng.get("alerted_support"):
                _emit_alert(symbol, "support", rng, close, config, ticks, notifier, dry_run)
                rng["alerted_support"] = True
                fired += 1
        elif abs(close - sup) > 1.5 * proximity:
            rng["alerted_support"] = False

        # Rejection at resistance -> sell.
        if side == "resistance":
            if not rng.get("alerted_resistance"):
                _emit_alert(symbol, "resistance", rng, close, config, ticks, notifier, dry_run)
                rng["alerted_resistance"] = True
                fired += 1
        elif abs(close - res) > 1.5 * proximity:
            rng["alerted_resistance"] = False

    logger.info("Alert check complete: %d pair(s) checked, %d alert(s)", checked, fired)
    return fired


# --------------------------------------------------------------------------- #
# State persistence (survive reboots; also feeds --status)
# --------------------------------------------------------------------------- #
def _empty_state():
    return {"pairs": [], "ranges": {}, "ticks": {}, "last_pair_refresh": 0}


def load_state():
    if not os.path.isfile(STATE_FILE):
        return _empty_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        for key, default in _empty_state().items():
            state.setdefault(key, default)
        logger.info("Loaded state: %d pair(s), %d active range(s)",
                    len(state["pairs"]), len(state["ranges"]))
        return state
    except (OSError, ValueError) as exc:
        logger.warning("Could not load state (%s); starting fresh", exc)
        return _empty_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        logger.warning("Could not save state: %s", exc)


# --------------------------------------------------------------------------- #
# Console reporting (--status / --test)
# --------------------------------------------------------------------------- #
def print_ranges(state):
    ranges = state.get("ranges", {})
    ticks = state.get("ticks", {})
    if not ranges:
        print("No ranges currently detected.")
        return
    now = time.time()
    print(f"Detected ranges: {len(ranges)}")
    print("-" * 84)
    print(f"{'PAIR':<14}{'SUPPORT':>15}{'RESISTANCE':>15}{'TOUCHES S/R':>14}{'TREND':>8}{'AGE(h)':>10}")
    print("-" * 84)
    for symbol in sorted(ranges):
        rng = ranges[symbol]
        age = (now - rng.get("detected_at", now)) / 3600.0
        touches = f"{rng['support_touches']}/{rng['resistance_touches']}"
        trend = rng.get("trend", "flat")
        print(f"{symbol:<14}"
              f"{fmt_price(rng['support'], symbol, ticks):>15}"
              f"{fmt_price(rng['resistance'], symbol, ticks):>15}"
              f"{touches:>14}{trend:>8}{age:>10.1f}")


# --------------------------------------------------------------------------- #
# Scheduling helpers
# --------------------------------------------------------------------------- #
def next_aligned(now_ts, period, offset=0):
    """Next UTC-aligned timestamp strictly after now_ts for the given period."""
    n = math.floor((now_ts - offset) / period) + 1
    return n * period + offset


# --------------------------------------------------------------------------- #
# Run modes
# --------------------------------------------------------------------------- #
def _range_status(close, rng, config):
    """Short human label for where price sits and whether it would alert."""
    atr = rng["atr"]
    sup = rng["support"]
    res = rng["resistance"]
    prox = config["alert_atr_mult"] * atr
    trend = rng.get("trend", "flat")
    tf = config["trend_filter"]
    if close < sup:
        return "below support (broken)"
    if close > res:
        return "above resist (broken)"
    # Defer the fire/no-fire decision to the same rule the live bot uses.
    side = evaluate_alert(close, rng, config)
    if side == "support":
        return ">> BUY signal"
    if side == "resistance":
        return ">> SELL signal"
    bf = config["break_filter"]
    if close <= sup + prox:
        if tf and trend == "down":
            return "at support (trend block)"
        if bf and rng.get("support_broken"):
            return "at support (recent break)"
    if close >= res - prox:
        if tf and trend == "up":
            return "at resist (trend block)"
        if bf and rng.get("resistance_broken"):
            return "at resist (recent break)"
    return "mid-range"


def print_range_status(state, client, config):
    """For each detected range, fetch the latest 1H close and show where price
    sits relative to the boundaries, so it's clear which setups are close to
    triggering vs. parked mid-range."""
    ranges = state.get("ranges", {})
    ticks = state.get("ticks", {})
    signals = []
    if not ranges:
        return signals
    print("\n--- Live position vs range (latest 1H close) ---")
    print("-" * 92)
    print(f"{'PAIR':<14}{'CLOSE':>14}{'TREND':>7}"
          f"{'TO SUP(ATR)':>13}{'TO RES(ATR)':>13}{'STATUS':>26}")
    print("-" * 92)
    for symbol in sorted(ranges):
        rng = ranges[symbol]
        bars = client.get_klines(symbol, "1h", 2)
        if not bars:
            continue
        close = bars[-1][2]
        atr = rng["atr"]
        d_sup = (close - rng["support"]) / atr if atr else 0.0   # + above support
        d_res = (rng["resistance"] - close) / atr if atr else 0.0  # + below resistance
        print(f"{symbol:<14}{fmt_price(close, symbol, ticks):>14}"
              f"{rng.get('trend', 'flat'):>7}{d_sup:>13.2f}{d_res:>13.2f}"
              f"{_range_status(close, rng, config):>26}")
        side = evaluate_alert(close, rng, config)
        if side:
            signals.append((symbol, side, close))
    return signals


def run_test(state, client, config):
    """One full scan cycle printed to the console; sends no Telegram messages."""
    logger.info("TEST MODE: one full scan cycle (no Telegram sends)")
    client.max_retries = 2  # fail fast for quick feedback

    pairs = refresh_pairs(state, client, config)
    print(f"\nPairs fetched: {len(pairs)} | "
          f"PAXGUSDT included: {'yes' if 'PAXGUSDT' in pairs else 'no'}")
    if not pairs:
        print("Could not fetch pairs from Binance (check network/region). Aborting test.")
        return

    n = run_range_detection(state, client, config)
    print(f"\nValid ranges detected: {n}\n")
    print_ranges(state)

    # One fetch pass: the position view and the alerts below come from the same
    # 1H closes, so a ">> signal" row always matches what would fire.
    signals = print_range_status(state, client, config)

    print("\n--- Alerts that would fire now ---")
    if not signals:
        print("None at current prices.")
    else:
        ticks = state.get("ticks", {})
        for symbol, side, close in signals:
            _emit_alert(symbol, side, state["ranges"][symbol], close, config,
                        ticks, notifier=None, dry_run=True)
    print("\nTEST complete. No Telegram messages were sent.")


def run_loop(state, client, config, notifier):
    """The continuous scheduler: detection (4h), alerts (30m), refresh (24h)."""
    pairs = refresh_pairs(state, client, config)

    logger.info("=" * 60)
    logger.info("Crypto Range Monitor started (USDT-M futures: %s%s)",
                config["binance_base_url"],
                "; via proxy" if config["proxy"] else "")
    logger.info("Env vars loaded: TELEGRAM_BOT_TOKEN (set), TELEGRAM_CHAT_ID=%s",
                config["telegram_chat_id"])
    logger.info("Config: top %d pairs by 24h volume, always include [%s], "
                "ATR(%d) on %d x 4H bars, zone=%.2fxATR, alert=%.2fxATR, stop=%.2fxATR, "
                "max width=%.2fxATR/%.1f%%, min touches=%d, trend filter=%s, "
                "break filter=%s",
                config["top_n"], ", ".join(config["always_include"]) or "none",
                config["atr_period"], config["range_candles"], config["zone_atr_mult"],
                config["alert_atr_mult"], config["stop_atr_mult"],
                config["max_range_atr_mult"], config["max_range_pct"],
                config["min_touches"], "on" if config["trend_filter"] else "off",
                "on" if config["break_filter"] else "off")
    logger.info("Monitoring %d perpetual(s); PAXGUSDT included: %s",
                len(pairs), "yes" if "PAXGUSDT" in pairs else "no")
    logger.info("Schedule: range detection every 4h (UTC-aligned), "
                "alert check every 30m, pair refresh every 24h")
    logger.info("=" * 60)

    try:
        run_range_detection(state, client, config)
        run_alert_check(state, client, config, notifier)
        save_state(state)
    except Exception as exc:  # noqa: BLE001 - never let startup work crash the loop
        logger.exception("Error during initial cycle: %s", exc)

    now = time.time()
    next_detect = next_aligned(now, RANGE_INTERVAL) + DETECT_DELAY
    next_alert = next_aligned(now, ALERT_INTERVAL)
    next_refresh = next_aligned(now, PAIR_REFRESH_INTERVAL)
    logger.info("Next range detection in %.0f min; next alert check in %.0f min",
                (next_detect - now) / 60, (next_alert - now) / 60)

    while True:
        try:
            now = time.time()

            if now >= next_refresh:
                refresh_pairs(state, client, config)
                save_state(state)
                next_refresh = next_aligned(time.time(), PAIR_REFRESH_INTERVAL)

            if now >= next_detect:
                run_range_detection(state, client, config)
                save_state(state)
                next_detect = next_aligned(time.time(), RANGE_INTERVAL) + DETECT_DELAY

            if now >= next_alert:
                run_alert_check(state, client, config, notifier)
                save_state(state)
                next_alert = next_aligned(time.time(), ALERT_INTERVAL)

            sleep_for = max(1.0, min(next_detect, next_alert, next_refresh) - time.time())
            time.sleep(min(sleep_for, MAX_SLEEP))

        except KeyboardInterrupt:
            logger.info("Shutting down (keyboard interrupt)")
            save_state(state)
            break
        except Exception as exc:  # noqa: BLE001 - keep the loop alive on any error
            logger.exception("Unexpected error in main loop: %s; continuing", exc)
            time.sleep(30)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args():
    parser = argparse.ArgumentParser(
        description="Monitor Binance USDT pairs and alert on range-boundary approaches.")
    parser.add_argument("--test", "--once", action="store_true", dest="test",
                        help="Run one full scan cycle, print ranges and alerts to "
                             "the console, and exit (no Telegram messages).")
    parser.add_argument("--status", action="store_true",
                        help="Print currently detected ranges from the saved state "
                             "and exit (no Telegram, no network).")
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging()
    load_env_file(ENV_FILE)
    config = get_config()

    if args.status:
        print_ranges(load_state())
        return

    client = BinanceClient(config["binance_base_url"], config["proxy"])
    state = load_state()

    if args.test:
        run_test(state, client, config)
        return

    missing = [name for name, key in (("TELEGRAM_BOT_TOKEN", "telegram_token"),
                                      ("TELEGRAM_CHAT_ID", "telegram_chat_id"))
               if not config[key]]
    if missing:
        logger.error("Missing required environment variable(s): %s", ", ".join(missing))
        logger.error("Set them in the environment or in %s and restart.", ENV_FILE)
        sys.exit(1)

    notifier = TelegramNotifier(config["telegram_token"], config["telegram_chat_id"])
    run_loop(state, client, config, notifier)


if __name__ == "__main__":
    main()
