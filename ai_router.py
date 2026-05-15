import os
import json
import time
import logging
from google import genai
from dotenv import load_dotenv

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

gemini_calls = []
GEMINI_RATE_LIMIT = 14

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_VALID_ACTIONS    = {"BUY_CALL", "BUY_PUT", "STAY_OUT"}
_VALID_CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}


def is_gemini_available() -> bool:
    now = time.time()
    global gemini_calls
    gemini_calls = [t for t in gemini_calls if now - t < 60]
    return len(gemini_calls) < GEMINI_RATE_LIMIT


def _is_trading_allowed(cur_time: str) -> tuple[bool, str]:
    if not cur_time or cur_time == "N/A":
        return True, "OK"
    if cur_time < "09:20":
        return False, "Pre-market — wait for 9:20"
    if "09:20" <= cur_time <= "09:30":
        return False, "Opening volatility window"
    if cur_time > "15:00":
        return False, "Post 3 PM — theta risk"
    return True, "OK"


def validate_signal(signal: dict, ltp: float = 0) -> dict:
    """Validate Gemini output. Returns STAY_OUT on unrecoverable fields."""
    action = signal.get("action", "")
    if action not in _VALID_ACTIONS:
        logger.warning(f"Invalid action '{action}' — falling back to STAY_OUT")
        return {"action": "STAY_OUT",
                "reason": f"Model returned unrecognised action: {action}",
                "confidence": "LOW", "source": signal.get("source", "gemini")}

    if signal.get("confidence") not in _VALID_CONFIDENCE:
        logger.warning(f"Invalid confidence '{signal.get('confidence')}' — set to LOW")
        signal["confidence"] = "LOW"

    if action != "STAY_OUT":
        strike = signal.get("strike")
        if not isinstance(strike, (int, float)) or strike <= 0:
            if ltp:
                signal["strike"] = int(round(ltp / 50) * 50)
                logger.warning(f"Missing/invalid strike — derived {signal['strike']} from LTP")
            else:
                logger.warning("Missing strike and no LTP — falling back to STAY_OUT")
                return {"action": "STAY_OUT", "reason": "Missing strike price",
                        "confidence": "LOW", "source": signal.get("source", "gemini")}
        else:
            signal["strike"] = int(strike)

    if not signal.get("reason"):
        signal["reason"] = "No reason provided"

    return signal


def rule_based_signal(market_data: dict) -> dict:
    ltp        = market_data.get("ltp", 0)
    prev_close = market_data.get("prev_close", 0)
    change_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0
    cur_time   = market_data.get("time", "")
    ce_prem    = market_data.get("option_premium_ce") or 0
    pe_prem    = market_data.get("option_premium_pe") or 0

    allowed, block_reason = _is_trading_allowed(cur_time)
    if not allowed:
        return {"action": "STAY_OUT", "reason": block_reason,
                "confidence": "HIGH", "source": "rule_based"}

    # CE > PE = market pricing in upside; PE > CE = downside
    prem_bullish = (ce_prem > pe_prem) if (ce_prem and pe_prem) else None
    ratio_str    = f" CE/PE={ce_prem:.0f}/{pe_prem:.0f}" if (ce_prem and pe_prem) else ""

    if change_pct > 1.5 and prem_bullish is not False:
        return {"action": "BUY_CALL",
                "reason": f"Up {change_pct:+.2f}%{ratio_str}",
                "confidence": "LOW", "source": "rule_based"}
    if change_pct < -1.5 and prem_bullish is not True:
        return {"action": "BUY_PUT",
                "reason": f"Down {change_pct:+.2f}%{ratio_str}",
                "confidence": "LOW", "source": "rule_based"}

    return {"action": "STAY_OUT", "reason": "No clear trend",
            "confidence": "HIGH", "source": "rule_based"}


_SYSTEM_PROMPT = """You are an expert NSE options trader specialising in Nifty 50 index options.
You analyse market data and generate precise trading signals.

Rules you MUST follow:
1. Only recommend BUY_CALL when there is clear bullish momentum with confirmation.
2. Only recommend BUY_PUT when there is clear bearish momentum with confirmation.
3. Recommend STAY_OUT when trend is unclear, choppy, or risk is high.
4. Consider time of day — avoid signals after 2:30 PM (theta decay risk).
5. Avoid buying options when VIX > 20 (high volatility events).
6. Strike must always be the nearest 50-point multiple to current LTP.
7. Confidence HIGH only when multiple data points align.
8. Confidence MEDIUM when the primary signal is clear but confirmation is weak.
9. Confidence LOW when only one indicator suggests direction.

Always respond in the exact JSON format specified — nothing else."""


def get_trading_signal(market_data: dict) -> dict:
    if is_gemini_available():
        try:
            gemini_calls.append(time.time())
            ltp      = market_data.get("ltp", 0)
            prev     = market_data.get("prev_close", 0)
            chg      = market_data.get("change_pct", 0)
            vix      = market_data.get("vix", "N/A")
            ce_prem  = market_data.get("option_premium_ce", "N/A")
            pe_prem  = market_data.get("option_premium_pe", "N/A")
            strike   = market_data.get("ce_strike", "N/A")
            cur_time = market_data.get("time", "N/A")

            prompt = f"""{_SYSTEM_PROMPT}

Current market data:
- Nifty LTP      : ₹{ltp:,.2f}
- Previous close : ₹{prev:,.2f}
- Change         : {chg:+.2f}%
- India VIX      : {vix}
- ATM strike     : {strike}
- ATM Call (CE) premium : ₹{ce_prem}
- ATM Put  (PE) premium : ₹{pe_prem}
- Time           : {cur_time}

Respond ONLY in this JSON format:
{{
    "action": "BUY_CALL or BUY_PUT or STAY_OUT",
    "strike": <nearest 50-point strike as integer>,
    "reason": "<specific one-line reason citing the data above>",
    "confidence": "HIGH or MEDIUM or LOW",
    "source": "gemini"
}}"""
            r = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            text = r.text.strip().replace("```json", "").replace("```", "").strip()
            signal = json.loads(text)
            signal = validate_signal(signal, market_data.get("ltp", 0))
            logger.info(f"Gemini: {signal['action']} ({signal['confidence']})")
            return signal
        except Exception as e:
            logger.warning(f"Gemini failed: {e} — falling back to rules")

    return rule_based_signal(market_data)
