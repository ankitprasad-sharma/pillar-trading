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
import threading
from datetime import datetime
import requests
from dotenv import load_dotenv

load_dotenv()

_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
_LOG_FILE  = "signals_log.csv"

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
        f"🚀 *Pillar Trading started*\n"
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
        f"⚡ *SIGNAL: {_esc(action)}*\n"
        f"Contract: {_esc(symbol)}\n"
        f"Premium: ₹{premium:.0f} | Confidence: {confidence}\n"
        f"{src_line}\n"
        f"Awaiting approval..."
    )


def notify_trade_opened(action: str, symbol: str, premium: float,
                        quantity: int, stop: float, target: float,
                        nifty_ltp: float):
    cost = premium * quantity
    send(
        f"✅ *TRADE OPENED*\n"
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
        f"{header}\n"
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
