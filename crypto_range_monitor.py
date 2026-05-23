#!/usr/bin/env python3
"""Crypto range monitor & alert scheduler.

Monitors Binance spot USDT pairs for price action that is *approaching* the edge
of an established trading range, and sends a Telegram alert when price comes
within a configurable multiple of ATR(14) of the range high or low.

What it does
------------
* Pair universe is built dynamically from Binance: every spot ``*USDT`` pair
  whose rolling 24h quote volume exceeds ``MIN_QUOTE_VOLUME`` (default $50M).
  The list is refreshed every 24h.
* Range detection runs every 4h, aligned to Binance 4H bar close. For each pair
  it pulls closed 4H candles, computes ATR(14) from scratch, and records the
  range high/low over a lookback window.
* Alert checks run every 30 minutes. Current price is compared against each
  stored range; if price is within ``PROXIMITY_ATR_MULT * ATR`` of an edge (and
  still inside the range) a Telegram alert fires. Per-edge de-duplication with
  hysteresis prevents repeat spam until price moves away and comes back.

Dependencies: ``requests`` plus the Python standard library only. No pandas,
no numpy.

Environment variables
---------------------
Required:
    TELEGRAM_BOT_TOKEN   Telegram bot token (from @BotFather)
    TELEGRAM_CHAT_ID     Chat / channel id to send alerts to
Optional (defaults in parentheses):
    MIN_QUOTE_VOLUME     (50000000)  min 24h quote volume in USDT to include a pair
    RANGE_LOOKBACK       (20)        number of closed 4H bars defining the range
    ATR_PERIOD           (14)        ATR period
    PROXIMITY_ATR_MULT   (0.5)       alert when within this * ATR of an edge
    BINANCE_BASE_URL     (https://api.binance.com)  primary API host
These may also be placed in a ``.env`` file next to this script.

Running on a Windows VPS (survive reboots)
-----------------------------------------
The script runs its own internal scheduling loop forever, so it only needs to be
(re)launched once per boot. Use Windows Task Scheduler:
    1. Create Task -> Trigger: "At startup"
    2. Action: Start a program
         Program/script:  C:\\path\\to\\python.exe
         Arguments:       C:\\path\\to\\crypto_range_monitor.py
    3. Settings: "If the task fails, restart every 1 minute" (so it self-heals).
Logs are written to crypto_range_monitor.log in the same directory as this file.
"""

import json
import logging
import math
import os
import sys
import time
from logging.handlers import RotatingFileHandler

import requests

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "crypto_range_monitor.log")
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

# Trading pairs whose base asset matches these are skipped (stablecoins).
STABLE_BASES = {
    "USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "UST", "USDD",
    "GUSD", "PAX", "USTC", "EUR", "AEUR", "USD1", "USDe",
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


def get_config():
    return {
        "telegram_token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        "min_quote_volume": _env_float("MIN_QUOTE_VOLUME", 50_000_000),
        "range_lookback": _env_int("RANGE_LOOKBACK", 20),
        "atr_period": _env_int("ATR_PERIOD", 14),
        "proximity_atr_mult": _env_float("PROXIMITY_ATR_MULT", 0.5),
        "binance_base_url": os.environ.get("BINANCE_BASE_URL", "https://api.binance.com").rstrip("/"),
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

    def __init__(self, base_url):
        # Primary host first, then public fallbacks used on persistent failure.
        self.hosts = []
        for host in (base_url, "https://api1.binance.com",
                     "https://api2.binance.com", "https://data-api.binance.vision"):
            if host and host not in self.hosts:
                self.hosts.append(host)
        self.host_index = 0
        self.timeout = 15
        self.max_retries = 6
        self.initial_backoff = 2.0
        self.max_backoff = 64.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "crypto-range-monitor/1.0"})

    def _rotate_host(self):
        self.host_index = (self.host_index + 1) % len(self.hosts)
        logger.info("Switching Binance host to %s", self.hosts[self.host_index])

    def _get(self, path, params=None):
        """GET with backoff on 429/418/5xx and network errors. Returns parsed
        JSON or None if every retry was exhausted."""
        backoff = self.initial_backoff
        for attempt in range(1, self.max_retries + 1):
            host = self.hosts[self.host_index]
            url = host + path
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
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

            # Other 4xx: not retryable.
            logger.error("HTTP %d on %s: %s", resp.status_code, path, resp.text[:200])
            return None

        logger.error("Giving up on %s after %d attempts", path, self.max_retries)
        return None

    def get_24hr(self):
        """All-symbol 24h ticker stats (list of dicts)."""
        return self._get("/api/v3/ticker/24hr")

    def get_all_prices(self):
        """Map of {symbol: float price} for every symbol."""
        data = self._get("/api/v3/ticker/price")
        prices = {}
        if isinstance(data, list):
            for row in data:
                try:
                    prices[row["symbol"]] = float(row["price"])
                except (KeyError, TypeError, ValueError):
                    continue
        return prices

    def get_klines(self, symbol, interval="4h", limit=120):
        """Return a list of (high, low, close) tuples for *closed* candles only.

        Binance includes the in-progress candle as the last element, so we
        request one extra and drop it."""
        data = self._get("/api/v3/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit + 1})
        if not isinstance(data, list) or len(data) < 2:
            return None
        bars = []
        for kline in data[:-1]:  # drop the still-forming candle
            try:
                high = float(kline[2])
                low = float(kline[3])
                close = float(kline[4])
            except (IndexError, TypeError, ValueError):
                continue
            bars.append((high, low, close))
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
                retry_after = 1
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
# Range detection & alerting
# --------------------------------------------------------------------------- #
def detect_range(bars, config):
    """Build a range record from closed bars, or None if not a usable range."""
    atr = compute_atr(bars, config["atr_period"])
    if atr is None or atr <= 0:
        return None

    lookback = config["range_lookback"]
    if len(bars) < lookback:
        return None

    window = bars[-lookback:]
    high = max(b[0] for b in window)
    low = min(b[1] for b in window)
    width = high - low
    threshold = config["proximity_atr_mult"] * atr

    # Range must be wide enough that the high-zone and low-zone don't overlap,
    # otherwise "approaching" is meaningless.
    if width <= 2 * threshold:
        return None

    return {"high": high, "low": low, "atr": atr,
            "detected_at": int(time.time()),
            "alerted_high": False, "alerted_low": False}


def fmt(value):
    """Human-friendly number formatting across very different magnitudes."""
    if value == 0:
        return "0"
    if abs(value) >= 1:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{value:.6g}"


def refresh_pairs(state, client, config):
    """Fetch the dynamic pair universe from Binance and store it on state."""
    data = client.get_24hr()
    if not isinstance(data, list):
        logger.warning("Pair refresh failed; keeping %d existing pair(s)",
                       len(state.get("pairs", [])))
        return state.get("pairs", [])

    min_vol = config["min_quote_volume"]
    candidates = []
    for row in data:
        symbol = row.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        base = symbol[:-4]
        if base in STABLE_BASES:
            continue
        if base.endswith(("UP", "DOWN", "BULL", "BEAR")):  # leveraged tokens
            continue
        try:
            quote_volume = float(row.get("quoteVolume", 0))
        except (TypeError, ValueError):
            continue
        if quote_volume >= min_vol:
            candidates.append((symbol, quote_volume))

    candidates.sort(key=lambda x: x[1], reverse=True)
    pairs = [sym for sym, _ in candidates]
    state["pairs"] = pairs
    state["last_pair_refresh"] = int(time.time())
    logger.info("Pair universe refreshed: %d pairs with 24h quote volume >= $%s",
                len(pairs), f"{min_vol:,.0f}")
    return pairs


def run_range_detection(state, client, config):
    """Recompute ranges for all monitored pairs (every 4h)."""
    if not state.get("pairs"):
        refresh_pairs(state, client, config)

    pairs = state.get("pairs", [])
    logger.info("Range detection starting for %d pair(s)", len(pairs))
    ranges = state.setdefault("ranges", {})
    detected = 0

    for symbol in pairs:
        bars = client.get_klines(symbol, interval="4h",
                                 limit=max(config["range_lookback"], config["atr_period"]) + 5)
        if not bars:
            continue
        new_range = detect_range(bars, config)
        if new_range is None:
            ranges.pop(symbol, None)  # no longer a valid range
            continue

        # Preserve de-dup flags if the range edges barely moved, so a pair that
        # is parked at an edge doesn't re-alert every 4h.
        old = ranges.get(symbol)
        if old:
            tol = 0.1 * new_range["atr"]
            if abs(new_range["high"] - old["high"]) < tol and abs(new_range["low"] - old["low"]) < tol:
                new_range["alerted_high"] = old.get("alerted_high", False)
                new_range["alerted_low"] = old.get("alerted_low", False)
        ranges[symbol] = new_range
        detected += 1

    # Drop ranges for pairs that left the universe.
    for stale in [s for s in ranges if s not in pairs]:
        ranges.pop(stale, None)

    logger.info("Range detection complete: %d active range(s)", detected)


def run_alert_check(state, client, config, notifier):
    """Compare current prices against stored ranges and alert near edges (30m)."""
    ranges = state.get("ranges", {})
    if not ranges:
        logger.info("Alert check: no active ranges yet")
        return

    prices = client.get_all_prices()
    if not prices:
        logger.warning("Alert check skipped: could not fetch prices")
        return

    checked = 0
    fired = 0
    for symbol, rng in ranges.items():
        price = prices.get(symbol)
        if price is None:
            continue
        checked += 1
        high = rng["high"]
        low = rng["low"]
        atr = rng["atr"]
        threshold = config["proximity_atr_mult"] * atr

        dist_high = high - price
        dist_low = price - low

        # Approaching the high from inside the range.
        if 0 <= dist_high <= threshold:
            if not rng.get("alerted_high"):
                _send_edge_alert(notifier, symbol, "HIGH", price, rng, threshold)
                rng["alerted_high"] = True
                fired += 1
        elif dist_high > 1.5 * threshold:  # hysteresis reset
            rng["alerted_high"] = False

        # Approaching the low from inside the range.
        if 0 <= dist_low <= threshold:
            if not rng.get("alerted_low"):
                _send_edge_alert(notifier, symbol, "LOW", price, rng, threshold)
                rng["alerted_low"] = True
                fired += 1
        elif dist_low > 1.5 * threshold:  # hysteresis reset
            rng["alerted_low"] = False

    logger.info("Alert check complete: %d pair(s) checked, %d alert(s) sent", checked, fired)


def _send_edge_alert(notifier, symbol, edge, price, rng, threshold):
    high, low, atr = rng["high"], rng["low"], rng["atr"]
    edge_price = high if edge == "HIGH" else low
    distance = abs(edge_price - price)
    pct = (distance / price * 100) if price else 0.0
    mult = (distance / atr) if atr else 0.0
    text = (
        f"{symbol} approaching range {edge}\n"
        f"Price: {fmt(price)}\n"
        f"Range {edge.lower()}: {fmt(edge_price)}\n"
        f"Range: {fmt(low)} - {fmt(high)}\n"
        f"Distance: {fmt(distance)} ({pct:.2f}% / {mult:.2f}xATR)\n"
        f"ATR(14): {fmt(atr)}"
    )
    logger.info("ALERT %s approaching %s (price=%s edge=%s dist=%.4f xATR=%.2f)",
                symbol, edge, fmt(price), fmt(edge_price), distance, mult)
    notifier.send(text)


# --------------------------------------------------------------------------- #
# State persistence (survive reboots)
# --------------------------------------------------------------------------- #
def load_state():
    if not os.path.isfile(STATE_FILE):
        return {"pairs": [], "ranges": {}, "last_pair_refresh": 0}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        state.setdefault("pairs", [])
        state.setdefault("ranges", {})
        state.setdefault("last_pair_refresh", 0)
        logger.info("Loaded state: %d pair(s), %d active range(s)",
                    len(state["pairs"]), len(state["ranges"]))
        return state
    except (OSError, ValueError) as exc:
        logger.warning("Could not load state (%s); starting fresh", exc)
        return {"pairs": [], "ranges": {}, "last_pair_refresh": 0}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        logger.warning("Could not save state: %s", exc)


# --------------------------------------------------------------------------- #
# Scheduling helpers
# --------------------------------------------------------------------------- #
def next_aligned(now_ts, period, offset=0):
    """Next UTC-aligned timestamp strictly after now_ts for the given period."""
    n = math.floor((now_ts - offset) / period) + 1
    return n * period + offset


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    setup_logging()
    load_env_file(ENV_FILE)
    config = get_config()

    missing = [name for name, key in (("TELEGRAM_BOT_TOKEN", "telegram_token"),
                                      ("TELEGRAM_CHAT_ID", "telegram_chat_id"))
               if not config[key]]
    if missing:
        logger.error("Missing required environment variable(s): %s", ", ".join(missing))
        logger.error("Set them in the environment or in %s and restart.", ENV_FILE)
        sys.exit(1)

    client = BinanceClient(config["binance_base_url"])
    notifier = TelegramNotifier(config["telegram_token"], config["telegram_chat_id"])
    state = load_state()

    # Initial pair fetch so we can print a meaningful startup message.
    pairs = refresh_pairs(state, client, config)

    logger.info("=" * 60)
    logger.info("Crypto Range Monitor started")
    logger.info("Env vars loaded: TELEGRAM_BOT_TOKEN (set), TELEGRAM_CHAT_ID=%s",
                config["telegram_chat_id"])
    logger.info("Config: min_quote_volume=$%s, lookback=%d bars, ATR period=%d, "
                "proximity=%.2fxATR",
                f"{config['min_quote_volume']:,.0f}", config["range_lookback"],
                config["atr_period"], config["proximity_atr_mult"])
    logger.info("Monitoring %d USDT pair(s)", len(pairs))
    logger.info("Schedule: range detection every 4h (UTC-aligned), "
                "alert check every 30m, pair refresh every 24h")
    logger.info("=" * 60)

    # Run an initial cycle immediately so the bot is useful right away.
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


if __name__ == "__main__":
    main()
