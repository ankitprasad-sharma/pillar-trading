"""
notifier.py — Telegram alerts for Pillar Trading.
Uses requests only (no extra dependencies). All sends are
fire-and-forget daemon threads — Telegram slowness never blocks trading.

Requires in .env:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import os
import csv
import time
import threading
from datetime import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

_BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID        = os.getenv("TELEGRAM_CHAT_ID", "")
_last_update_id  = [0]   # tracks last processed Telegram update for command polling
_trade_callback  = [None]  # set by market_feed to handle Y/N trade approval
_LOG_FILE  = "signals_log.csv"


def _mode_tag() -> str:
    """Return '[LIVE]' or '[PAPER]' header based on order_manager.LIVE_TRADING."""
    try:
        import order_manager
        return "⚠️ LIVE" if order_manager.LIVE_TRADING else "📝 PAPER"
    except Exception:
        return "📝 PAPER"

_LOG_HEADERS = [
    "timestamp", "nifty_ltp", "orb_high", "orb_low", "orb_range",
    "action", "strike", "confidence", "reason", "traded",
    "entry_premium", "exit_premium", "pnl", "exit_reason",
]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _esc(s) -> str:
    """Escape underscores so Telegram Markdown v1 doesn't mis-parse them."""
    return str(s).replace("_", "\\_")


def _ensure_log():
    if not os.path.exists(_LOG_FILE):
        with open(_LOG_FILE, "w", newline="") as f:
            csv.writer(f).writerow(_LOG_HEADERS)


# ── Public send ───────────────────────────────────────────────────────────────

def register_trade_callback(fn):
    """Register a function(ch: str) that handles 'y' or 'n' trade approval."""
    _trade_callback[0] = fn


def _format_status() -> str:
    """Build /status reply text from paper_trader ledger + market_feed state."""
    try:
        from paper_trader import load_ledger
        ledger = load_ledger()
        pos    = ledger.get("open_position")
        if not pos:
            return "📊 *No open position*"
        try:
            import market_feed as _mf
            live = _mf.state.get("pos_premium_live") or pos["entry_premium"]
            ltp  = _mf.state.get("ltp") or pos.get("entry_ltp", 0)
        except Exception:
            live = pos["entry_premium"]
            ltp  = pos.get("entry_ltp", 0)
        ep   = pos["entry_premium"]
        qty  = pos["quantity"]
        pct  = (live - ep) / ep * 100 if ep else 0
        pnl  = (live - ep) * qty
        emj  = "🟢" if pnl >= 0 else "🔴"
        return (
            f"📊 *OPEN POSITION* {_mode_tag()}\n"
            f"{_esc(pos['action'])} {_esc(pos.get('trading_symbol',''))}\n"
            f"Entry ₹{ep:.1f} → Live ₹{live:.1f}\n"
            f"{emj} P&L: ₹{pnl:+,.0f} ({pct:+.1f}%)\n"
            f"Nifty: ₹{ltp:,.0f}\n"
            f"Source: {_esc(pos.get('source',''))}\n"
            f"Since: {pos.get('entry_time','')}"
        )
    except Exception as e:
        return f"❌ Error reading status: {_esc(str(e)[:80])}"


def _format_summary() -> str:
    """Build /summary reply text from ledger."""
    try:
        from paper_trader import load_ledger, STARTING_CAPITAL
        ledger = load_ledger()
        trades = ledger.get("trades", [])
        wins   = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0 and t.get("pnl") is not None]
        wr     = len(wins) / len(trades) * 100 if trades else 0
        cap    = ledger.get("capital", STARTING_CAPITAL)
        pnl    = ledger.get("total_pnl", 0)
        ret    = (cap - STARTING_CAPITAL) / STARTING_CAPITAL * 100
        return (
            f"📊 *PORTFOLIO SUMMARY* {_mode_tag()}\n"
            f"Capital: ₹{cap:,.0f}  ({ret:+.2f}%)\n"
            f"Total P&L: ₹{pnl:+,.0f}\n"
            f"Trades: {len(trades)} | Wins: {len(wins)} | Losses: {len(losses)}\n"
            f"Win rate: {wr:.1f}%"
        )
    except Exception as e:
        return f"❌ Error reading summary: {_esc(str(e)[:80])}"


def send(text: str, parse_mode: str = "Markdown"):
    """Post one message to Telegram in a background thread. Silent on failure."""
    if not _BOT_TOKEN or not _CHAT_ID:
        return

    def _post():
        try:
            requests.post(
                f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
                json={"chat_id": _CHAT_ID, "text": text, "parse_mode": parse_mode},
                timeout=10,
            )
        except Exception:
            pass

    threading.Thread(target=_post, daemon=True).start()


# ── Public notification functions ─────────────────────────────────────────────

def notify_startup(capital: float, market_status: str):
    send(
        f"🚀 *Pillar Trading started* {_mode_tag()}\n"
        f"Capital: ₹{capital:,.0f}\n"
        f"Market: {_esc(market_status)}\n"
        f"ORB window: 9:15–9:30 AM"
    )


def notify_orb_locked(high: float, low: float, range_pt: float):
    if range_pt > 150:
        status = f"SKIP-WIDE ({range_pt:.0f}pt)"
    elif range_pt < 30:
        status = f"SKIP-NARROW ({range_pt:.0f}pt)"
    else:
        status = f"WATCHING ({range_pt:.0f}pt)"
    send(
        f"📐 *ORB LOCKED*\n"
        f"High: ₹{high:,.0f} | Low: ₹{low:,.0f}\n"
        f"Range: {range_pt:.0f} pts | {status}"
    )


def notify_signal(action: str, symbol: str, premium: float,
                  confidence: str, source: str):
    src_map = {
        "hybrid"    : "ORB + Gemini confirmed",
        "orb"       : "ORB breakout",
        "gemini"    : "Gemini signal",
        "rule_based": "Rule-based",
        "gap_and_go": "Gap and Go (9:15 AM)",
    }
    src_line = src_map.get(source, source or "Signal")
    send(
        f"⚡ *SIGNAL: {_esc(action)}* {_mode_tag()}\n"
        f"Contract: {_esc(symbol)}\n"
        f"Premium: ₹{premium:.0f} | Confidence: {confidence}\n"
        f"{src_line}\n"
        f"Reply */y* to trade or */n* to skip"
    )


def notify_trade_opened(action: str, symbol: str, premium: float,
                        quantity: int, stop: float, target: float,
                        nifty_ltp: float):
    cost = premium * quantity
    send(
        f"✅ *TRADE OPENED* {_mode_tag()}\n"
        f"{_esc(action)} {_esc(symbol)}\n"
        f"Entry: ₹{premium:.0f} × {quantity} = ₹{cost:,.0f}\n"
        f"Stop: ₹{stop:.0f} | Target: ₹{target:.0f}\n"
        f"Nifty: ₹{nifty_ltp:,.0f}"
    )


def notify_trade_closed(action: str, symbol: str, entry: float,
                        exit_p: float, pnl: float, reason: str,
                        capital: float):
    pct    = (exit_p - entry) / entry * 100 if entry else 0
    emoji  = "🟢" if pnl >= 0 else "🔴"
    is_stop = "stop" in reason.lower()
    header  = "🛑 *STOP LOSS HIT*" if is_stop else f"{emoji} *TRADE CLOSED*"
    send(
        f"{header} {_mode_tag()}\n"
        f"{_esc(action)} {_esc(symbol)}\n"
        f"Entry ₹{entry:.0f} → Exit ₹{exit_p:.0f} ({pct:+.1f}%)\n"
        f"P&L: ₹{pnl:+,.0f} | Reason: {_esc(reason)}\n"
        f"Capital: ₹{capital:,.0f}"
    )


def notify_limit_hit(reason: str, daily_pnl: float):
    send(
        f"⚠️ *DAILY LIMIT REACHED*\n"
        f"Reason: {reason}\n"
        f"Trading paused for today\n"
        f"Daily P&L: ₹{daily_pnl:+,.0f}"
    )


def notify_error(error_msg: str):
    send(f"❌ *System Error*\n{str(error_msg)[:200]}")


def notify_token_needed(auth_url: str, minutes_to_open: int):
    send(
        f"🔑 *UPSTOX TOKEN REFRESH NEEDED*\n\n"
        f"*📱 Phone:*\n"
        f"1. [Tap to login]({auth_url})\n"
        f"2. Complete Upstox login\n"
        f"3. Browser shows error — that's expected\n"
        f"4. Copy the code from the URL bar:\n"
        f"   `?code=XXXXXXXX`\n"
        f"5. Send this bot: `/code XXXXXXXX`\n"
        f"   _(code expires in ~30s — be quick)_\n\n"
        f"*💻 Computer:*\n"
        f"`python3 upstox_auth.py`\n\n"
        f"⏰ Market opens in *{minutes_to_open} min*\n"
        f"System resumes automatically after login."
    )


def notify_token_urgent(minutes_to_open: int):
    send(
        f"⚠️ *TOKEN STILL EXPIRED — {minutes_to_open} min to market open!*\n"
        f"Run `python3 upstox_auth.py` immediately."
    )


# ── Telegram command polling ──────────────────────────────────────────────────

def _reply(chat_id: str, text: str):
    """Send a direct reply to a specific Telegram chat."""
    if not _BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def _token_status_reply(chat_id: str):
    """Reply to /token with current token age and validity."""
    from dotenv import dotenv_values
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    vals     = dotenv_values(env_path)
    tok_time = float(vals.get("UPSTOX_TOKEN_TIME", "0") or "0")
    age_h    = (time.time() - tok_time) / 3600

    if tok_time == 0:
        msg = "⚠️ *Token age unknown* — `UPSTOX_TOKEN_TIME` not set\nRun `python3 upstox_auth.py`"
    elif age_h >= 24:
        msg = (f"⚠️ *Token expired* — {age_h:.1f}h old\n"
               f"Run `python3 upstox_auth.py` or use the phone flow.")
    elif age_h > 20:
        left = 24 - age_h
        msg  = (f"⚠️ *Token expiring soon — {age_h:.1f}h old*\n"
                f"Expires in ~{left:.1f}h — refresh recommended.")
    else:
        left = 24 - age_h
        msg  = (f"🔑 *Token age: {age_h:.1f}h — valid ✅*\n"
                f"Expires in ~{left:.1f}h")

    _reply(chat_id, msg)


def _exchange_code_for_token(code: str, chat_id: str):
    """Exchange OAuth code (from /code command) for access token and save to .env."""
    from dotenv import dotenv_values, set_key as _set_key
    env_path   = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    vals       = dotenv_values(env_path)
    api_key    = vals.get("UPSTOX_API_KEY", "")
    api_secret = vals.get("UPSTOX_API_SECRET", "")

    if not api_key or not api_secret:
        _reply(chat_id, "❌ API credentials missing from `.env`")
        return

    _reply(chat_id, "⏳ Exchanging code for token...")
    try:
        r = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "code": code.strip(),
                "client_id": api_key,
                "client_secret": api_secret,
                "redirect_uri": "http://127.0.0.1:3000",
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        data = r.json()
        if "access_token" not in data:
            err = data.get("message", str(data))[:100]
            _reply(chat_id, f"❌ Token exchange failed: {_esc(err)}")
            return

        _set_key(env_path, "UPSTOX_ACCESS_TOKEN", data["access_token"])
        _set_key(env_path, "UPSTOX_TOKEN_TIME",   str(int(time.time())))
        _reply(chat_id, "✅ *Token saved!* System will resume automatically.")
    except Exception as e:
        _reply(chat_id, f"❌ Error: {_esc(str(e)[:80])}")


def _handle_update(upd: dict):
    """Dispatch a single Telegram update to the right command handler."""
    msg     = upd.get("message", {})
    text    = msg.get("text", "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not chat_id or not text.startswith("/"):
        return
    parts = text.split()
    cmd   = parts[0].split("@")[0].lower()   # strip bot-name suffix e.g. /token@mybot
    if cmd == "/token":
        _token_status_reply(chat_id)
    elif cmd == "/code":
        if len(parts) >= 2:
            _exchange_code_for_token(parts[1], chat_id)
        else:
            _reply(chat_id,
                   "Usage: `/code XXXXXXXX`\n"
                   "Paste the code from the URL bar after Upstox login.")
    elif cmd == "/y":
        if not _trade_callback[0]:
            _reply(chat_id, "⚠️ System not ready")
            return
        _trade_callback[0]('y')
        _reply(chat_id, "⚡ Trade approved — check dashboard")
    elif cmd == "/n":
        if not _trade_callback[0]:
            _reply(chat_id, "⚠️ System not ready")
            return
        _trade_callback[0]('n')
        _reply(chat_id, "⏭️ Signal skipped")
    elif cmd == "/status":
        _reply(chat_id, _format_status())
    elif cmd == "/summary":
        _reply(chat_id, _format_summary())
    elif cmd == "/close":
        if not _trade_callback[0]:
            _reply(chat_id, "⚠️ System not ready")
            return
        # 'x' triggers handle_key('x') in market_feed → manual exit at current premium
        _trade_callback[0]('x')
        _reply(chat_id, "🔴 Close request sent — check dashboard")
    elif cmd == "/help":
        _reply(chat_id,
               "*Commands:*\n"
               "`/y` — approve pending signal\n"
               "`/n` — skip pending signal\n"
               "`/status` — current open position\n"
               "`/summary` — portfolio summary\n"
               "`/close` — exit current position\n"
               "`/token` — token validity check\n"
               "`/code XXXX` — submit OAuth code")


def _poll_commands():
    """Long-poll Telegram for bot commands. Runs in a daemon thread."""
    while True:
        if not _BOT_TOKEN:
            time.sleep(60)
            continue
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{_BOT_TOKEN}/getUpdates",
                params={"offset": _last_update_id[0] + 1, "timeout": 10},
                timeout=20,
            )
            if r.status_code == 200:
                for upd in r.json().get("result", []):
                    _last_update_id[0] = upd["update_id"]
                    threading.Thread(
                        target=_handle_update, args=(upd,), daemon=True
                    ).start()
        except Exception:
            time.sleep(10)


def start_command_polling():
    """Start Telegram command polling, skipping any pre-existing messages."""
    def _init_and_poll():
        # Drain existing updates so we don't replay old /token commands on restart
        if _BOT_TOKEN:
            try:
                r = requests.get(
                    f"https://api.telegram.org/bot{_BOT_TOKEN}/getUpdates",
                    params={"offset": -1},
                    timeout=10,
                )
                if r.status_code == 200:
                    updates = r.json().get("result", [])
                    if updates:
                        _last_update_id[0] = updates[-1]["update_id"]
            except Exception:
                pass
        _poll_commands()

    threading.Thread(target=_init_and_poll, daemon=True).start()


# ── CSV logging ───────────────────────────────────────────────────────────────

def log_signal(nifty_ltp, orb_high, orb_low, orb_range,
               action: str, strike, confidence: str, reason: str):
    """Append one row to signals_log.csv when a signal fires."""
    _ensure_log()
    try:
        with open(_LOG_FILE, "a", newline="") as f:
            csv.writer(f).writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                nifty_ltp  or "",
                orb_high   or "",
                orb_low    or "",
                orb_range  or "",
                action,
                strike     or "",
                confidence,
                reason,
                "N",
                "", "", "", "",
            ])
    except Exception:
        pass


def log_trade_result(action: str, strike, entry_premium: float,
                     exit_premium: float, pnl: float, exit_reason: str):
    """Update the most-recent untraded signal row with trade outcome."""
    _ensure_log()
    try:
        with open(_LOG_FILE, "r", newline="") as f:
            all_rows = list(csv.DictReader(f))

        updated = False
        for row in reversed(all_rows):
            if (
                not updated
                and row.get("action") == action
                and str(row.get("strike", "")) == str(strike or "")
                and row.get("traded") in ("N", "")
                and not row.get("exit_premium")
            ):
                row["traded"]        = "Y"
                row["entry_premium"] = entry_premium
                row["exit_premium"]  = round(exit_premium, 2)
                row["pnl"]           = round(pnl, 2)
                row["exit_reason"]   = exit_reason or ""
                updated = True
                break

        if updated:
            with open(_LOG_FILE, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_LOG_HEADERS)
                writer.writeheader()
                writer.writerows(all_rows)
    except Exception:
        pass
