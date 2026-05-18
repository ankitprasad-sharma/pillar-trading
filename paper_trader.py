import json
import os
from datetime import datetime
try:
    import notifier as _notifier
except ImportError:
    _notifier = None

LEDGER_FILE = "paper_trades.json"
STARTING_CAPITAL = 100000  # ₹1 lakh virtual capital


def load_ledger() -> dict:
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE, "r") as f:
            return json.load(f)
    return {
        "capital": STARTING_CAPITAL,
        "trades": [],
        "open_position": None,
        "total_pnl": 0
    }


def save_ledger(ledger: dict):
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger, f, indent=2)


def _resolve_contract(signal: dict, market_data: dict) -> tuple:
    """Return (instrument_key, trading_symbol, strike), looking up option chain if needed."""
    instrument_key = signal.get("instrument_key") or ""
    trading_symbol = signal.get("trading_symbol") or ""
    strike         = signal.get("strike") or None

    if instrument_key and trading_symbol and strike:
        return instrument_key, trading_symbol, strike

    try:
        from option_chain import get_all_contracts, get_nearest_expiry
        opt_type      = "CE" if signal["action"] == "BUY_CALL" else "PE"
        ltp           = market_data.get("ltp", 0)
        lookup_strike = float(strike) if strike else round(ltp / 50) * 50
        contracts     = get_all_contracts()
        expiry        = get_nearest_expiry()
        match = next((c for c in contracts
                      if c["expiry"] == expiry
                      and c["instrument_type"] == opt_type
                      and c["strike_price"] == lookup_strike), None)
        if match:
            print(f"🔍 Resolved contract: {match['trading_symbol']} ({match['instrument_key']})")
            return match["instrument_key"], match["trading_symbol"], int(match["strike_price"])
        print(f"❌ No {opt_type} contract found for strike {lookup_strike} expiry {expiry}")
    except Exception as e:
        print(f"⚠️  Contract lookup failed: {e}")

    return instrument_key, trading_symbol, strike


def open_trade(signal: dict, market_data: dict) -> dict:
    ledger = load_ledger()

    if ledger["open_position"]:
        print(f"⚠️  Already in a position — close it first")
        return ledger

    premium = market_data.get("option_premium", 100)
    quantity = 65  # 1 lot
    cost = premium * quantity

    if cost > ledger["capital"]:
        print(f"❌ Insufficient capital: ₹{ledger['capital']:.2f} available, ₹{cost} needed")
        return ledger

    instrument_key, trading_symbol, strike = _resolve_contract(signal, market_data)

    if not instrument_key:
        msg = f"instrument_key missing for {signal.get('action')} — trade blocked"
        print(f"❌ {msg}")
        if _notifier:
            _notifier.notify_error(msg)
        return ledger

    trade = {
        "id": len(ledger["trades"]) + 1,
        "action": signal["action"],
        "strike": strike,
        "trading_symbol": trading_symbol,
        "instrument_key": instrument_key,
        "entry_premium": premium,
        "quantity": quantity,
        "cost": cost,
        "entry_ltp": market_data["ltp"],
        "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "exit_premium": None,
        "exit_ltp": None,
        "exit_time": None,
        "pnl": None,
        "status": "OPEN",
        "reason": signal["reason"],
        "confidence": signal["confidence"],
        "source": signal.get("source", ""),
    }

    ledger["capital"] -= cost
    ledger["open_position"] = trade
    save_ledger(ledger)

    if _notifier:
        source = trade.get("source", "")
        if "orb" in source or "hybrid" in source:
            stop_p   = premium * 0.65
            target_p = premium * 1.75
        else:
            stop_p   = premium * 0.70
            target_p = premium * 1.50
        _notifier.notify_trade_opened(
            trade["action"], trade.get("trading_symbol", ""),
            premium, quantity, stop_p, target_p, market_data["ltp"],
        )

    print(f"\n📝 PAPER TRADE OPENED")
    print(f"   Action:    {trade['action']}")
    print(f"   Strike:    {trade['strike']}")
    print(f"   Premium:   ₹{premium} × {quantity} = ₹{cost}")
    print(f"   Nifty LTP: ₹{market_data['ltp']}")
    print(f"   Capital remaining: ₹{ledger['capital']:.2f}")
    return ledger


def close_trade(market_data: dict, exit_premium: float = None,
                exit_reason: str = "") -> dict:
    ledger = load_ledger()

    if not ledger["open_position"]:
        print("⚠️  No open position to close")
        return ledger

    pos = ledger["open_position"]
    exit_prem = exit_premium or market_data.get("option_premium", 100)
    quantity = pos["quantity"]

    pnl = (exit_prem - pos["entry_premium"]) * quantity
    exit_value = exit_prem * quantity

    pos["exit_premium"] = exit_prem
    pos["exit_ltp"] = market_data["ltp"]
    pos["exit_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pos["pnl"] = pnl
    pos["status"] = "CLOSED"

    ledger["capital"] += (pos["cost"] + pnl)
    ledger["total_pnl"] += pnl
    ledger["trades"].append(pos)
    ledger["open_position"] = None
    save_ledger(ledger)

    if _notifier:
        _notifier.notify_trade_closed(
            pos["action"], pos.get("trading_symbol", ""),
            pos["entry_premium"], exit_prem, pnl,
            exit_reason or "manual", ledger["capital"],
        )
        _notifier.log_trade_result(
            pos["action"], pos.get("strike"), pos["entry_premium"],
            exit_prem, pnl, exit_reason,
        )

    emoji = "✅" if pnl >= 0 else "❌"
    print(f"\n📝 PAPER TRADE CLOSED")
    print(f"   Entry premium: ₹{pos['entry_premium']} → Exit: ₹{exit_prem}")
    print(f"   {emoji} P&L: ₹{pnl:+.2f}")
    print(f"   Capital now: ₹{ledger['capital']:.2f}")
    print(f"   Total P&L:   ₹{ledger['total_pnl']:+.2f}")
    return ledger


def show_summary():
    ledger = load_ledger()
    trades = ledger["trades"]

    print(f"\n{'='*50}")
    print(f"📊 PAPER TRADING SUMMARY")
    print(f"{'='*50}")
    print(f"Starting capital: ₹{STARTING_CAPITAL:,.2f}")
    print(f"Current capital:  ₹{ledger['capital']:,.2f}")
    print(f"Total P&L:        ₹{ledger['total_pnl']:+,.2f}")
    print(f"Total trades:     {len(trades)}")

    if trades:
        wins = [t for t in trades if t["pnl"] and t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] and t["pnl"] <= 0]
        win_rate = len(wins) / len(trades) * 100 if trades else 0

        print(f"Win rate:         {win_rate:.1f}%")
        print(f"Wins: {len(wins)} | Losses: {len(losses)}")

        print(f"\n{'─'*50}")
        print(f"{'#':<4} {'Action':<10} {'Entry':>7} {'Exit':>7} {'P&L':>10} {'Time'}")
        print(f"{'─'*50}")
        for t in trades[-10:]:  # last 10 trades
            pnl_str = f"₹{t['pnl']:+.0f}" if t['pnl'] else "OPEN"
            print(f"{t['id']:<4} {t['action']:<10} "
                  f"₹{t['entry_premium']:>5} "
                  f"₹{t.get('exit_premium', 0):>5} "
                  f"{pnl_str:>10}  {t['entry_time']}")

    if ledger["open_position"]:
        pos = ledger["open_position"]
        print(f"\n🟡 OPEN POSITION: {pos['action']} | "
              f"Strike {pos['strike']} | "
              f"Entry ₹{pos['entry_premium']} | "
              f"Since {pos['entry_time']}")
    print(f"{'='*50}")
