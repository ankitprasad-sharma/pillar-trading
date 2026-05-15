import os
import time
import threading
import requests
from datetime import date
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN")
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json"
}

_contracts_cache  = {}           # cached once per day
_premium_cache    = {}           # cached per strike/type
PREMIUM_TTL       = 15           # seconds between real fetches
_contracts_lock   = threading.Lock()  # prevents duplicate simultaneous fetches


def get_nearest_strike(ltp: float, step: int = 50) -> int:
    return round(ltp / step) * step


def get_all_contracts() -> list:
    if _contracts_cache.get("data"):
        return _contracts_cache["data"]
    with _contracts_lock:
        # Re-check inside lock — another thread may have populated while we waited
        if _contracts_cache.get("data"):
            return _contracts_cache["data"]
        try:
            r = requests.get(
                "https://api.upstox.com/v2/option/contract",
                headers=HEADERS,
                params={"instrument_key": "NSE_INDEX|Nifty 50"},
                timeout=10,
            )
            data = r.json().get("data", [])
        except Exception:
            data = []
        _contracts_cache["data"] = data
        return data


def get_nearest_expiry() -> str:
    contracts = get_all_contracts()
    today     = date.today().strftime("%Y-%m-%d")
    expiries  = sorted(set(c["expiry"] for c in contracts))
    for e in expiries:
        if e >= today:
            return e
    return expiries[0]


def get_option_premium(ltp: float, action: str) -> dict:
    strike      = get_nearest_strike(ltp)
    expiry      = get_nearest_expiry()
    option_type = "CE" if action == "BUY_CALL" else "PE"
    cache_key   = f"{strike}_{option_type}_{expiry}"

    # Return cached value if still fresh
    cached = _premium_cache.get(cache_key)
    if cached and (time.time() - cached["time"]) < PREMIUM_TTL:
        return cached["data"]

    # Find matching contract
    contracts = get_all_contracts()
    filtered  = [c for c in contracts
                 if c["expiry"] == expiry
                 and c["instrument_type"] == option_type]

    if not filtered:
        # Return stale cache if available, else estimate
        if cached:
            return cached["data"]
        est = round(ltp * 0.005)
        return {"premium": est, "strike": strike, "expiry": expiry}

    match          = min(filtered,
                        key=lambda x: abs(x.get("strike_price", 0) - strike))
    actual_strike  = match["strike_price"]
    instrument_key = match["instrument_key"]

    try:
        r = requests.get(
            "https://api.upstox.com/v3/market-quote/ltp",
            headers=HEADERS,
            params={"instrument_key": instrument_key},
            timeout=5
        )
        data = r.json()

        if r.status_code == 200:
            for key, val in data.get("data", {}).items():
                premium = val.get("last_price") or val.get("ltp", 0)
                if premium and premium > 0:
                    result = {
                        "premium":        premium,
                        "strike":         actual_strike,
                        "expiry":         expiry,
                        "instrument_key": instrument_key,
                        "option_type":    option_type,
                        "trading_symbol": match["trading_symbol"],
                        "prev_close":     val.get("cp", 0)
                    }
                    # Cache the fresh result
                    _premium_cache[cache_key] = {
                        "data": result,
                        "time": time.time()
                    }
                    return result

    except Exception:
        pass

    # API failed — return stale cache if available
    if cached:
        return cached["data"]

    # Last resort — estimate
    est = round(ltp * 0.005)
    return {
        "premium":        est,
        "strike":         actual_strike,
        "expiry":         expiry,
        "instrument_key": instrument_key,
        "option_type":    option_type,
        "trading_symbol": match["trading_symbol"]
    }


if __name__ == "__main__":
    test_ltp = 23450.0
    print(f"Nifty LTP: ₹{test_ltp}")
    print(f"Nearest expiry: {get_nearest_expiry()}")
    print()
    ce = get_option_premium(test_ltp, "BUY_CALL")
    print(f"Call: {ce}")
    print()
    pe = get_option_premium(test_ltp, "BUY_PUT")
    print(f"Put: {pe}")
