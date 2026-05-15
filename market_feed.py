import logging
logging.disable(logging.CRITICAL)
import sys, os
sys.stderr = open(os.devnull, "w")

import time
import threading
import select
import termios
import tty
from datetime import datetime
from dotenv import load_dotenv
import upstox_client
from upstox_client import MarketDataStreamerV3

load_dotenv()

TOKEN     = os.getenv("UPSTOX_ACCESS_TOKEN")
NIFTY_KEY = "NSE_INDEX|Nifty 50"

import order_manager
import notifier

# ANSI
G  = "\033[92m"; R  = "\033[91m"; Y  = "\033[93m"
C  = "\033[96m"; W  = "\033[97m"; D  = "\033[2m"
B  = "\033[1m";  X  = "\033[0m"

def clr():
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()

state = {
    "prev_close"      : None,
    "ltp"             : None,
    "change_pct"      : 0.0,
    "last_update"     : None,
    "market_status"   : "CONNECTING...",
    "last_signal"     : None,
    "last_signal_time": None,
    "ce_premium"      : None,
    "pe_premium"      : None,
    "ce_strike"       : None,
    "pe_strike"       : None,
    "ce_instrument"   : None,
    "pe_instrument"   : None,
    "ce_symbol"       : "",
    "pe_symbol"       : "",
    "pending_signal"  : None,
    "pending_option"  : None,
    # FIX 1 — pos tracking initialized properly
    "pos_instrument"  : None,
    "pos_premium_live": None,
    "log"             : [],
    "running"         : True,
    "paused"          : False,
    "prev_close_locked": False,
    # ORB state
    "orb_day"         : None,    # trading date the current range belongs to
    "orb_high"        : None,    # range high (built 9:15–9:30, locked after)
    "orb_low"         : None,    # range low
    "orb_range"       : None,    # locked range size in points
    "orb_status"      : "BUILDING",  # BUILDING / WATCHING / BROKE UP|DN / SKIP-* / HYBRID-SKIP
    "orb_traded_today": False,   # True once a trade or skip-decision has been made today
    "orb_locked"      : False,   # True after 9:30 range is finalised
    "orb_signal"      : None,    # BUY_CALL / BUY_PUT / None after breakout detected
    "orb_trail_active": False,   # True once trailing stop raised to breakeven
    "strategy_mode"   : "GEMINI",    # updated from main.STRATEGY_MODE each signal cycle
    # Gap and Go state
    "gap_pct"         : None,    # % gap at 9:15 AM vs prev close (set once per day)
    "gap_signal"      : None,    # BUY_CALL / BUY_PUT / None
    "gap_traded"      : False,   # one trade per day guard
    "gap_status"      : "WAITING",   # WAITING / GAP UP x% / GAP DOWN x% / NO GAP / SKIP-* / TRADED
    # VWAP Reversal state
    "vwap"            : None,        # current rolling 30-candle VWAP proxy
    "rsi"             : None,        # current RSI(14)
    "vwap_deviation"  : None,        # (ltp - vwap) / vwap
    "vwap_signal"     : None,        # BUY_CALL / BUY_PUT set by on_vwap_signal
    "vwap_traded"     : False,       # one trade per day guard
    "vwap_status"     : "WAITING",   # WAITING / COMPUTING / WATCHING / SIGNAL / TRADED / SKIPPED
}

_draw_lock      = threading.Lock()
_signal_lock    = threading.Lock()
_streamer       = None
_last_draw      = 0
_last_tick_time = time.time()   # updated on every real WS message
_sub_strike     = None
_price_buffer   = []    # rolling Nifty closes for VWAP proxy and RSI
_vwap_day       = None  # date string — buffer resets on new day


def _compute_rsi(closes: list, period: int = 14) -> float:
    """Wilder smoothing RSI for VWAP reversal."""
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def heartbeat_loop():
    """Every 10s: update timestamp + poll position price if no recent tick"""
    import requests, os
    last_pos_tick = [time.time()]

    while state["running"]:
        time.sleep(10)
        if state["paused"]:
            continue

        # Check if feed is stale (no tick in 60s)
        last_tick_age = time.time() - _last_tick_time
        if last_tick_age > 60:
            state["market_status"] = "FEED STALE — reconnecting"
            add_log("⚠️  No ticks in 60s — token may be expired")
        state["last_update"] = datetime.now().strftime("%H:%M:%S") + " ✓"

        # Poll position price if WebSocket tick is stale (>30s)
        inst = state.get("pos_instrument")
        if inst and (time.time() - last_pos_tick[0]) > 30:
            try:
                token = os.getenv("UPSTOX_ACCESS_TOKEN")
                r = requests.get(
                    "https://api.upstox.com/v3/market-quote/ltp",
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/json"},
                    params={"instrument_key": inst},
                    timeout=5
                )
                if r.status_code == 200:
                    for k, v in r.json().get("data", {}).items():
                        p = v.get("last_price") or v.get("ltp", 0)
                        if p and p > 0:
                            state["pos_premium_live"] = p
                            last_pos_tick[0] = time.time()
            except Exception:
                pass

        # Also poll ATM if stale
        ce_inst = state.get("ce_instrument")
        pe_inst = state.get("pe_instrument")
        if ce_inst and pe_inst:
            try:
                token = os.getenv("UPSTOX_ACCESS_TOKEN")
                r = requests.get(
                    "https://api.upstox.com/v3/market-quote/ltp",
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/json"},
                    params={"instrument_key": f"{ce_inst},{pe_inst}"},
                    timeout=5
                )
                if r.status_code == 200:
                    for k, v in r.json().get("data", {}).items():
                        p = v.get("last_price") or v.get("ltp", 0)
                        if p and p > 0:
                            if ce_inst.split("|")[1] in k:
                                state["ce_premium"] = p
                            elif pe_inst.split("|")[1] in k:
                                state["pe_premium"] = p
            except Exception:
                pass

        draw()

def fetch_orb_range():
    """On restart: seed ORB high/low from today's intraday 1-minute candles."""
    import requests as _req
    from paper_trader import load_ledger as _ll

    now   = datetime.now()
    cur_t = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")

    if cur_t < "09:15":
        return  # market not open yet — nothing to restore

    try:
        r = _req.get(
            "https://api.upstox.com/v2/historical-candle/intraday/"
            "NSE_INDEX%7CNifty%2050/1minute",
            headers={"Authorization": f'Bearer {os.getenv("UPSTOX_ACCESS_TOKEN")}',
                     "Accept": "application/json"},
            timeout=5,
        )
        if r.status_code != 200:
            add_log(f"⚠️  ORB restore: HTTP {r.status_code}")
            return
        candles = r.json().get("data", {}).get("candles", [])
        if not candles:
            return

        # Candle: [timestamp, open, high, low, close, vol, oi]
        # Timestamp format: "2026-05-15T09:15:00+05:30" — slice [11:16] → "09:15"
        orb_candles = [c for c in candles if "09:15" <= c[0][11:16] <= "09:30"]
        if not orb_candles:
            return

        orb_high  = max(c[2] for c in orb_candles)
        orb_low   = min(c[3] for c in orb_candles)
        orb_range = orb_high - orb_low

        # Check ledger to see if a trade was already entered today
        ledger       = _ll()
        traded_today = any(
            t.get("entry_time", "").startswith(today)
            for t in ledger.get("trades", [])
        )
        if not traded_today and ledger.get("open_position"):
            traded_today = ledger["open_position"].get("entry_time", "").startswith(today)

        # Set orb_day first — prevents the on_message day-boundary reset from wiping this
        state["orb_day"]          = today
        state["orb_high"]         = orb_high
        state["orb_low"]          = orb_low
        state["orb_range"]        = orb_range
        state["orb_traded_today"] = traded_today

        if cur_t > "09:30":
            state["orb_locked"] = True
            if traded_today:
                state["orb_status"] = "WATCHING"
            elif orb_range > 150:
                state["orb_status"]       = f"SKIP-WIDE ({orb_range:.0f}pt)"
                state["orb_traded_today"] = True
            elif orb_range < 30:
                state["orb_status"]       = f"SKIP-NARROW ({orb_range:.0f}pt)"
                state["orb_traded_today"] = True
            else:
                state["orb_status"] = "WATCHING"
        else:
            state["orb_status"] = "BUILDING"

        add_log(
            f"📐 ORB restored: {orb_low:.0f}–{orb_high:.0f}"
            f" ({orb_range:.0f}pts) → {state['orb_status']}"
        )

        # Restore gap status from 9:15 candle open price (needs prev_close locked)
        prev_close = state.get("prev_close")
        if prev_close and state.get("gap_pct") is None:
            c915 = next((c for c in candles if c[0][11:16] == "09:15"), None)
            if c915:
                open_9_15 = c915[1]  # open price of 9:15 candle
                _gap = (open_9_15 - prev_close) / prev_close * 100
                state["gap_pct"] = _gap
                if abs(_gap) > 2.0:
                    state["gap_status"] = f"SKIP-EXTREME ({_gap:+.2f}%)"
                elif _gap > 0.5:
                    state["gap_signal"] = "BUY_CALL"
                    state["gap_status"] = f"GAP UP {_gap:+.2f}% — signal ready"
                elif _gap < -0.5:
                    state["gap_signal"] = "BUY_PUT"
                    state["gap_status"] = f"GAP DOWN {_gap:+.2f}% — signal ready"
                else:
                    state["gap_status"] = f"NO GAP ({_gap:+.2f}%)"
                add_log(f"📊 GAP restored: {_gap:+.2f}%")

    except Exception as e:
        add_log(f"⚠️  ORB range restore failed: {e}")


def fetch_prev_close() -> float:
    """Fetch true previous day close from historical API"""
    from datetime import date, timedelta
    import requests as _req
    try:
        # Use yesterday as to_date so today's incomplete candle is never included.
        # Go back 7 days as from_date to handle weekends and holidays.
        yesterday = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')
        week_ago  = (date.today() - timedelta(days=7)).strftime('%Y-%m-%d')
        r = _req.get(
            f'https://api.upstox.com/v2/historical-candle/NSE_INDEX%7CNifty%2050/day/{yesterday}/{week_ago}',
            headers={'Authorization': f'Bearer {os.getenv("UPSTOX_ACCESS_TOKEN")}',
                     'Accept': 'application/json'},
            timeout=5
        )
        candles = r.json().get('data', {}).get('candles', [])
        if candles:
            prev_close = candles[0][4]  # index 4 = close price; candles[0] = most recent completed day
            add_log(f'📅 Prev close locked: ₹{prev_close:,.2f}')
            return prev_close
    except Exception as e:
        add_log(f'⚠️  Prev close fetch failed: {e}')
    return None


def add_log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    state["log"].append(f"[{ts}] {msg}")
    state["log"] = state["log"][-7:]


def _log_auto(msg: str):
    """Append one timestamped line to auto_trades.log for every autonomous action."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open("auto_trades.log", "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def draw():
    global _last_draw
    if state["paused"]:
        return
    now = time.time()
    if now - _last_draw < 0.6:
        return
    _last_draw = now

    with _draw_lock:
        from paper_trader import load_ledger
        ledger    = load_ledger()
        pos       = ledger.get("open_position")
        capital   = ledger.get("capital", 100000)
        total_pnl = ledger.get("total_pnl", 0)
        trades    = ledger.get("trades", [])
        wins      = len([t for t in trades if t.get("pnl", 0) > 0])
        win_rate  = (wins / len(trades) * 100) if trades else 0

        ltp  = state["ltp"]
        cp   = state["prev_close"]
        chg  = state["change_pct"]
        ce   = state["ce_premium"]
        pe   = state["pe_premium"]
        ces  = state["ce_strike"]
        pes  = state["pe_strike"]
        sig  = state["last_signal"]
        pend = state["pending_signal"]
        popt = state["pending_option"]

        pc   = G if chg >= 0 else R
        arr  = "▲" if chg >= 0 else "▼"
        pnlc = G if total_pnl >= 0 else R
        sep  = f"{C}{'─'*62}{X}"

        lines = []
        mode_c = R if order_manager.LIVE_TRADING else Y
        mode_s = "⚠️  LIVE ORDERS ON" if order_manager.LIVE_TRADING else "PAPER MODE"
        lines.append(sep)
        lines.append(f"{C}{B}  🚀  PILLAR TRADING{X}  {mode_c}{B}[ {mode_s} ]{X}")
        lines.append(sep)

        msc = G if "OPEN" in state["market_status"] else R
        lines.append(
            f"{W}Market: {msc}{B}{state['market_status']:<18}{X}"
            f"{D}Updated: {state['last_update'] or '---'}{X}"
        )

        if ltp:
            prev_s = f"  {D}Prev: ₹{cp:,.2f}{X}" if cp else ""
            lines.append(
                f"{C}NIFTY 50  {pc}{B}₹{ltp:>10,.2f}  {arr} {chg:+.2f}%{X}{prev_s}"
            )
        else:
            lines.append(f"{C}NIFTY 50  {Y}Fetching...{X}")

        ce_s = f"{G}CE {int(ces) if ces else '?'}: ₹{ce:.2f}{X}" if ce else f"{D}CE: fetching...{X}"
        pe_s = f"{R}PE {int(pes) if pes else '?'}: ₹{pe:.2f}{X}" if pe else f"{D}PE: fetching...{X}"
        lines.append(f"{ce_s}    {pe_s}")

        o_h  = state.get("orb_high")
        o_l  = state.get("orb_low")
        o_r  = state.get("orb_range")
        o_st = state.get("orb_status", "BUILDING")
        sm   = state.get("strategy_mode", "GEMINI")
        if sm in ("ORB", "HYBRID"):
            if o_h and o_l:
                o_rng    = f"{o_r:.0f}pt" if o_r else "building"
                orb_line = f"ORB: H ₹{o_h:.0f} | L ₹{o_l:.0f} | Range {o_rng} | {o_st}"
            else:
                orb_line = "ORB: Building range (9:15–9:30)..."
            if "BROKE" in o_st:
                o_c = G if "UP" in o_st else R
            elif o_st == "WATCHING":
                o_c = Y
            elif "SKIP" in o_st or "HYBRID" in o_st:
                o_c = D
            else:
                o_c = C
            lines.append(f"  {o_c}{orb_line}{X}")

        g_pct = state.get("gap_pct")
        g_st  = state.get("gap_status", "WAITING")
        if g_pct is not None:
            if "UP" in g_st or "BUY_CALL" in g_st:
                g_c = G
            elif "DOWN" in g_st or "BUY_PUT" in g_st:
                g_c = R
            elif "SKIP" in g_st or "NO GAP" in g_st:
                g_c = D
            else:
                g_c = Y
            lines.append(f"  {g_c}GAP: {g_pct:+.2f}%  {g_st}{X}")
        else:
            lines.append(f"  {D}GAP: Waiting for 9:15 AM first tick{X}")

        vwap_v = state.get("vwap")
        rsi_v  = state.get("rsi")
        v_st   = state.get("vwap_status", "WAITING")
        if sm == "VWAP_REVERSAL" or vwap_v is not None:
            if vwap_v is not None:
                dev_pct = (state.get("vwap_deviation") or 0) * 100
                dev_c   = G if dev_pct >= 0 else R
                rsi_c   = R if (rsi_v or 50) > 62 else (G if (rsi_v or 50) < 38 else W)
                lines.append(
                    f"  {C}VWAP: ₹{vwap_v:,.0f}  "
                    f"Dev: {dev_c}{dev_pct:+.2f}%{X}  "
                    f"{rsi_c}RSI: {rsi_v:.0f}{X}  "
                    f"{D}{v_st}{X}"
                )
            else:
                lines.append(f"  {D}VWAP: {v_st}{X}")

        lines.append(sep)

        # Pending signal alert
        if pend and popt:
            sc = G if pend["action"] == "BUY_CALL" else R
            lines.append(
                f"{Y}{B}⚡ {sc}{pend['action']}{Y}  "
                f"{pend.get('trading_symbol', '')}  "
                f"₹{popt.get('premium', '?')}  "
                f"conf={pend['confidence']}{X}"
            )
            lines.append(f"{Y}{B}   >>> Press {G}Y {Y}to Trade   {R}N {Y}to Skip <<<{X}")
            lines.append(sep)

        # Position
        lines.append(f"{C}{B}POSITION{X}")
        if pos:
            ep    = pos["entry_premium"]
            qty   = pos["quantity"]
            sym   = pos.get("trading_symbol", "")
            e_ltp = pos.get("entry_ltp", 0)

            # Row 1 — bought contract live P&L
            # FIX 2 — use pos_premium_live not ATM premium
            pos_live = state["pos_premium_live"] or ep
            cpct1    = (pos_live - ep) / ep * 100
            upnl1    = (pos_live - ep) * qty
            uc1      = G if upnl1 >= 0 else R
            tracking = "live" if state["pos_premium_live"] else "entry"
            lines.append(f"  {C}{B}Bought contract: {D}({tracking}){X}")
            lines.append(
                f"  {W}{sym or 'N/A'}{X}  "
                f"Entry ₹{ep} → Live ₹{pos_live:.1f}  "
                f"{uc1}{B}₹{upnl1:+.0f} ({cpct1:+.1f}%){X}"
            )

            # Row 2 — current ATM for context
            cur_p   = (ce if pos["action"] == "BUY_CALL" else pe) or ep
            cur_sym = (state.get("ce_symbol") if pos["action"] == "BUY_CALL"
                       else state.get("pe_symbol"))
            cur_st  = (state.get("ce_strike") if pos["action"] == "BUY_CALL"
                       else state.get("pe_strike"))
            cpct2   = (cur_p - ep) / ep * 100
            upnl2   = (cur_p - ep) * qty
            uc2     = G if upnl2 >= 0 else R
            lines.append(f"  {Y}Current ATM:{X}")
            lines.append(
                f"  {W}{cur_sym or str(cur_st)}{X}  "
                f"Strike ₹{int(cur_st) if cur_st else '?'} → ₹{cur_p:.1f}  "
                f"{uc2}₹{upnl2:+.0f} ({cpct2:+.1f}%){X}"
            )
            if sm in ("ORB", "HYBRID"):
                trail_tag  = "  Trail:BE@+40% ✓" if state.get("orb_trail_active") else ""
                params_str = f"Stop:-35%  Target:+75%{trail_tag}  Force-exit:14:30"
            elif sm == "GAP_AND_GO":
                params_str = "Stop:-25%  Target:+40%  Force-exit:10:00"
            else:
                params_str = "Stop:-30%  Target:+50%"
            lines.append(
                f"  {D}{params_str}  "
                f"Nifty entry:₹{e_ltp:,.0f}  Since:{pos['entry_time']}{X}"
            )
        else:
            lines.append(f"  {D}No open position{X}")

        lines.append(sep)
        lines.append(f"{C}{B}PORTFOLIO{X}")
        lines.append(
            f"  {W}Capital: ₹{capital:>10,.2f}   "
            f"{pnlc}{B}P&L: ₹{total_pnl:+,.2f}{X}"
        )
        lines.append(
            f"  {W}Trades: {len(trades)}  Wins: {wins}  Win rate: {win_rate:.1f}%{X}"
        )
        lines.append(sep)

        lines.append(f"{C}{B}LAST SIGNAL{X}")
        if sig:
            sc = G if sig["action"] == "BUY_CALL" else (
                 R if sig["action"] == "BUY_PUT" else Y)
            lines.append(
                f"  {sc}{B}{sig['action']:<12}{X}"
                f"{D} conf={sig['confidence']} @ {state['last_signal_time']}{X}"
            )
            lines.append(f"  {D}{sig.get('reason', '')[:70]}{X}")
        else:
            lines.append(f"  {D}Waiting for first signal...{X}")

        lines.append(sep)
        lines.append(f"{C}{B}LOG{X}")
        for line in state["log"]:
            lc = G if any(x in line for x in ["✅", "🎯"]) else (
                 R if any(x in line for x in ["❌", "🛑"]) else W)
            lines.append(f"  {lc}{line}{X}")

        lines.append(sep)
        lines.append(
            f"{D}  q=quit  s=summary  "
            f"{G}Y{X}{D}=trade  {R}N{X}{D}=skip  {Y}X{X}{D}=exit now{X}"
        )

        clr()
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()


# ── Option subscription ───────────────────────────────────

def subscribe_options(ltp):
    global _sub_strike, _streamer
    from option_chain import (get_nearest_strike, get_nearest_expiry,
                               get_all_contracts)
    strike = get_nearest_strike(ltp)
    if strike == _sub_strike:
        return
    expiry    = get_nearest_expiry()
    contracts = get_all_contracts()

    ce = next((c for c in contracts
               if c["expiry"] == expiry
               and c["instrument_type"] == "CE"
               and c["strike_price"] == strike), None)
    pe = next((c for c in contracts
               if c["expiry"] == expiry
               and c["instrument_type"] == "PE"
               and c["strike_price"] == strike), None)

    if not ce or not pe:
        add_log(f"⚠️  No contracts for strike {strike}")
        return

    state["ce_instrument"] = ce["instrument_key"]
    state["pe_instrument"] = pe["instrument_key"]
    state["ce_strike"]     = strike
    state["pe_strike"]     = strike
    state["ce_symbol"]     = ce["trading_symbol"]
    state["pe_symbol"]     = pe["trading_symbol"]

    if _streamer:
        try:
            keys = [ce["instrument_key"], pe["instrument_key"]]
            _streamer.subscribe(keys, "full")
            _sub_strike = strike
            add_log(f"📡 ATM subscribed @ {strike}")
        except Exception as e:
            add_log(f"❌ Subscribe failed: {str(e)[:40]}")
    draw()


def subscribe_position(inst_key: str, entry_premium: float):
    """FIX 3 — subscribe to the exact bought contract"""
    global _streamer
    if not inst_key or not _streamer:
        return
    try:
        _streamer.subscribe([inst_key], "full")
        state["pos_instrument"]   = inst_key
        state["pos_premium_live"] = entry_premium
        add_log(f"📡 Position tracking: {inst_key}")
    except Exception as e:
        add_log(f"❌ Pos subscribe: {str(e)[:40]}")


# ── WebSocket callbacks ───────────────────────────────────

def on_message(message):
    global _last_tick_time
    _last_tick_time = time.time()
    msg_type = message.get("type")

    if msg_type == "market_info":
        s = message.get("marketInfo", {}).get("segmentStatus", {})
        state["market_status"] = s.get("NSE_INDEX", "UNKNOWN")
        add_log(f"Market: {state['market_status']}")
        draw()
        return

    for key, val in message.get("feeds", {}).items():

        # ── Option feed ──
        if "NSE_FO" in key:
            try:
                ff      = val.get("fullFeed", {})
                mff     = ff.get("marketFF", {})
                ltpc    = mff.get("ltpc", {})
                ltp_opt = ltpc.get("ltp", 0)

                if ltp_opt and ltp_opt > 0:
                    if key == state.get("ce_instrument"):
                        state["ce_premium"] = ltp_opt
                        draw()
                    elif key == state.get("pe_instrument"):
                        state["pe_premium"] = ltp_opt
                        draw()
                    # FIX 2 — track bought contract separately
                    if key == state.get("pos_instrument"):
                        state["pos_premium_live"] = ltp_opt
                        draw()
            except Exception:
                pass
            continue

        # ── Nifty index feed ──
        try:
            ltpc = val["fullFeed"]["indexFF"]["ltpc"]
            ltp  = ltpc.get("ltp")
            cp   = ltpc.get("cp")

            if ltp and ltp > 0:
                if cp and not state.get("prev_close_locked"):
                    state["prev_close"] = cp
                state["ltp"]         = ltp
                state["last_update"] = datetime.now().strftime("%H:%M:%S")
                if state["prev_close"]:
                    state["change_pct"] = (
                        (ltp - state["prev_close"]) / state["prev_close"] * 100
                    )
                draw()

                # ── ORB range tracking ────────────────────────────────
                _now     = datetime.now()
                _cur_t   = _now.strftime("%H:%M")
                _cur_day = _now.strftime("%Y-%m-%d")
                # Reset at day boundary
                if state["orb_day"] != _cur_day:
                    state["orb_day"]          = _cur_day
                    state["orb_high"]         = None
                    state["orb_low"]          = None
                    state["orb_range"]        = None
                    state["orb_status"]       = "BUILDING"
                    state["orb_traded_today"] = False
                    state["orb_locked"]       = False
                    state["orb_signal"]       = None
                    state["orb_trail_active"] = False
                    state["gap_pct"]          = None
                    state["gap_signal"]       = None
                    state["gap_traded"]       = False
                    state["gap_status"]       = "WAITING"
                    state["vwap"]             = None
                    state["rsi"]              = None
                    state["vwap_deviation"]   = None
                    state["vwap_signal"]      = None
                    state["vwap_traded"]      = False
                    state["vwap_status"]      = "WAITING"
                # Build range 9:15–9:30
                if "09:15" <= _cur_t <= "09:30":
                    if state["orb_high"] is None:
                        state["orb_high"] = ltp
                        state["orb_low"]  = ltp
                    else:
                        state["orb_high"] = max(state["orb_high"], ltp)
                        state["orb_low"]  = min(state["orb_low"],  ltp)
                # Lock range on first tick after 9:30
                elif (_cur_t > "09:30" and state["orb_status"] == "BUILDING"
                      and state["orb_high"] is not None):
                    _rng = state["orb_high"] - state["orb_low"]
                    state["orb_range"]  = _rng
                    state["orb_locked"] = True
                    if _rng > 150:
                        state["orb_status"]       = f"SKIP-WIDE ({_rng:.0f}pt)"
                        state["orb_traded_today"] = True
                    elif _rng < 30:
                        state["orb_status"]       = f"SKIP-NARROW ({_rng:.0f}pt)"
                        state["orb_traded_today"] = True
                    else:
                        state["orb_status"] = "WATCHING"
                    add_log(
                        f"📐 ORB: {state['orb_low']:.0f}–{state['orb_high']:.0f}"
                        f" ({_rng:.0f}pts) → {state['orb_status']}"
                    )
                    notifier.notify_orb_locked(
                        state["orb_high"], state["orb_low"], _rng
                    )

                # ── Gap detection — first 9:15 tick of the day ───────────────
                if (_cur_t == "09:15" and state["gap_pct"] is None
                        and state["prev_close"]):
                    _gap = (ltp - state["prev_close"]) / state["prev_close"] * 100
                    state["gap_pct"] = _gap
                    _vix = 14.0  # hardcoded until real VIX feed
                    if _vix > 18:
                        state["gap_status"] = f"SKIP-VIX ({_gap:+.2f}%)"
                    elif abs(_gap) > 2.0:
                        state["gap_status"] = f"SKIP-EXTREME ({_gap:+.2f}%)"
                    elif _gap > 0.5:
                        state["gap_signal"] = "BUY_CALL"
                        state["gap_status"] = f"GAP UP {_gap:+.2f}% — signal ready"
                        add_log(f"📈 Gap UP {_gap:+.2f}% detected @ 9:15")
                    elif _gap < -0.5:
                        state["gap_signal"] = "BUY_PUT"
                        state["gap_status"] = f"GAP DOWN {_gap:+.2f}% — signal ready"
                        add_log(f"📉 Gap DOWN {_gap:+.2f}% detected @ 9:15")
                    else:
                        state["gap_status"] = f"NO GAP ({_gap:+.2f}% < ±0.5%)"
                        add_log(f"➡️  No gap: {_gap:+.2f}%")

                # ── VWAP / RSI rolling buffer ─────────────────────────────────
                global _price_buffer, _vwap_day
                if _vwap_day != _cur_day:
                    _price_buffer         = []
                    _vwap_day             = _cur_day
                    state["vwap_status"]  = "COMPUTING"
                    state["vwap_traded"]  = False
                    state["vwap_signal"]  = None
                _price_buffer.append(ltp)
                if len(_price_buffer) >= 30:
                    _vwap = sum(_price_buffer[-30:]) / 30
                    _rsi  = _compute_rsi(_price_buffer)
                    state["vwap"]           = _vwap
                    state["rsi"]            = _rsi
                    state["vwap_deviation"] = (ltp - _vwap) / _vwap
                    if state["vwap_status"] == "COMPUTING":
                        state["vwap_status"] = "WATCHING"

                threading.Thread(
                    target=subscribe_options,
                    args=(ltp,),
                    daemon=True
                ).start()

                market_data = {
                    "ltp"              : ltp,
                    "prev_close"       : state["prev_close"] or ltp,
                    "change_pct"       : state["change_pct"],
                    "option_premium_ce": state["ce_premium"] or 100,
                    "option_premium_pe": state["pe_premium"] or 100,
                    "option_premium"   : state["ce_premium"] or 100,
                    "ce_strike"        : state["ce_strike"],
                    "volume"           : 0,
                    "vix"              : 14.0,
                    "time"             : datetime.now().strftime("%H:%M"),
                }
                threading.Thread(
                    target=run_signal,
                    args=(market_data,),
                    daemon=True
                ).start()
        except (KeyError, TypeError):
            pass


def on_error(m):
    add_log(f"❌ {str(m)[:55]}")
    notifier.notify_error(str(m)[:80])
    draw()


def on_close(m):
    add_log("🔌 Disconnected")
    draw()


def on_open():
    add_log("✅ Connected to Upstox")
    import main as _main
    _main.risk.reset_daily_counters()
    add_log("🔄 Daily risk counters reset")
    draw()
    threading.Thread(target=restore_position_subscription, daemon=True).start()
    # Fetch prev close first, then restore ORB+gap (gap calc needs prev_close)
    def startup_market_data():
        pc = fetch_prev_close()
        if pc:
            state["prev_close"]        = pc
            state["prev_close_locked"] = True
        fetch_orb_range()
    threading.Thread(target=startup_market_data, daemon=True).start()


def restore_position_subscription():
    """Always look up instrument key dynamically — never trust saved key"""
    from paper_trader import load_ledger
    from option_chain import get_all_contracts, get_nearest_expiry, get_nearest_strike
    import time as _t
    _t.sleep(2)

    ledger = load_ledger()
    pos    = ledger.get("open_position")
    if not pos:
        return

    action   = pos.get("action", "BUY_CALL")
    opt_type = "CE" if action == "BUY_CALL" else "PE"
    strike   = pos.get("strike")

    # Always derive strike from entry_ltp if missing or unreliable
    if not strike:
        entry_ltp = pos.get("entry_ltp", 0)
        if entry_ltp:
            strike = get_nearest_strike(entry_ltp)
            add_log(f"ℹ️  Derived strike from entry LTP: {strike}")
        else:
            add_log("⚠️  Can't restore — no strike or entry LTP")
            return

    # Always look up fresh from contracts — never trust saved key
    contracts = get_all_contracts()
    expiry    = get_nearest_expiry()
    match = next((c for c in contracts
                  if c["expiry"] == expiry
                  and c["instrument_type"] == opt_type
                  and c["strike_price"] == float(strike)), None)

    if match:
        inst_key = match["instrument_key"]
        # Update JSON with correct key for future
        import json
        with open("paper_trades.json") as f:
            data = json.load(f)
        if data.get("open_position"):
            data["open_position"]["instrument_key"]  = inst_key
            data["open_position"]["trading_symbol"]  = match["trading_symbol"]
            data["open_position"]["strike"]          = int(strike)
            with open("paper_trades.json", "w") as f:
                json.dump(data, f, indent=2)
        subscribe_position(inst_key, pos["entry_premium"])
        add_log(f"✅ Position key verified: {inst_key}")
    else:
        add_log(f"⚠️  Contract not found: {opt_type} @ {strike} {expiry}")


# ── Signal logic ──────────────────────────────────────────

def _check_orb_signal(market_data: dict):
    """ORB breakout detection only. Returns {signal, option_data} or None."""
    cur_time = datetime.now().strftime("%H:%M")
    if state.get("orb_status") != "WATCHING":
        return None
    if state.get("orb_traded_today"):
        return None
    if cur_time >= "14:30":
        return None

    ltp      = market_data.get("ltp", 0)
    orb_high = state.get("orb_high")
    orb_low  = state.get("orb_low")
    if not ltp or not orb_high or not orb_low:
        return None

    action = None
    if ltp > orb_high:
        action = "BUY_CALL"
    elif ltp < orb_low:
        action = "BUY_PUT"
    if not action:
        return None

    state["orb_traded_today"] = True
    state["orb_signal"]       = action
    orb_range = orb_high - orb_low
    direction = "UP" if action == "BUY_CALL" else "DN"
    state["orb_status"] = f"BROKE {direction} @ {ltp:.0f}"
    add_log(f"📐 ORB broke {direction} @ ₹{ltp:.0f} ({orb_low:.0f}–{orb_high:.0f})")

    strike   = round(ltp / 50) * 50
    premium  = (state["ce_premium"] if action == "BUY_CALL"
                else state["pe_premium"]) or ltp * 0.008
    inst_key = (state.get("ce_instrument") if action == "BUY_CALL"
                else state.get("pe_instrument"))
    sym      = (state.get("ce_symbol") if action == "BUY_CALL"
                else state.get("pe_symbol"))

    signal = {
        "action"        : action,
        "strike"        : strike,
        "reason"        : f"ORB break {direction} {orb_low:.0f}–{orb_high:.0f} ({orb_range:.0f}pts)",
        "confidence"    : "HIGH",
        "source"        : "orb",
        "trading_symbol": sym or "",
        "instrument_key": inst_key,
    }
    return {
        "signal"     : signal,
        "option_data": {"premium": premium, "strike": strike,
                        "trading_symbol": sym or "", "instrument_key": inst_key}
    }


def _execute_live_open(sig: dict, popt: dict) -> bool:
    """Place a live BUY order if LIVE_TRADING is enabled.
    Returns True to proceed (paper mode or order accepted), False to abort."""
    if not order_manager.LIVE_TRADING:
        return True
    ord_ = order_manager.place_order(sig, popt)
    if ord_["status"] == "FAILED":
        add_log(f"❌ Live order FAILED: {ord_['message'][:45]}")
        return False
    add_log(f"📋 BUY order placed: {ord_['order_id']}")
    return True


def _execute_live_close(pos: dict, cur_p: float) -> None:
    """Place a live SELL order if LIVE_TRADING is enabled. Logs result."""
    if not order_manager.LIVE_TRADING:
        return
    ord_ = order_manager.close_order(pos, cur_p)
    if ord_["status"] == "FAILED":
        add_log(f"⚠️  Live close FAILED: {ord_['message'][:45]}")
    else:
        add_log(f"📋 SELL order placed: {ord_['order_id']}")


def run_signal(market_data):
    if not _signal_lock.acquire(blocking=False):
        return
    try:
        from paper_trader import load_ledger, close_trade
        import main as _main
        _main._log_fn = add_log

        ledger        = load_ledger()
        strategy_mode = getattr(_main, "STRATEGY_MODE", "GEMINI")
        state["strategy_mode"] = strategy_mode
        cur_time      = datetime.now().strftime("%H:%M")

        # Monitor open position
        if ledger["open_position"]:
            pos      = ledger["open_position"]
            pos_live = state.get("pos_premium_live")
            cur_p    = pos_live or (
                state["ce_premium"] if pos["action"] == "BUY_CALL"
                else state["pe_premium"]
            )
            if not cur_p:
                return
            ep   = pos["entry_premium"]
            cpct = (cur_p - ep) / ep * 100
            market_data["option_premium"] = cur_p

            if strategy_mode in ("ORB", "HYBRID"):
                target = getattr(_main, "ORB_PROFIT_TARGET", 75)
                stop   = getattr(_main, "ORB_STOP_LOSS", -35)
                # Raise stop to breakeven once gain reaches +40%
                if cpct >= 40 and not state.get("orb_trail_active"):
                    state["orb_trail_active"] = True
                    add_log("🔒 Trail active — stop → breakeven")
                    _log_auto(f"TRAIL-ACTIVATED | {pos.get('action')} {pos.get('trading_symbol','')} | entry ₹{ep} cur ₹{cur_p:.1f} {cpct:+.1f}%")
                effective_stop = 0.0 if state.get("orb_trail_active") else stop
                exit_reason = None
                if cur_time >= "14:30":
                    exit_reason = f"⏰ 14:30 force exit ({cpct:+.1f}%)"
                elif cpct >= target:
                    exit_reason = f"🎯 +{cpct:.1f}% ORB target"
                elif cpct <= effective_stop:
                    label = "trail-stop" if state.get("orb_trail_active") else "stop"
                    exit_reason = f"🛑 {cpct:.1f}% ORB {label}"
                if exit_reason:
                    add_log(f"{exit_reason} — closing!")
                    _log_auto(f"AUTO-CLOSE ORB | {pos.get('action')} {pos.get('trading_symbol','')} | entry ₹{ep} cur ₹{cur_p:.1f} {cpct:+.1f}% | {exit_reason}")
                    _execute_live_close(pos, cur_p)
                    close_trade(market_data, cur_p, exit_reason)
                    state["pos_instrument"]   = None
                    state["pos_premium_live"] = None
                    state["orb_trail_active"] = False
                    draw()
            elif strategy_mode == "GAP_AND_GO":
                gap_exit = None
                if cur_time >= "10:00":
                    gap_exit = f"⏰ 10:00 AM exit ({cpct:+.1f}%)"
                elif cpct >= 40:
                    gap_exit = f"🎯 +{cpct:.1f}% gap target"
                elif cpct <= -25:
                    gap_exit = f"🛑 {cpct:.1f}% gap stop"
                if gap_exit:
                    add_log(f"{gap_exit} — closing!")
                    _log_auto(f"AUTO-CLOSE GAP | {pos.get('action')} {pos.get('trading_symbol','')} | entry ₹{ep} cur ₹{cur_p:.1f} {cpct:+.1f}% | {gap_exit}")
                    _execute_live_close(pos, cur_p)
                    close_trade(market_data, cur_p, gap_exit)
                    state["pos_instrument"]   = None
                    state["pos_premium_live"] = None
                    draw()
            elif strategy_mode == "VWAP_REVERSAL":
                target_pct = getattr(_main, "VWAP_PROFIT_TARGET", 25)
                stop_pct   = getattr(_main, "VWAP_STOP_LOSS", -20)
                vwap_v     = state.get("vwap")
                nifty_ltp  = market_data.get("ltp", 0)
                reverted   = False
                if vwap_v and nifty_ltp:
                    if pos["action"] == "BUY_PUT"  and nifty_ltp <= vwap_v:
                        reverted = True
                    elif pos["action"] == "BUY_CALL" and nifty_ltp >= vwap_v:
                        reverted = True
                vwap_exit = None
                if cur_time >= "14:00":
                    vwap_exit = f"⏰ 14:00 force exit ({cpct:+.1f}%)"
                elif cpct >= target_pct:
                    vwap_exit = f"🎯 +{cpct:.1f}% VWAP target"
                elif cpct <= stop_pct:
                    vwap_exit = f"🛑 {cpct:.1f}% VWAP stop"
                elif reverted and cpct > -5:
                    vwap_exit = f"↩️  reverted to VWAP ({cpct:+.1f}%)"
                if vwap_exit:
                    add_log(f"{vwap_exit} — closing!")
                    _log_auto(f"AUTO-CLOSE VWAP | {pos.get('action')} {pos.get('trading_symbol','')} | entry ₹{ep} cur ₹{cur_p:.1f} {cpct:+.1f}% | {vwap_exit}")
                    _execute_live_close(pos, cur_p)
                    close_trade(market_data, cur_p, vwap_exit)
                    state["pos_instrument"]   = None
                    state["pos_premium_live"] = None
                    draw()
            else:
                if cpct >= 50:
                    add_log(f"🎯 +{cpct:.1f}% profit — closing!")
                    _log_auto(f"AUTO-CLOSE GEMINI PROFIT | {pos.get('action')} {pos.get('trading_symbol','')} | entry ₹{ep} cur ₹{cur_p:.1f} {cpct:+.1f}%")
                    _execute_live_close(pos, cur_p)
                    close_trade(market_data, cur_p, "profit target")
                    state["pos_instrument"]   = None
                    state["pos_premium_live"] = None
                    draw()
                elif cpct <= -30:
                    add_log(f"🛑 {cpct:.1f}% stop loss — closing!")
                    _log_auto(f"AUTO-CLOSE GEMINI STOP | {pos.get('action')} {pos.get('trading_symbol','')} | entry ₹{ep} cur ₹{cur_p:.1f} {cpct:+.1f}%")
                    _execute_live_close(pos, cur_p)
                    close_trade(market_data, cur_p, "stop loss")
                    state["pos_instrument"]   = None
                    state["pos_premium_live"] = None
                    draw()
            return

        # Don't generate new signal while one is pending
        if state["pending_signal"]:
            return

        if strategy_mode == "GEMINI":
            result = _main.on_market_data(market_data)
        elif strategy_mode == "ORB":
            orb_result = _check_orb_signal(market_data)
            if orb_result:
                sig      = orb_result["signal"]
                premium  = orb_result["option_data"]["premium"]
                approval = _main.risk.approve_trade(sig, premium, 65)
                if approval["approved"]:
                    add_log(f"🔔 ORB Signal: {sig['action']} ₹{premium:.0f}")
                    result = orb_result
                else:
                    add_log(f"🚫 {approval['reason']}")
                    result = None
            else:
                result = None
        elif strategy_mode == "HYBRID":
            orb_result = _check_orb_signal(market_data)
            result     = _main.on_orb_signal(orb_result, market_data) if orb_result else None
        elif strategy_mode == "GAP_AND_GO":
            if (state.get("gap_signal") and not state.get("gap_traded")
                    and cur_time < "10:00"):
                state["gap_traded"] = True
                result = _main.on_gap_signal(state["gap_pct"], market_data)
            else:
                result = None
        elif strategy_mode == "VWAP_REVERSAL":
            vwap_v = state.get("vwap")
            rsi_v  = state.get("rsi")
            ltp_v  = market_data.get("ltp", 0)
            if (vwap_v and rsi_v and ltp_v
                    and not state.get("vwap_traded")
                    and not state.get("orb_traded_today")
                    and "10:00" <= cur_time <= "13:30"):
                dev = (ltp_v - vwap_v) / vwap_v
                _vd = getattr(_main, "VWAP_DEVIATION", 0.003)
                _rob = getattr(_main, "VWAP_RSI_OB", 60)
                _ros = getattr(_main, "VWAP_RSI_OS", 40)
                if (dev > _vd and rsi_v > _rob) or (dev < -_vd and rsi_v < _ros):
                    state["vwap_traded"] = True
                    state["vwap_status"] = "TRADED"
                    result = _main.on_vwap_signal(vwap_v, ltp_v, rsi_v, market_data)
                else:
                    result = None
            else:
                result = None
        else:
            result = None

        # Set pending atomically only if still empty
        if result and not state["pending_signal"]:
            state["pending_signal"]   = result["signal"]
            state["pending_option"]   = result["option_data"]
            state["last_signal"]      = result["signal"]
            state["last_signal_time"] = time.strftime("%H:%M:%S")
        draw()

    except Exception as e:
        add_log(f"❌ Signal: {str(e)[:50]}")
        draw()
    finally:
        _signal_lock.release()


# ── Streamer ──────────────────────────────────────────────

def run_streamer():
    global _streamer
    cfg = upstox_client.Configuration()
    cfg.access_token = TOKEN
    _streamer = MarketDataStreamerV3(
        upstox_client.ApiClient(cfg), [NIFTY_KEY], "full"
    )
    _streamer.on("message", on_message)
    _streamer.on("error",   on_error)
    _streamer.on("close",   on_close)
    _streamer.on("open",    on_open)
    _streamer.connect()


# ── Key handling ──────────────────────────────────────────

def handle_key(ch):
    ch = ch.lower()

    if ch == 'q':
        state["running"] = False

    elif ch == 's':
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        state["paused"] = True
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        clr()
        from paper_trader import show_summary
        show_summary()
        input("\nPress Enter to return...")
        tty.setcbreak(fd)
        state["paused"] = False
        draw()

    elif ch == 'y':
        sig  = state.get("pending_signal")
        popt = state.get("pending_option")
        if sig and popt:
            # Live order placement (if enabled); aborts on failure
            if not _execute_live_open(sig, popt):
                state["pending_signal"] = None
                state["pending_option"] = None
                draw()
                return
            # Paper trade — always recorded (parallel tracking / comparison)
            from paper_trader import open_trade
            open_trade(sig, {
                "ltp"           : state["ltp"] or 0,
                "prev_close"    : state["prev_close"] or 0,
                "option_premium": popt["premium"],
                "volume"        : 0,
                "vix"           : 14.0,
            })
            state["pending_signal"] = None
            state["pending_option"] = None
            # Always look up instrument key dynamically
            def sub_new_position(s=sig, p=popt):
                from option_chain import get_all_contracts, get_nearest_expiry
                action   = s.get("action", "BUY_CALL")
                opt_type = "CE" if action == "BUY_CALL" else "PE"
                strike   = s.get("strike")
                if not strike:
                    return
                contracts = get_all_contracts()
                expiry    = get_nearest_expiry()
                match = next((c for c in contracts
                              if c["expiry"] == expiry
                              and c["instrument_type"] == opt_type
                              and c["strike_price"] == float(strike)), None)
                if match:
                    # Update JSON with verified key
                    import json
                    with open("paper_trades.json") as f:
                        data = json.load(f)
                    if data.get("open_position"):
                        data["open_position"]["instrument_key"] = match["instrument_key"]
                        data["open_position"]["trading_symbol"] = match["trading_symbol"]
                        with open("paper_trades.json", "w") as f:
                            json.dump(data, f, indent=2)
                    subscribe_position(match["instrument_key"], p["premium"])
                    add_log(f"✅ Subscribed: {match['instrument_key']}")
                else:
                    add_log(f"⚠️  Contract not found for {opt_type} @ {strike}")
            threading.Thread(target=sub_new_position, daemon=True).start()
            add_log(f"✅ Opened {sig['action']} @ ₹{popt['premium']}")
        else:
            add_log("⚠️  No pending signal")
        draw()

    elif ch == 'n':
        if state.get("pending_signal"):
            add_log(f"⏭️  Skipped {state['pending_signal']['action']}")
            state["pending_signal"] = None
            state["pending_option"] = None
        draw()

    elif ch == 'x':
        from paper_trader import load_ledger, close_trade
        ledger = load_ledger()
        if ledger["open_position"]:
            pos   = ledger["open_position"]
            cur_p = state.get("pos_premium_live") or pos["entry_premium"]
            ep    = pos["entry_premium"]
            pnl   = (cur_p - ep) * pos["quantity"]
            add_log(f"🔴 Manual exit @ ₹{cur_p} P&L: ₹{pnl:+.0f}")
            _execute_live_close(pos, cur_p)
            close_trade({
                "ltp": state["ltp"] or 0,
                "prev_close": state["prev_close"] or 0,
                "option_premium": cur_p
            }, cur_p, "manual exit")
            state["pos_instrument"]   = None
            state["pos_premium_live"] = None
            state["orb_trail_active"] = False
        else:
            add_log("⚠️  No open position to exit")
        draw()


def input_loop():
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while state["running"]:
            ready, _, _ = select.select([sys.stdin], [], [], 0.1)
            if ready:
                ch = sys.stdin.read(1)
                handle_key(ch)
    except Exception as e:
        add_log(f"Input error: {e}")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Main ──────────────────────────────────────────────────

if __name__ == "__main__":
    from paper_trader import load_ledger as _load_startup
    _startup_ledger = _load_startup()
    notifier.notify_startup(
        _startup_ledger.get("capital", 100000),
        "CONNECTING",
    )
    add_log("Starting Pillar Trading...")
    draw()
    threading.Thread(target=run_streamer, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    try:
        input_loop()
    except KeyboardInterrupt:
        pass
    state["running"] = False
    clr()
    print(f"{G}👋 Pillar Trading stopped.{X}")
