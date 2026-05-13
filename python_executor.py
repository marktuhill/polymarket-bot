"""
Python CLOB Executor
=====================
Direct Polymarket order placement via py-clob-client.
Drop-in replacement for the Node.js executor subprocess.
"""

import os
import logging
import time
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

# ── .env discovery ────────────────────────────────────────────────────────────
_ENV_CANDIDATES = [
    Path(r"C:\CUsersMarkpolymarket-momentum-bot\.env"),
    Path(r"C:\Users\markt\polymarket-momentum-bot\.env"),
    Path(r"C:\Users\Mark\polymarket-momentum-bot\.env"),
    Path(__file__).parent / ".env",
]
for _p in _ENV_CANDIDATES:
    if _p.exists():
        load_dotenv(_p)
        break

# ── ScraperAPI proxy — patch httpx BEFORE importing py_clob_client ───────────
# Routes Polymarket CLOB API calls through ScraperAPI residential IPs.
# py_clob_client does `from httpx import Client` at import time, so we must
# replace httpx.Client in the httpx module namespace first.
_SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "f4bf5987abe7be581dccf7dc52998dc7")

if _SCRAPERAPI_KEY:
    import httpx as _httpx
    _CLOB_PROXY = f"http://scraperapi:{_SCRAPERAPI_KEY}@proxy-server.scraperapi.com:8001"
    _OrigHttpxClient = _httpx.Client

    class _ProxiedClient(_OrigHttpxClient):
        def __init__(self, *args, **kwargs):
            if "proxy" not in kwargs and "mounts" not in kwargs:
                kwargs["proxy"]  = _CLOB_PROXY
                kwargs["verify"] = False
                kwargs["http2"]  = False   # HTTP/2 conflicts with HTTP CONNECT proxies
            super().__init__(*args, **kwargs)

    _httpx.Client = _ProxiedClient

# ── NOW import py_clob_client — it will pick up the patched httpx.Client ─────
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID  = 137
TICK      = 0.01

# ─────────────────────────────────────────────
# Singleton client
# ─────────────────────────────────────────────

_client: Optional[ClobClient] = None


def _get_client() -> Optional[ClobClient]:
    global _client
    if _client is not None:
        return _client

    pk         = os.getenv("POLYMARKET_PK", "")
    funder     = os.getenv("POLYMARKET_FUNDER", "")
    sig_type   = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
    api_key    = os.getenv("POLYMARKET_API_KEY", "")
    api_secret = os.getenv("POLYMARKET_API_SECRET", "")
    passphrase = os.getenv("POLYMARKET_PASSPHRASE", "")

    if not all([pk, funder, api_key, api_secret, passphrase]):
        logger.error("python_executor: missing .env credentials")
        return None

    try:
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=passphrase,
        )
        _client = ClobClient(
            host=CLOB_HOST,
            chain_id=CHAIN_ID,
            key=pk,
            signature_type=sig_type,
            funder=funder,
            creds=creds,
        )
        proxy_status = "via ScraperAPI" if _SCRAPERAPI_KEY else "direct"
        logger.info(f"python_executor: ClobClient ready [{proxy_status}]")
        return _client
    except Exception as e:
        logger.error(f"python_executor: client init failed: {e}")
        return None


# ─────────────────────────────────────────────
# Public interface
# ─────────────────────────────────────────────

def place_order(side: str, token_id: str, price: float, shares: int,
                expiry_sec: int = 0) -> Optional[str]:
    client = _get_client()
    if not client:
        return None

    rounded_price = max(round(round(price / TICK) * TICK, 2), TICK)

    POLY_MIN_EXPIRY_OFFSET = 61
    expiration  = int(time.time()) + POLY_MIN_EXPIRY_OFFSET + expiry_sec if expiry_sec > 0 else 0
    order_type  = OrderType.GTD if expiry_sec > 0 else OrderType.GTC

    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=rounded_price,
            size=float(shares),
            side=side,
            expiration=expiration,
        )
        t0 = time.time()
        signed = client.create_order(order_args)
        logger.info(f"  python_executor: create_order took {time.time()-t0:.3f}s")
        resp   = client.post_order(signed, order_type)

        if resp and (resp.get("orderID") or resp.get("order_id")):
            order_id = resp.get("orderID") or resp.get("order_id")
            status   = resp.get("status", "unknown")
            order_label = f"GTD+{expiry_sec}s" if expiry_sec > 0 else "GTC"
            logger.info(
                f"  python_executor: {side} {shares}x @ ${rounded_price:.2f} "
                f"[{order_label}] -> {order_id[:16]}... [{status}]"
            )
            return order_id

        if resp and resp.get("status") in ("matched", "filled"):
            fallback_id = f"FILLED_{int(time.time())}"
            logger.info(
                f"  python_executor: {side} {shares}x @ ${rounded_price:.2f} "
                f"-> matched (no order ID in response)"
            )
            return fallback_id

        logger.error(f"  python_executor: unexpected response: {resp}")
        return None

    except Exception as e:
        import traceback as _tb
        logger.error(f"  python_executor: order traceback:\n{_tb.format_exc()}")
        err = str(e)
        if "400" in err and "not enough balance" in err and side == "SELL":
            import re as _re
            balance_match = _re.search(r'balance:\s*(\d+)', err)
            if balance_match:
                actual_units  = int(balance_match.group(1))
                actual_shares = round(actual_units / 1_000_000, 6)
                if actual_shares >= 1.0:
                    logger.warning(
                        f"  python_executor: SELL balance short — retrying with "
                        f"actual balance {actual_shares:.4f} shares"
                    )
                    return place_order(side, token_id, price, actual_shares)
            logger.error(f"  python_executor: 400 balance error — {e}")
        elif "400" in err:
            logger.error(f"  python_executor: 400 bad request — {e}")
        elif "401" in err:
            logger.error(f"  python_executor: 401 unauthorized — API keys may have expired")
        else:
            logger.error(f"  python_executor: order failed — {e}")
        return None


def cancel_order(order_id: str) -> str:
    client = _get_client()
    if not client:
        return "failed"
    try:
        resp = client.cancel(order_id)
        if not resp:
            return "failed"
        cancelled_list = resp.get("canceled", [])
        if cancelled_list:
            logger.info(f"  python_executor: {order_id[:16]}... CANCELLED")
            return "cancelled"
        not_cancelled = resp.get("not_canceled", {})
        if order_id in not_cancelled:
            reason = str(not_cancelled[order_id]).lower()
            if "matched" in reason:
                return "matched"
            return "failed"
        return "failed"
    except Exception as e:
        logger.error(f"python_executor: cancel_order failed — {e}")
        return "failed"


def get_order_status(order_id: str) -> Optional[str]:
    client = _get_client()
    if not client:
        return None
    try:
        resp = client.get_order(order_id)
        if resp:
            raw = resp.get("status")
            return raw.lower() if raw else None
        return None
    except Exception as e:
        logger.error(f"python_executor: get_order failed — {e}")
        return None


def get_usdc_balance() -> Optional[float]:
    client = _get_client()
    if not client:
        return None
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=sig_type,
        )
        resp = client.get_balance_allowance(params)
        if resp:
            raw = resp.get("balance") or resp.get("allowance", "0")
            return int(raw) / 1_000_000
        return None
    except Exception as e:
        logger.debug(f"python_executor: get_usdc_balance — {e}")
        return None


def wait_for_confirmation(order_id: str, live_timeout_sec: float = 45.0,
                          poll_interval: float = 2.0) -> bool:
    if not order_id or order_id.startswith(("DRY_", "FILLED_")):
        return True
    status = get_order_status(order_id)
    if status == "matched":
        return True
    if status in ("canceled", "failed"):
        return False
    live_deadline = time.time() + live_timeout_sec
    while time.time() < live_deadline:
        time.sleep(poll_interval)
        status = get_order_status(order_id)
        if status == "matched":
            return True
        if status in ("canceled", "failed"):
            return False
    cancel_result = cancel_order(order_id)
    return cancel_result == "matched"
