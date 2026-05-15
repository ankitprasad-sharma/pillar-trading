"""
order_manager.py — Live order execution via Upstox API v3.

LIVE_TRADING controls whether real orders are sent to the exchange.
It is NEVER changed programmatically — only a human editing this file may do so.
Paper trades always run in parallel (see paper_trader.py) for comparison.
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

# ── SAFETY GATE ────────────────────────────────────────────────────────────────
# Set to True ONLY after completing the Going Live Checklist in CLAUDE.md.
# This flag is NEVER toggled by code. A human must edit this line.
LIVE_TRADING = False
# ──────────────────────────────────────────────────────────────────────────────

_ORDER_URL  = "https://api.upstox.com/v3/order/place"
_STATUS_URL = "https://api.upstox.com/v3/order/details"


def _headers() -> dict:
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def _extract_error(data: dict, http_status: int) -> str:
    return (
        data.get("message")
        or (data.get("errors") or [{}])[0].get("message", "")
        or f"HTTP {http_status}"
    )


def place_order(signal: dict, option_data: dict, quantity: int = 65) -> dict:
    """
    Place a BUY MARKET order for the given option contract.

    Returns:
        {"order_id": str, "status": "PLACED", "message": str}  on success
        {"order_id": None, "status": "FAILED", "message": str} on failure
    """
    inst_key = option_data.get("instrument_key")
    if not inst_key:
        return {"order_id": None, "status": "FAILED",
                "message": "instrument_key missing from option_data"}

    payload = {
        "quantity"          : quantity,
        "product"           : "D",
        "validity"          : "DAY",
        "price"             : 0,
        "instrument_token"  : inst_key,
        "order_type"        : "MARKET",
        "transaction_type"  : "BUY",
        "disclosed_quantity": 0,
        "trigger_price"     : 0,
        "is_amo"            : False,
    }
    try:
        r    = requests.post(_ORDER_URL, json=payload, headers=_headers(), timeout=10)
        data = r.json()
        if r.status_code == 200 and data.get("status") == "success":
            order_id = data["data"]["order_id"]
            return {"order_id": order_id, "status": "PLACED",
                    "message": f"BUY order accepted: {order_id}"}
        return {"order_id": None, "status": "FAILED",
                "message": _extract_error(data, r.status_code)}
    except Exception as e:
        return {"order_id": None, "status": "FAILED", "message": str(e)[:80]}


def close_order(position: dict, current_premium: float,
                quantity: int = 65) -> dict:
    """
    Place a SELL MARKET order to close an existing option position.

    Returns:
        {"order_id": str, "status": "PLACED", "message": str}  on success
        {"order_id": None, "status": "FAILED", "message": str} on failure
    """
    inst_key = position.get("instrument_key")
    if not inst_key:
        return {"order_id": None, "status": "FAILED",
                "message": "instrument_key missing from position"}

    payload = {
        "quantity"          : quantity,
        "product"           : "D",
        "validity"          : "DAY",
        "price"             : 0,
        "instrument_token"  : inst_key,
        "order_type"        : "MARKET",
        "transaction_type"  : "SELL",
        "disclosed_quantity": 0,
        "trigger_price"     : 0,
        "is_amo"            : False,
    }
    try:
        r    = requests.post(_ORDER_URL, json=payload, headers=_headers(), timeout=10)
        data = r.json()
        if r.status_code == 200 and data.get("status") == "success":
            order_id = data["data"]["order_id"]
            return {"order_id": order_id, "status": "PLACED",
                    "message": f"SELL order accepted: {order_id}"}
        return {"order_id": None, "status": "FAILED",
                "message": _extract_error(data, r.status_code)}
    except Exception as e:
        return {"order_id": None, "status": "FAILED", "message": str(e)[:80]}


def get_order_status(order_id: str) -> dict:
    """
    Fetch the current status of a placed order.

    Returns the Upstox order data dict on success, or:
        {"status": "ERROR", "message": str} on failure.
    """
    try:
        r    = requests.get(_STATUS_URL, params={"order_id": order_id},
                            headers=_headers(), timeout=10)
        data = r.json()
        if r.status_code == 200 and data.get("status") == "success":
            return data["data"]
        return {"status": "ERROR",
                "message": _extract_error(data, r.status_code)}
    except Exception as e:
        return {"status": "ERROR", "message": str(e)[:80]}
