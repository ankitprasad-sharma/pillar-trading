import time
import notifier
from ai_router import get_trading_signal
from risk_manager import RiskManager
from paper_trader import open_trade, close_trade, show_summary, load_ledger

# One-shot deduplication so daily-limit alerts are sent only once per session
_limit_notified: set = set()

# ── Strategy mode ──────────────────────────────────────────────────────────────
# "ORB"    : Opening Range Breakout (9:15–9:30 range, close-confirm breakout)
# "HYBRID" : ORB breakout + Gemini must agree on direction before trading
# "GEMINI" : Legacy — Gemini/rule-based signal on change_pct threshold
STRATEGY_MODE = "HYBRID"

# ── ORB exit parameters (used by run_signal in market_feed.py) ─────────────────
ORB_PROFIT_TARGET  =  75   # %  — close at +75% premium gain
ORB_STOP_LOSS      = -35   # %  — close at -35% premium loss

VWAP_PROFIT_TARGET =  25   # %  — VWAP reversal: close at +25% premium gain  (sweep best: Exit B)
VWAP_STOP_LOSS     = -20   # %  — VWAP reversal: close at -20% premium loss  (sweep best: Exit B)
VWAP_DEVIATION     = 0.003 # price vs 30-candle VWAP proxy threshold
VWAP_RSI_OB        = 60    # RSI overbought threshold (fade UP)
VWAP_RSI_OS        = 40    # RSI oversold threshold  (fade DOWN)

risk = RiskManager(
    max_daily_loss=5000,      # one bad trade shouldn't end the day
    max_trades_per_day=1,     # ORB = strictly one trade per day
    max_position_size=16250,  # 250 max premium × 65 lots
)

last_signal_time = 0
SIGNAL_COOLDOWN  = 60

_log_fn = print


def log(msg):
    _log_fn(msg)


def on_market_data(market_data: dict):
    global last_signal_time

    ledger = load_ledger()

    # Don't generate signal if position open or pending
    if ledger["open_position"]:
        return

    # Check pending signal via market_feed state
    try:
        import market_feed as mf
        if mf.state.get("pending_signal"):
            return
    except ImportError:
        pass

    # Cooldown
    now = time.time()
    if now - last_signal_time < SIGNAL_COOLDOWN:
        return
    last_signal_time = now

    # Get signal
    signal = get_trading_signal(market_data)

    if signal["action"] == "STAY_OUT":
        log(f"⏭️  Stay out — {signal['reason'][:70]}")
        return

    # Pick correct premium
    if signal["action"] == "BUY_CALL":
        premium = market_data.get("option_premium_ce") or market_data.get("option_premium", 100)
        strike  = market_data.get("ce_strike")
        symbol  = market_data.get("ce_symbol", "")
    else:
        premium = market_data.get("option_premium_pe") or market_data.get("option_premium", 100)
        strike  = market_data.get("pe_strike")
        symbol  = market_data.get("pe_symbol", "")

    # Try to get strike/symbol from market_feed state
    try:
        import market_feed as mf
        if signal["action"] == "BUY_CALL":
            premium = mf.state.get("ce_premium") or premium
            strike  = mf.state.get("ce_strike") or strike
            symbol  = mf.state.get("ce_symbol") or symbol
        else:
            premium = mf.state.get("pe_premium") or premium
            strike  = mf.state.get("pe_strike") or strike
            symbol  = mf.state.get("pe_symbol") or symbol
    except ImportError:
        pass

    signal["strike"]         = strike
    signal["trading_symbol"] = symbol

    # Get instrument key from market_feed state
    try:
        import market_feed as mf
        inst_key = (mf.state.get("ce_instrument") if signal["action"] == "BUY_CALL"
                    else mf.state.get("pe_instrument"))
    except ImportError:
        inst_key = None

    signal["instrument_key"] = inst_key

    option_data = {
        "premium":        premium,
        "strike":         strike,
        "trading_symbol": symbol,
        "instrument_key": inst_key
    }

    approval = risk.approve_trade(signal, premium, 65)
    if not approval["approved"]:
        log(f"🚫 {approval['reason']}")
        reason = approval["reason"]
        if "loss limit" in reason or "trades/day" in reason:
            if reason not in _limit_notified:
                _limit_notified.add(reason)
                notifier.notify_limit_hit(reason, load_ledger().get("total_pnl", 0))
        return

    log(f"🔔 Signal: {signal['action']} {symbol} ₹{premium} conf={signal['confidence']}")
    log("⚡ Press Y to trade, N to skip")

    notifier.notify_signal(
        signal["action"], symbol, premium,
        signal["confidence"], signal.get("source", ""),
    )
    notifier.log_signal(
        market_data.get("ltp"),
        market_data.get("orb_high"), market_data.get("orb_low"),
        market_data.get("orb_range"),
        signal["action"], signal.get("strike"),
        signal["confidence"], signal.get("reason", ""),
    )
    return {"signal": signal, "option_data": option_data}


def on_orb_signal(orb_result: dict, market_data: dict):
    """HYBRID mode: Gemini must confirm ORB breakout direction before trading."""
    try:
        import market_feed as mf
        _mf = mf
    except ImportError:
        _mf = None

    action = orb_result["signal"]["action"]
    gemini = get_trading_signal(market_data)

    if gemini["action"] != action:
        log(f"⚠️  HYBRID skip: ORB={action} Gemini={gemini['action']}")
        if _mf:
            _mf.state["orb_status"] = "HYBRID-SKIP"
        return None

    log(f"✅ HYBRID confirmed: {action} (ORB + Gemini agree)")

    signal               = orb_result["signal"].copy()
    signal["confidence"] = gemini["confidence"]
    signal["source"]     = "hybrid"
    option_data          = orb_result["option_data"]
    premium              = option_data["premium"]

    if not signal.get("instrument_key"):
        try:
            from option_chain import get_all_contracts, get_nearest_expiry
            opt_type  = "CE" if action == "BUY_CALL" else "PE"
            contracts = get_all_contracts()
            expiry    = get_nearest_expiry()
            _m = next((c for c in contracts
                       if c["expiry"] == expiry
                       and c["instrument_type"] == opt_type
                       and c["strike_price"] == float(signal.get("strike", 0))), None)
            if _m:
                signal["instrument_key"] = _m["instrument_key"]
                signal["trading_symbol"] = signal.get("trading_symbol") or _m["trading_symbol"]
                option_data["instrument_key"] = _m["instrument_key"]
        except Exception:
            pass

    approval = risk.approve_trade(signal, premium, 65)
    if not approval["approved"]:
        log(f"🚫 {approval['reason']}")
        reason = approval["reason"]
        if "loss limit" in reason or "trades/day" in reason:
            if reason not in _limit_notified:
                _limit_notified.add(reason)
                notifier.notify_limit_hit(reason, load_ledger().get("total_pnl", 0))
        return None

    sym = signal.get("trading_symbol", "")
    log(f"🔔 HYBRID Signal: {action} {sym} ₹{premium:.0f}")
    notifier.notify_signal(
        action, sym, premium,
        signal["confidence"], signal.get("source", ""),
    )
    notifier.log_signal(
        market_data.get("ltp"),
        market_data.get("orb_high"), market_data.get("orb_low"),
        market_data.get("orb_range"),
        action, signal.get("strike"),
        signal["confidence"], signal.get("reason", ""),
    )
    return {"signal": signal, "option_data": option_data}


def on_gap_signal(gap_pct: float, market_data: dict):
    """GAP AND GO: trade the 9:15 AM opening gap — no AI confirmation needed."""
    try:
        import market_feed as mf
        _mf = mf
    except ImportError:
        _mf = None

    action    = "BUY_CALL" if gap_pct > 0 else "BUY_PUT"
    direction = "UP" if gap_pct > 0 else "DOWN"

    if action == "BUY_CALL":
        premium = market_data.get("option_premium_ce") or market_data.get("option_premium", 100)
        strike  = market_data.get("ce_strike")
        symbol  = market_data.get("ce_symbol", "")
    else:
        premium = market_data.get("option_premium_pe") or market_data.get("option_premium", 100)
        strike  = market_data.get("pe_strike")
        symbol  = market_data.get("pe_symbol", "")

    if _mf:
        if action == "BUY_CALL":
            premium = _mf.state.get("ce_premium") or premium
            strike  = _mf.state.get("ce_strike")  or strike
            symbol  = _mf.state.get("ce_symbol")  or symbol
        else:
            premium = _mf.state.get("pe_premium") or premium
            strike  = _mf.state.get("pe_strike")  or strike
            symbol  = _mf.state.get("pe_symbol")  or symbol

    inst_key = None
    if _mf:
        inst_key = (_mf.state.get("ce_instrument") if action == "BUY_CALL"
                    else _mf.state.get("pe_instrument"))

    if not inst_key:
        try:
            from option_chain import get_all_contracts, get_nearest_expiry
            opt_type  = "CE" if action == "BUY_CALL" else "PE"
            atm       = round(market_data.get("ltp", 0) / 50) * 50
            contracts = get_all_contracts()
            expiry    = get_nearest_expiry()
            _m = next((c for c in contracts
                       if c["expiry"] == expiry
                       and c["instrument_type"] == opt_type
                       and c["strike_price"] == float(atm)), None)
            if _m:
                inst_key = _m["instrument_key"]
                symbol   = symbol or _m["trading_symbol"]
                strike   = strike or int(_m["strike_price"])
        except Exception:
            pass

    signal = {
        "action"        : action,
        "strike"        : strike,
        "reason"        : f"Gap {direction} {gap_pct:+.2f}% at 9:15 AM",
        "confidence"    : "HIGH",
        "source"        : "gap_and_go",
        "trading_symbol": symbol,
        "instrument_key": inst_key,
    }
    option_data = {
        "premium"       : premium,
        "strike"        : strike,
        "trading_symbol": symbol,
        "instrument_key": inst_key,
    }

    approval = risk.approve_trade(signal, premium, 65)
    if not approval["approved"]:
        log(f"🚫 {approval['reason']}")
        reason = approval["reason"]
        if any(k in reason for k in ("loss limit", "trades/day", "1 trade")):
            if reason not in _limit_notified:
                _limit_notified.add(reason)
                notifier.notify_limit_hit(reason, load_ledger().get("total_pnl", 0))
        return None

    log(f"🔔 GAP {direction} {gap_pct:+.2f}%: {action} {symbol} ₹{premium:.0f}")
    notifier.notify_signal(action, symbol, premium, "HIGH", "gap_and_go")
    notifier.log_signal(
        market_data.get("ltp"),
        None, None, None,
        action, strike, "HIGH", f"Gap {direction} {gap_pct:+.2f}%",
    )

    if _mf:
        _mf.state["gap_status"] = f"TRADED — {action}"

    return {"signal": signal, "option_data": option_data}


def on_vwap_signal(vwap: float, ltp: float, rsi: float, market_data: dict):
    """VWAP Reversal: fade overbought/oversold deviations from rolling VWAP."""
    try:
        import market_feed as mf
        _mf = mf
    except ImportError:
        _mf = None

    dev = (ltp - vwap) / vwap
    if dev > VWAP_DEVIATION and rsi > VWAP_RSI_OB:
        action    = "BUY_PUT"
        direction = "UP"
    elif dev < -VWAP_DEVIATION and rsi < VWAP_RSI_OS:
        action    = "BUY_CALL"
        direction = "DOWN"
    else:
        return None

    if action == "BUY_CALL":
        premium = market_data.get("option_premium_ce") or market_data.get("option_premium", 100)
        strike  = market_data.get("ce_strike")
        symbol  = market_data.get("ce_symbol", "")
    else:
        premium = market_data.get("option_premium_pe") or market_data.get("option_premium", 100)
        strike  = market_data.get("pe_strike")
        symbol  = market_data.get("pe_symbol", "")

    if _mf:
        if action == "BUY_CALL":
            premium = _mf.state.get("ce_premium") or premium
            strike  = _mf.state.get("ce_strike")  or strike
            symbol  = _mf.state.get("ce_symbol")  or symbol
        else:
            premium = _mf.state.get("pe_premium") or premium
            strike  = _mf.state.get("pe_strike")  or strike
            symbol  = _mf.state.get("pe_symbol")  or symbol

    inst_key = None
    if _mf:
        inst_key = (_mf.state.get("ce_instrument") if action == "BUY_CALL"
                    else _mf.state.get("pe_instrument"))

    signal = {
        "action"        : action,
        "strike"        : strike,
        "reason"        : f"VWAP dev {dev*100:+.2f}% RSI={rsi:.0f} — fade {direction}",
        "confidence"    : "HIGH",
        "source"        : "vwap_reversal",
        "trading_symbol": symbol,
        "instrument_key": inst_key,
    }
    option_data = {
        "premium"       : premium,
        "strike"        : strike,
        "trading_symbol": symbol,
        "instrument_key": inst_key,
    }

    approval = risk.approve_trade(signal, premium, 65)
    if not approval["approved"]:
        log(f"🚫 {approval['reason']}")
        reason = approval["reason"]
        if any(k in reason for k in ("loss limit", "trades/day", "1 trade")):
            if reason not in _limit_notified:
                _limit_notified.add(reason)
                notifier.notify_limit_hit(reason, load_ledger().get("total_pnl", 0))
        return None

    log(f"🔔 VWAP {direction} dev={dev*100:+.2f}% RSI={rsi:.0f}: {action} {symbol} ₹{premium:.0f}")
    notifier.notify_signal(action, symbol, premium, "HIGH", "vwap_reversal")
    notifier.log_signal(
        market_data.get("ltp"),
        None, None, None,
        action, strike, "HIGH", f"VWAP dev {dev*100:+.2f}% RSI={rsi:.0f}",
    )

    if _mf:
        _mf.state["vwap_signal"] = action
        _mf.state["vwap_status"] = f"SIGNAL {action} dev={dev*100:+.2f}% RSI={rsi:.0f}"

    return {"signal": signal, "option_data": option_data}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        show_summary()
