#!/usr/bin/env python3
"""Crypto range monitor & alert scheduler.

Scans Binance USDT-M perpetual futures (``*USDT`` perps) for established price
*ranges* on the 4H timeframe (swing-based support/resistance zones) and sends a
Telegram alert when a lower-timeframe close (default 15m) approaches a zone
boundary, including ready-to-use order levels.

What it does
------------
* Pair universe: the top 100 USDT-margined perpetual contracts by 24h quote
  volume with stablecoin pairs removed, plus PAXGUSDT which is always included
  regardless of volume. Refreshed every 24h (along with per-pair tick sizes).
  Note: futures uses 1000x multiplier symbols (e.g. 1000PEPEUSDT), so the levels
  match the exact contract you trade.
* Range detection (every 4h, aligned to Binance 4H bar close): pulls the last
  30 closed 4H candles per pair, computes ATR(14) from scratch, finds swing
  highs/lows, clusters them into support/resistance zones (members within
  +/-0.5*ATR, zone level = mean). A valid range needs >= 2 *genuine* touches of
  each side -- touches only count as separate if price left the zone and came
  back between them, so a burst of pivots from one visit counts once -- and a
  width between 0.5*ATR and MAX_RANGE_ATR_MULT*ATR (default 3) that is
  also <= MAX_RANGE_PCT (default 8%) of price, so it stays tight enough to
  round-trip intraday.
* Alert check (every ALERT_INTERVAL_MIN min, default 10): pulls the last closed
  ALERT_TIMEFRAME candle (default 15m) per pair and fires
  a buy only when the close sits just above support (a bounce, not a breakdown)
  and a sell only just below resistance, while skipping counter-trend setups
  (TREND_FILTER) and levels a recent 4H bar closed decisively beyond, i.e. a
  reclaim of a broken level (BREAK_FILTER). Each zone alerts once per approach
  (de-dup with hysteresis until price leaves and re-approaches).
* Every alert includes exact order levels (entry / stop / target / R:R) printed
  to the pair's native Binance tick size. Entry is the level, target the opposite
  level, and the stop sits just beyond the level's wick extreme (STOP_BUFFER_ATR).

Dependencies: ``requests`` plus the Python standard library only. No pandas,
no numpy.

Environment variables
---------------------
Required (never hardcoded):
    TELEGRAM_BOT_TOKEN   Telegram bot token (from @BotFather)
    TELEGRAM_CHAT_ID     Chat / channel id to send alerts to
Optional (defaults in parentheses):
    TOP_PAIRS            (100)       number of top pairs by 24h quote volume
    ALWAYS_INCLUDE       (PAXGUSDT)  comma-separated symbols always included
    ATR_PERIOD           (14)        ATR period
    RANGE_CANDLES        (30)        number of closed 4H candles to analyse
    SWING_STRENGTH       (2)         bars on each side that define a swing point
    ZONE_ATR_MULT        (0.5)       cluster swings within this * ATR into a zone
    ALERT_ATR_MULT       (0.25)      alert when within this * ATR of a boundary
    ENTRY_OFFSET_ATR     (0.05)      place entry this * ATR inside the level so it
                                     fills on entry into the zone (cuts no-fills)
    STOP_BUFFER_ATR      (0.1)       stop sits this * ATR beyond the level's wick
                                     extreme (tight, structure-based stop)
    STOP_ATR_MULT        (1.0)       fallback stop distance in ATR (only used if
                                     wick data is missing)
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
    ALERT_TIMEFRAME      (15m)       candle close used as the alert trigger
                                     (1m/3m/5m/15m/30m/1h; lower = faster, noisier)
    ALERT_INTERVAL_MIN   (10)        minutes between alert checks
    ALERT_COOLDOWN_HOURS (6)         min hours between alerts on the same level/
                                     side (stops one level chopping out repeats)
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
    --review  Forward-test logged alerts: for each row in the alert CSV, fetch
              later 5m price and report whether it filled and hit target or stop
              first, plus win-rate / expectancy. Read-only (needs Binance).
    --experiment  Sweep stop x target exit rules over the logged alerts and print
              win-rate / expectancy grids to search for a better exit. Read-only.

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
PAIR_REFRESH_INTERVAL = 24 * 60 * 60  # 24 hours
DETECT_DELAY = 30                 # wait this long after bar close before pulling
MAX_SLEEP = 120                   # never sleep longer than this between wakes

CSV_HEADER = ["timestamp", "pair", "direction", "entry", "stop", "target",
              "rr", "zone_high", "zone_low", "atr", "regime", "box_broken"]

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


_VALID_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h"}


def _env_timeframe(name, default):
    tf = os.environ.get(name, default).strip()
    if tf not in _VALID_TIMEFRAMES:
        logger.warning("Invalid %s=%r, using %s", name, tf, default)
        return default
    return tf


def get_config():
    return {
        "telegram_token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        "top_n": _env_int("TOP_PAIRS", 100),
        "always_include": [s.strip().upper() for s in
                           os.environ.get("ALWAYS_INCLUDE", "PAXGUSDT").split(",") if s.strip()],
        "atr_period": _env_int("ATR_PERIOD", 14),
        "range_candles": _env_int("RANGE_CANDLES", 30),
        "swing_strength": _env_int("SWING_STRENGTH", 2),
        "zone_atr_mult": _env_float("ZONE_ATR_MULT", 0.5),
        "alert_atr_mult": _env_float("ALERT_ATR_MULT", 0.25),
        "stop_atr_mult": _env_float("STOP_ATR_MULT", 1.0),
        "stop_buffer_atr": _env_float("STOP_BUFFER_ATR", 0.1),
        "entry_offset_atr": _env_float("ENTRY_OFFSET_ATR", 0.05),
        "max_range_atr_mult": _env_float("MAX_RANGE_ATR_MULT", 3.0),
        "max_range_pct": _env_float("MAX_RANGE_PCT", 8.0),
        "min_touches": _env_int("MIN_TOUCHES", 2),
        "trend_filter": _env_bool("TREND_FILTER", True),
        "trend_flat_atr": _env_float("TREND_FLAT_ATR", 1.0),
        "break_filter": _env_bool("BREAK_FILTER", True),
        "break_atr": _env_float("BREAK_ATR", 0.5),
        "break_lookback": _env_int("BREAK_LOOKBACK", 8),
        "review_fill_hours": _env_float("REVIEW_FILL_HOURS", 4.0),
        "review_hold_hours": _env_float("REVIEW_HOLD_HOURS", 36.0),
        "alert_timeframe": _env_timeframe("ALERT_TIMEFRAME", "15m"),
        "alert_interval_min": _env_int("ALERT_INTERVAL_MIN", 10),
        "alert_cooldown_hours": _env_float("ALERT_COOLDOWN_HOURS", 6.0),
        "regime_breadth_pct": _env_float("REGIME_BREADTH_PCT", 55.0),
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


def _genuine_touches(cluster, bars, tol, kind):
    """Count touches where price actually left the zone (by `tol`) and came back
    between them, so a run of pivots from a single visit counts as one genuine
    touch. `kind` is 'high' (resistance) or 'low' (support)."""
    members = sorted(cluster, key=lambda p: p[0])  # by bar index
    if not members:
        return 0
    level = _cluster_mean(cluster)
    count = 1
    last_idx = members[0][0]
    for idx, _ in members[1:]:
        between = bars[last_idx + 1:idx]
        if kind == "high":
            left = any(b[1] < level - tol for b in between)   # dipped off resistance
        else:
            left = any(b[0] > level + tol for b in between)   # popped off support
        if left:
            count += 1
            last_idx = idx
    return count


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
    res_clusters = [c for c in highs if _genuine_touches(c, bars, tol, "high") >= min_touches]
    sup_clusters = [c for c in lows if _genuine_touches(c, bars, tol, "low") >= min_touches]
    if not res_clusters or not sup_clusters:
        return None

    resistance = max(res_clusters, key=lambda c: (_genuine_touches(c, bars, tol, "high"),
                                                  max(i for i, _ in c)))
    res_level = _cluster_mean(resistance)

    # Support must sit below resistance to form a real range.
    below = [c for c in sup_clusters if _cluster_mean(c) < res_level]
    if not below:
        return None
    support = max(below, key=lambda c: (_genuine_touches(c, bars, tol, "low"),
                                        max(i for i, _ in c)))
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
        # Actual wick extremes of the swings that formed each level, so the stop
        # can sit just beyond proven price rather than a generic ATR distance.
        "support_low": min(p for _, p in support),
        "resistance_high": max(p for _, p in resistance),
        "support_touches": _genuine_touches(support, bars, tol, "low"),
        "resistance_touches": _genuine_touches(resistance, bars, tol, "high"),
        "trend": classify_trend(bars, atr, config),
        "support_broken": support_broken,
        "resistance_broken": resistance_broken,
        "detected_at": int(time.time()),
        "alerted_support": False,
        "alerted_resistance": False,
        "alerted_support_ts": 0,
        "alerted_resistance_ts": 0,
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
# Market regime (measured, not gated -- driven by top-100 breadth)
# --------------------------------------------------------------------------- #
def classify_btc_trend(client):
    """BTC daily trend (price vs a 20d SMA and the SMA's slope), recorded as
    context alongside the breadth-based regime. Returns 'up'/'down'/'flat' (or
    'unknown' if no data)."""
    bars = client.get_klines("BTCUSDT", "1d", 30)
    if not bars or len(bars) < 25:
        return "unknown"
    closes = [b[2] for b in bars]
    n = 20
    sma_now = sum(closes[-n:]) / n
    sma_prev = sum(closes[-n - 5:-5]) / n
    last = closes[-1]
    if last > sma_now and sma_now > sma_prev:
        return "up"
    if last < sma_now and sma_now < sma_prev:
        return "down"
    return "flat"


def update_regime(state, client, config):
    """Market regime from universe breadth alone: long if >= REGIME_BREADTH_PCT
    of the top-100 are trending up, short if that many are down, else neutral.
    Measured only -- it tags alerts but doesn't gate them, so we can validate it
    across regimes before acting. BTC's daily trend is recorded as context only
    (not part of the call) so we can compare approaches later."""
    breadth = state.get("breadth", {})
    total = sum(breadth.values())
    up_pct = 100.0 * breadth.get("up", 0) / total if total else 0.0
    down_pct = 100.0 * breadth.get("down", 0) / total if total else 0.0
    thr = config["regime_breadth_pct"]
    if up_pct >= thr:
        regime = "long"
    elif down_pct >= thr:
        regime = "short"
    else:
        regime = "neutral"
    btc = classify_btc_trend(client)  # context only; not used in the regime call
    state["regime"] = regime
    state["breadth_up_pct"] = up_pct
    state["btc_trend"] = btc
    logger.info("Market regime: %s (breadth %.0f%% up / %.0f%% down of %d pairs; "
                "BTC daily %s [context])", regime.upper(), up_pct, down_pct, total, btc)
    for rng in state.get("ranges", {}).values():
        rng["regime"] = regime
    return regime


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
    breadth = {"up": 0, "down": 0, "flat": 0}

    for symbol in pairs:
        bars = client.get_klines(symbol, "4h", config["range_candles"])
        if not bars or len(bars) < config["atr_period"] + 1:
            ranges.pop(symbol, None)
            continue
        # Tally each pair's trend for market breadth (crypto only -- skip forced
        # non-crypto includes like PAXG/gold, which don't reflect crypto regime).
        if symbol not in config["always_include"]:
            atr_b = compute_atr(bars, config["atr_period"])
            breadth[classify_trend(bars, atr_b, config) if atr_b else "flat"] += 1
        new_range = detect_range(symbol, bars, config)
        if new_range is None:
            ranges.pop(symbol, None)
            continue

        # Preserve de-dup flags + last-alert times when the zones barely moved,
        # so a pair parked at a boundary doesn't re-alert every 4h and the
        # per-level cooldown survives re-detection.
        old = ranges.get(symbol)
        if old:
            tol = 0.1 * new_range["atr"]
            if (abs(new_range["resistance"] - old["resistance"]) < tol and
                    abs(new_range["support"] - old["support"]) < tol):
                new_range["alerted_support"] = old.get("alerted_support", False)
                new_range["alerted_resistance"] = old.get("alerted_resistance", False)
                new_range["alerted_support_ts"] = old.get("alerted_support_ts", 0)
                new_range["alerted_resistance_ts"] = old.get("alerted_resistance_ts", 0)
        ranges[symbol] = new_range
        detected += 1

    for stale in [s for s in ranges if s not in pairs]:
        ranges.pop(stale, None)

    state["breadth"] = breadth
    logger.info("Range detection complete: %d valid range(s)", detected)
    update_regime(state, client, config)
    return detected


def _order_levels(side, rng, config):
    """Return (direction, entry, stop, target, rr) for a buy or sell setup.

    Entry sits just *inside* the level by ENTRY_OFFSET_ATR (so it fills as price
    enters the zone rather than needing an exact tag of the level). Stop sits just
    beyond the wick extreme of the swings that formed the level (a tight,
    structure-based stop) with a small STOP_BUFFER_ATR cushion; falls back to
    STOP_ATR_MULT*ATR if wick data isn't present (older saved ranges)."""
    atr = rng["atr"]
    sup = rng["support"]
    res = rng["resistance"]
    buf = config["stop_buffer_atr"] * atr
    offset = config["entry_offset_atr"] * atr
    if side == "support":
        direction = "BUY AT SUPPORT"
        wick = rng.get("support_low")
        stop = (wick - buf) if wick is not None else sup - config["stop_atr_mult"] * atr
        entry, target = sup + offset, res
        rr = (target - entry) / (entry - stop) if entry != stop else 0.0
    else:
        direction = "SELL AT RESISTANCE"
        wick = rng.get("resistance_high")
        stop = (wick + buf) if wick is not None else res + config["stop_atr_mult"] * atr
        entry, target = res - offset, sup
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


def _box_broken_label(rng):
    """'none' / 'support' / 'resistance' / 'both' -- which boundaries had a recent
    decisive close beyond them when the alert fired."""
    sb, rb = rng.get("support_broken"), rng.get("resistance_broken")
    if sb and rb:
        return "both"
    if sb:
        return "support"
    if rb:
        return "resistance"
    return "none"


def _record_alert_csv(symbol, side, rng, config, ticks):
    direction, entry, stop, target, rr = _order_levels(side, rng, config)

    def disp(price):
        return fmt_price(price, symbol, ticks)

    row = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "pair": symbol, "direction": direction,
        "entry": disp(entry), "stop": disp(stop), "target": disp(target),
        "rr": f"{rr:.2f}", "zone_high": disp(rng["resistance"]),
        "zone_low": disp(rng["support"]), "atr": disp(rng["atr"]),
        "regime": rng.get("regime", ""),
        # Was the box already compromised when this fired? (a side that closed
        # decisively beyond its boundary). Recorded only -- lets --experiment
        # later compare clean boxes vs. ones price had broken out of.
        "box_broken": _box_broken_label(rng),
    }
    try:
        _append_alert_row(row)
    except OSError as exc:
        logger.warning("Could not write alert CSV: %s", exc)


def _append_alert_row(row):
    """Append a row, migrating the CSV header in place if a column was added
    (old rows get blanks for the new field)."""
    old_header, existing = None, []
    if os.path.isfile(ALERTS_CSV):
        with open(ALERTS_CSV, "r", newline="", encoding="utf-8") as fh:
            data = list(csv.reader(fh))
        if data:
            old_header, existing = data[0], data[1:]
    if old_header == CSV_HEADER:
        with open(ALERTS_CSV, "a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=CSV_HEADER).writerow(row)
        return
    with open(ALERTS_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADER)
        for old in existing:
            mapped = dict(zip(old_header, old)) if old_header else {}
            writer.writerow([mapped.get(col, "") for col in CSV_HEADER])
        csv.DictWriter(fh, fieldnames=CSV_HEADER).writerow(row)


def _emit_alert(symbol, side, rng, current, config, ticks, notifier, dry_run):
    message = build_alert_message(symbol, side, rng, current, config, ticks)
    direction = "BUY AT SUPPORT" if side == "support" else "SELL AT RESISTANCE"
    logger.info("ALERT %s %s (close=%s)", symbol, direction, fmt_price(current, symbol, ticks))
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
    """Compare the latest trigger-timeframe close to each zone and alert near
    boundaries (runs every ALERT_INTERVAL_MIN minutes)."""
    ranges = state.get("ranges", {})
    ticks = state.get("ticks", {})
    if not ranges:
        logger.info("Alert check: no active ranges")
        return 0

    checked = 0
    fired = 0
    tf = config["alert_timeframe"]
    now_ts = time.time()
    cooldown = config["alert_cooldown_hours"] * 3600
    for symbol, rng in ranges.items():
        bars = client.get_klines(symbol, tf, 2)
        if not bars:
            continue
        close = bars[-1][2]  # latest closed trigger-timeframe close
        checked += 1
        proximity = config["alert_atr_mult"] * rng["atr"]
        sup = rng["support"]
        res = rng["resistance"]
        midline = (sup + res) / 2.0
        side = evaluate_alert(close, rng, config)

        # Bounce off support -> buy. Fire only if armed AND past the per-level
        # cooldown. Re-arm only after price has reclaimed the range midpoint
        # since the alert -- a true round-trip across the box, not a small
        # chop-out that would re-fire the same level all day.
        if side == "support":
            cooled = now_ts - rng.get("alerted_support_ts", 0) >= cooldown
            if not rng.get("alerted_support") and cooled:
                _emit_alert(symbol, "support", rng, close, config, ticks, notifier, dry_run)
                rng["alerted_support"] = True
                rng["alerted_support_ts"] = now_ts
                fired += 1
        elif close >= midline:
            rng["alerted_support"] = False

        # Rejection at resistance -> sell. Same midline re-arm rule.
        if side == "resistance":
            cooled = now_ts - rng.get("alerted_resistance_ts", 0) >= cooldown
            if not rng.get("alerted_resistance") and cooled:
                _emit_alert(symbol, "resistance", rng, close, config, ticks, notifier, dry_run)
                rng["alerted_resistance"] = True
                rng["alerted_resistance_ts"] = now_ts
                fired += 1
        elif close <= midline:
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
    tf = config["alert_timeframe"]
    if state.get("regime"):
        print(f"\nMarket regime: {state['regime'].upper()} "
              f"(breadth {state.get('breadth_up_pct', 0):.0f}% up, "
              f"BTC daily {state.get('btc_trend', '?')})")
    print(f"\n--- Live position vs range (latest {tf} close) ---")
    print("-" * 92)
    print(f"{'PAIR':<14}{'CLOSE':>14}{'TREND':>7}"
          f"{'TO SUP(ATR)':>13}{'TO RES(ATR)':>13}{'STATUS':>26}")
    print("-" * 92)
    for symbol in sorted(ranges):
        rng = ranges[symbol]
        bars = client.get_klines(symbol, tf, 2)
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


def _fetch_klines_from(client, symbol, interval, start_ms, limit):
    """Raw klines from a start time (forward review only). Returns [] on failure."""
    data = client._get("/fapi/v1/klines",
                       params={"symbol": symbol, "interval": interval,
                               "startTime": start_ms, "limit": limit})
    return data if isinstance(data, list) else []


def review_alerts(client, config):
    """Forward-test: resolve each logged alert against what price did next.

    For every row in crypto_range_alerts.csv, fetch 5m bars from the alert time
    and check (1) did the limit entry fill, and (2) did it then hit target or
    stop first. Prints per-alert outcomes and a performance summary. Read-only.
    """
    if not os.path.isfile(ALERTS_CSV):
        print(f"No alert log yet ({ALERTS_CSV}).")
        print("Forward results show up here once the live bot has fired alerts.")
        return
    try:
        with open(ALERTS_CSV, "r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as exc:
        print(f"Could not read {ALERTS_CSV}: {exc}")
        return
    if not rows:
        print("Alert log is empty - nothing to review yet.")
        return

    rows.sort(key=lambda r: r.get("timestamp", ""))
    interval, bar_min = "5m", 5
    fill_bars = max(1, int(config["review_fill_hours"] * 60 / bar_min))
    hold_bars = max(1, int(config["review_hold_hours"] * 60 / bar_min))
    cooldown_s = config["alert_cooldown_hours"] * 3600

    print(f"Reviewing {len(rows)} alert(s) | fill window {config['review_fill_hours']:.0f}h, "
          f"max hold {config['review_hold_hours']:.0f}h, dedup {config['alert_cooldown_hours']:.0f}h, "
          f"{interval} resolution\n")
    print("-" * 88)
    print(f"{'TIME (UTC)':<21}{'PAIR':<13}{'DIR':<5}{'ENTRY':>13}{'OUTCOME':>12}{'R':>8}")
    print("-" * 88)

    n_fill = n_nofill = n_win = n_loss = n_open = n_nodata = n_dup = 0
    total_r = 0.0
    last_kept = {}
    trades = []  # (is_buy, entry, target, atr, segment) for the stop-distance sweep
    for row in rows:
        try:
            ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            pair, direction = row["pair"], row["direction"]
            entry, stop, target = float(row["entry"]), float(row["stop"]), float(row["target"])
            rr, atr = float(row["rr"]), float(row["atr"])
        except (KeyError, ValueError):
            continue
        is_buy = "BUY" in direction
        # Collapse repeats: same pair+side within the cooldown window = one setup.
        key = (pair, direction)
        prev = last_kept.get(key)
        if prev is not None and ts.timestamp() - prev < cooldown_s:
            n_dup += 1
            print(f"{row['timestamp']:<21}{pair:<13}{('BUY' if is_buy else 'SELL'):<5}"
                  f"{entry:>13.6g}{'dup':>12}{'-':>8}")
            continue
        last_kept[key] = ts.timestamp()
        bars = _fetch_klines_from(client, pair, interval,
                                  int(ts.timestamp() * 1000), fill_bars + hold_bars + 5)
        outcome, r = "?", 0.0
        if not bars:
            outcome, n_nodata = "no-data", n_nodata + 1
        else:
            fill_i = None
            for i, b in enumerate(bars[:fill_bars]):
                hi, lo = float(b[2]), float(b[3])
                if (is_buy and lo <= entry) or (not is_buy and hi >= entry):
                    fill_i = i
                    break
            if fill_i is None:
                outcome, n_nofill = "no-fill", n_nofill + 1
            else:
                n_fill += 1
                seg = bars[fill_i:fill_i + hold_bars]
                if atr > 0:
                    trades.append((is_buy, entry, target, atr, seg))
                outcome = "open"
                for b in seg:
                    hi, lo = float(b[2]), float(b[3])
                    hit_stop = (lo <= stop) if is_buy else (hi >= stop)
                    hit_tgt = (hi >= target) if is_buy else (lo <= target)
                    if hit_stop:  # conservative: if both touch in one 5m bar, stop wins
                        outcome, r, n_loss = "loss", -1.0, n_loss + 1
                        break
                    if hit_tgt:
                        outcome, r, n_win = "win", rr, n_win + 1
                        break
                if outcome == "open":
                    n_open += 1
                else:
                    total_r += r
        rcell = f"{r:+.2f}" if outcome in ("win", "loss") else "-"
        print(f"{row['timestamp']:<21}{pair:<13}{('BUY' if is_buy else 'SELL'):<5}"
              f"{entry:>13.6g}{outcome:>12}{rcell:>8}")

    resolved = n_win + n_loss
    print("-" * 88)
    print(f"\nAlerts: {len(rows)} | distinct setups: {len(rows) - n_dup} "
          f"(deduped {n_dup} repeat(s)) | filled: {n_fill} | no-fill: {n_nofill} | "
          f"no-data: {n_nodata}")
    if resolved:
        print(f"Resolved: {resolved} (wins {n_win}, losses {n_loss}) | still open: {n_open}")
        print(f"Win rate: {100.0 * n_win / resolved:.0f}%  |  total: {total_r:+.2f}R  |  "
              f"expectancy: {total_r / resolved:+.2f}R per trade")
    else:
        print(f"Still open/unresolved: {n_open} - not enough forward data to score yet.")

    _print_stop_sweep(trades)


def _print_stop_sweep(trades):
    """Re-resolve the same filled trades at different stop distances (measured
    from entry, in ATR) to show whether a wider stop would help and where
    expectancy peaks. Reward keeps the logged target; risk = stop distance."""
    if not trades:
        return
    print("\n--- Stop-distance sweep (same trades, stop measured from entry) ---")
    print("How far each trade went against entry decides which stop survives.\n")
    print(f"{'STOP (xATR)':<12}{'RESOLVED':>10}{'WINS':>7}{'WIN%':>7}{'EXPECTANCY':>13}")
    print("-" * 49)
    for d in (0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0):
        wins = losses = 0
        tr = 0.0
        for is_buy, entry, target, atr, seg in trades:
            stop = entry - d * atr if is_buy else entry + d * atr
            risk = d * atr
            res = None
            for b in seg:
                hi, lo = float(b[2]), float(b[3])
                hit_stop = (lo <= stop) if is_buy else (hi >= stop)
                hit_tgt = (hi >= target) if is_buy else (lo <= target)
                if hit_stop:
                    res = "loss"
                    break
                if hit_tgt:
                    res = "win"
                    break
            if res == "win":
                wins += 1
                tr += abs(target - entry) / risk if risk else 0.0
            elif res == "loss":
                losses += 1
                tr -= 1.0
        n = wins + losses
        if n:
            print(f"{d:<12.2f}{n:>10}{wins:>7}{100.0 * wins / n:>6.0f}%{tr / n:>+12.2f}R")
    print("\n(stop from entry ~= ENTRY_OFFSET_ATR + STOP_BUFFER_ATR + wick distance; "
          "use the row with the best expectancy as a guide.)")


def _resolve_seg(seg, is_buy, stop, target):
    """Which is hit first over a 5m segment. Conservative: stop wins ties."""
    for b in seg:
        hi, lo = float(b[2]), float(b[3])
        hit_stop = (lo <= stop) if is_buy else (hi >= stop)
        hit_tgt = (hi >= target) if is_buy else (lo <= target)
        if hit_stop:
            return "loss"
        if hit_tgt:
            return "win"
    return "open"


def _collect_trades(client, config):
    """Filled, deduped alerts with their forward 5m price path, for exit-rule
    experiments. Each: dict(pair, is_buy, entry, target, atr, seg)."""
    if not os.path.isfile(ALERTS_CSV):
        return []
    try:
        with open(ALERTS_CSV, "r", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return []
    rows.sort(key=lambda r: r.get("timestamp", ""))
    bar_min = 5
    fill_bars = max(1, int(config["review_fill_hours"] * 60 / bar_min))
    hold_bars = max(1, int(config["review_hold_hours"] * 60 / bar_min))
    cooldown_s = config["alert_cooldown_hours"] * 3600
    last, out = {}, []
    for row in rows:
        try:
            ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            pair, direction = row["pair"], row["direction"]
            entry, target, atr = float(row["entry"]), float(row["target"]), float(row["atr"])
        except (KeyError, ValueError):
            continue
        key = (pair, direction)
        if key in last and ts.timestamp() - last[key] < cooldown_s:
            continue
        last[key] = ts.timestamp()
        if atr <= 0:
            continue
        is_buy = "BUY" in direction
        bars = _fetch_klines_from(client, pair, "5m",
                                  int(ts.timestamp() * 1000), fill_bars + hold_bars + 5)
        if not bars:
            continue
        fill_i = None
        for i, b in enumerate(bars[:fill_bars]):
            hi, lo = float(b[2]), float(b[3])
            if (is_buy and lo <= entry) or (not is_buy and hi >= entry):
                fill_i = i
                break
        if fill_i is None:
            continue
        out.append(dict(pair=pair, is_buy=is_buy, entry=entry, target=target,
                        atr=atr, regime=row.get("regime", "") or "?",
                        seg=bars[fill_i:fill_i + hold_bars]))
    return out


def run_experiment(client, config):
    """Sweep stop x target exit rules over the real fired alerts to look for a
    combo that lifts win rate / expectancy. Forward data only (optimises exits on
    actual signals, not a backtest of entries)."""
    trades = _collect_trades(client, config)
    if not trades:
        print("No filled trades to experiment on yet - let the bot run longer.")
        return
    nbuy = sum(1 for t in trades if t["is_buy"])
    print(f"Experimenting on {len(trades)} filled, deduped trades "
          f"({nbuy} buy / {len(trades) - nbuy} sell).")
    print("WARNING: tiny sample -> everything below is a hypothesis, not proof.\n")

    stops = [0.1, 0.2, 0.3, 0.5]
    tgts = [1.0, 1.5, 2.0, 3.0, "full"]

    def cell(S, T):
        w = l = 0
        tr = 0.0
        for t in trades:
            stop = t["entry"] - S * t["atr"] if t["is_buy"] else t["entry"] + S * t["atr"]
            risk = S * t["atr"]
            tgt = (t["target"] if T == "full"
                   else (t["entry"] + T * t["atr"] if t["is_buy"] else t["entry"] - T * t["atr"]))
            res = _resolve_seg(t["seg"], t["is_buy"], stop, tgt)
            if res == "win":
                w += 1
                tr += abs(tgt - t["entry"]) / risk if risk else 0.0
            elif res == "loss":
                l += 1
                tr -= 1.0
        n = w + l
        return n, (100.0 * w / n if n else 0.0), (tr / n if n else 0.0)

    grid = {(S, T): cell(S, T) for S in stops for T in tgts}
    hdr = "  stop\\tgt" + "".join(f"{(str(x) + 'x' if x != 'full' else 'full'):>8}" for x in tgts)

    print("WIN RATE %  (rows = stop xATR, cols = target):")
    print(hdr)
    for S in stops:
        print(f"  {S:>6.1f} " + "".join(f"{grid[(S, T)][1]:>7.0f}%" for T in tgts))

    print("\nEXPECTANCY R/trade:")
    print(hdr)
    for S in stops:
        print(f"  {S:>6.1f} " + "".join(f"{grid[(S, T)][2]:>+8.2f}" for T in tgts))

    (bS, bT), (bn, bwr, be) = max(grid.items(), key=lambda kv: kv[1][2])
    print(f"\nBest expectancy: {be:+.2f}R at stop {bS}xATR + target "
          f"{bT if bT == 'full' else str(bT) + 'xATR'} (win {bwr:.0f}%, n={bn}).")
    print("With this few trades the 'best' cell is likely overfit - treat as a "
          "direction to confirm as data grows.")

    # Subgroup breakdown (which directions / coins carry vs bleed) at one
    # representative exit, so we can see if a filter would help.
    ref_stop, ref_tgt = bS, bT

    def subset(sub):
        w = l = 0
        tr = 0.0
        for t in sub:
            stop = t["entry"] - ref_stop * t["atr"] if t["is_buy"] else t["entry"] + ref_stop * t["atr"]
            risk = ref_stop * t["atr"]
            tgt = (t["target"] if ref_tgt == "full"
                   else (t["entry"] + ref_tgt * t["atr"] if t["is_buy"] else t["entry"] - ref_tgt * t["atr"]))
            res = _resolve_seg(t["seg"], t["is_buy"], stop, tgt)
            if res == "win":
                w += 1
                tr += abs(tgt - t["entry"]) / risk if risk else 0.0
            elif res == "loss":
                l += 1
                tr -= 1.0
        n = w + l
        return n, w, tr

    reflabel = f"stop {ref_stop}xATR + target {ref_tgt if ref_tgt == 'full' else str(ref_tgt) + 'xATR'}"
    print(f"\n--- Breakdown at the best exit ({reflabel}) ---")
    print("By direction:")
    for label, sub in (("BUY", [t for t in trades if t["is_buy"]]),
                       ("SELL", [t for t in trades if not t["is_buy"]])):
        n, w, tr = subset(sub)
        wr = 100.0 * w / n if n else 0.0
        exp = tr / n if n else 0.0
        print(f"  {label:<5} n={n:<3} win {wr:>3.0f}%  total {tr:>+7.2f}R  exp {exp:>+.2f}R")

    regimes = sorted({t.get("regime", "?") for t in trades})
    if regimes != ["?"]:
        print("\nBy regime (needs data across regimes to be meaningful):")
        for reg in regimes:
            sub = [t for t in trades if t.get("regime", "?") == reg]
            n, w, tr = subset(sub)
            wr = 100.0 * w / n if n else 0.0
            exp = tr / n if n else 0.0
            print(f"  {reg:<8} n={n:<3} win {wr:>3.0f}%  total {tr:>+7.2f}R  exp {exp:>+.2f}R")

    by_coin = {}
    for t in trades:
        by_coin.setdefault(t["pair"], []).append(t)
    rows = []
    for pair, sub in by_coin.items():
        n, w, tr = subset(sub)
        if n:
            rows.append((tr, pair, n, w))
    rows.sort()  # worst total R first
    print("\nBy coin (worst total R first):")
    for tr, pair, n, w in rows:
        print(f"  {pair:<14} n={n:<3} win {100.0 * w / n:>3.0f}%  total {tr:>+7.2f}R")


def run_loop(state, client, config, notifier):
    """The continuous scheduler: detection (4h), alerts, refresh (24h)."""
    pairs = refresh_pairs(state, client, config)
    alert_interval = max(60, config["alert_interval_min"] * 60)

    logger.info("=" * 60)
    logger.info("Crypto Range Monitor started (USDT-M futures: %s%s)",
                config["binance_base_url"],
                "; via proxy" if config["proxy"] else "")
    logger.info("Env vars loaded: TELEGRAM_BOT_TOKEN (set), TELEGRAM_CHAT_ID=%s",
                config["telegram_chat_id"])
    logger.info("Config: top %d pairs by 24h volume, always include [%s], "
                "ATR(%d) on %d x 4H bars, zone=%.2fxATR, alert=%.2fxATR, "
                "stop=wick+%.2fxATR, max width=%.2fxATR/%.1f%%, min touches=%d, "
                "trend filter=%s, break filter=%s",
                config["top_n"], ", ".join(config["always_include"]) or "none",
                config["atr_period"], config["range_candles"], config["zone_atr_mult"],
                config["alert_atr_mult"], config["stop_buffer_atr"],
                config["max_range_atr_mult"], config["max_range_pct"],
                config["min_touches"], "on" if config["trend_filter"] else "off",
                "on" if config["break_filter"] else "off")
    logger.info("Monitoring %d perpetual(s); PAXGUSDT included: %s",
                len(pairs), "yes" if "PAXGUSDT" in pairs else "no")
    logger.info("Schedule: range detection every 4h (UTC-aligned), "
                "alert check every %dm on %s closes, pair refresh every 24h",
                config["alert_interval_min"], config["alert_timeframe"])
    logger.info("=" * 60)

    try:
        run_range_detection(state, client, config)
        run_alert_check(state, client, config, notifier)
        save_state(state)
    except Exception as exc:  # noqa: BLE001 - never let startup work crash the loop
        logger.exception("Error during initial cycle: %s", exc)

    now = time.time()
    next_detect = next_aligned(now, RANGE_INTERVAL) + DETECT_DELAY
    next_alert = next_aligned(now, alert_interval)
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
                next_alert = next_aligned(time.time(), alert_interval)

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
    parser.add_argument("--review", action="store_true",
                        help="Forward-test logged alerts (crypto_range_alerts.csv): "
                             "resolve each against later price and print stats.")
    parser.add_argument("--experiment", action="store_true",
                        help="Sweep stop x target exit rules over the logged "
                             "alerts to look for higher win rate / expectancy.")
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

    if args.review:
        review_alerts(client, config)
        return

    if args.experiment:
        run_experiment(client, config)
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
