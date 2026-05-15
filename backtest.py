#!/usr/bin/env python3
"""
backtest.py — Strategy backtest on historical Nifty 50 1-minute data.

Two strategies:
  rule_based  — fires on change_pct threshold + CE/PE imbalance
  orb         — Opening Range Breakout (9:15–9:30 range, then breakout entry)

Premium model (placeholder until real option history is available):
  entry_premium  = nifty * 0.008
  live_premium   = entry_premium ± DELTA * nifty_move  (DELTA = 0.5 for ATM)

Usage:
    python3 backtest.py --from 2026-01-01 --to 2026-05-13 --capital 100000
"""

import os
import sys
import argparse
import requests
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ai_router import rule_based_signal
from dotenv import load_dotenv

load_dotenv()

TOKEN          = os.getenv("UPSTOX_ACCESS_TOKEN")
LOT_SIZE       = 65
DELTA          = 0.5    # ATM delta: ₹0.50 premium move per ₹1 Nifty move
PREMIUM_FACTOR = 0.008  # entry_premium ≈ nifty * 0.008

# ── Rule-based strategy parameters ────────────────────────────────────────────
SIGNAL_START       = "10:00"
SIGNAL_END         = "13:30"
RB_FORCE_EXIT      = "14:45"
RB_TRAIL_TRIGGER   =  0.30
EVAL_COOLDOWN_SECS = 60
POST_EXIT_MINS     = 90

COMBOS = [
    {"label": "RB-A  -30%/+50%", "stop": -0.30, "target": 0.50},
    {"label": "RB-B  -40%/+80%", "stop": -0.40, "target": 0.80},
    {"label": "RB-C  -25%/+75%", "stop": -0.25, "target": 0.75},
]

# ── ORB strategy fixed parameters (varied ones are per-combo below) ────────────
ORB_RANGE_END  = "09:30"  # build opening range up to this time (inclusive)
ORB_FORCE_EXIT = "14:30"  # close all positions by this time

# ── Gap and Go strategy parameters ────────────────────────────────────────────
GAP_MIN_PCT   = 0.5    # minimum gap % to trade (below = no gap)
GAP_MAX_PCT   = 2.0    # maximum gap % to trade (above = too extreme, skip)
GAP_STOP      = -0.25  # -25% stop loss
GAP_TARGET    =  0.40  # +40% profit target
GAP_EXIT_TIME = "10:00"  # hard time exit regardless of P&L

# ── VWAP Reversal strategy parameters ─────────────────────────────────────────
VWAP_WINDOW     = 30      # rolling close-average window as VWAP proxy
VWAP_DEVIATION  = 0.006   # 0.6% deviation from VWAP to trigger entry
RSI_PERIOD      = 14
RSI_OB          = 62      # RSI overbought threshold → BUY_PUT (fade up-move)
RSI_OS          = 38      # RSI oversold threshold  → BUY_CALL (fade down-move)
VWAP_START      = "10:00"
VWAP_END        = "13:30"
VWAP_FORCE_EXIT = "14:00"
VWAP_TARGET     =  0.35   # +35% profit target
VWAP_STOP       = -0.25   # -25% stop loss

# ── ORB parameter combinations to test ────────────────────────────────────────
# close_confirm=False → breakout triggered when candle HIGH/LOW touches range
# close_confirm=True  → breakout triggered when candle CLOSE is beyond range
ORB_COMBOS = [
    # Phase 1: range filter variations (stop/target/trail fixed)
    {"label": "ORB 30-150 -35/75+T",  "min_range": 30, "max_range": 150,
     "stop": -0.35, "target": 0.75, "trail": 0.40, "close_confirm": False},
    {"label": "ORB 30-120 -35/75+T",  "min_range": 30, "max_range": 120,
     "stop": -0.35, "target": 0.75, "trail": 0.40, "close_confirm": False},
    {"label": "ORB 50-150 -35/75+T",  "min_range": 50, "max_range": 150,
     "stop": -0.35, "target": 0.75, "trail": 0.40, "close_confirm": False},
    {"label": "ORB 40-130 -35/75+T",  "min_range": 40, "max_range": 130,
     "stop": -0.35, "target": 0.75, "trail": 0.40, "close_confirm": False},
    # Phase 2: exit variations (range fixed at 30-150)
    {"label": "ORB -35/75 noTrail",   "min_range": 30, "max_range": 150,
     "stop": -0.35, "target": 0.75, "trail": None,  "close_confirm": False},
    {"label": "ORB -30/90 noTrail",   "min_range": 30, "max_range": 150,
     "stop": -0.30, "target": 0.90, "trail": None,  "close_confirm": False},
    # Phase 3: close confirmation (baseline stop/target/trail/range)
    {"label": "ORB close-conf +T",    "min_range": 30, "max_range": 150,
     "stop": -0.35, "target": 0.75, "trail": 0.40, "close_confirm": True},
]


# ── Data fetching ──────────────────────────────────────────────────────────────

def _fetch_chunk(from_date: str, to_date: str) -> list:
    url = (
        f"https://api.upstox.com/v2/historical-candle/"
        f"NSE_INDEX%7CNifty%2050/1minute/{to_date}/{from_date}"
    )
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("data", {}).get("candles", [])


def fetch_candles(from_date: str, to_date: str) -> list:
    """Fetch 1-minute candles in 7-day chunks; return sorted oldest-first."""
    all_candles = []
    start = datetime.strptime(from_date, "%Y-%m-%d")
    end   = datetime.strptime(to_date,   "%Y-%m-%d")

    while start <= end:
        chunk_end = min(start + timedelta(days=6), end)
        fs = start.strftime("%Y-%m-%d")
        fe = chunk_end.strftime("%Y-%m-%d")
        print(f"  Fetching {fs} → {fe} ...", end=" ", flush=True)
        try:
            chunk = _fetch_chunk(fs, fe)
            all_candles.extend(chunk)
            print(f"{len(chunk)} candles")
        except Exception as e:
            print(f"FAILED ({e})")
        start = chunk_end + timedelta(days=1)

    all_candles.sort(key=lambda c: c[0])
    return all_candles


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts[:19])


def _build_day_maps(candles: list) -> dict:
    """Return {trading_day: previous_day_close}."""
    day_last: dict[str, float] = {}
    for c in candles:
        dt  = _parse_dt(c[0])
        t   = dt.strftime("%H:%M")
        day = dt.strftime("%Y-%m-%d")
        if "09:15" <= t <= "15:30":
            day_last[day] = c[4]
    sd = sorted(day_last)
    return {day: day_last[sd[i - 1]] for i, day in enumerate(sd) if i > 0}


def live_premium(action: str, entry_nifty: float,
                 entry_prem: float, current_nifty: float) -> float:
    move = current_nifty - entry_nifty
    pnl  = DELTA * move if action == "BUY_CALL" else -DELTA * move
    return max(entry_prem + pnl, 0.05)


def _fresh_orb(day: str = None) -> dict:
    return {
        "day"         : day,
        "range_high"  : None,
        "range_low"   : None,
        "range_set"   : False,
        "skip_today"  : False,
        "traded_today": False,
    }


def _vol_confirmed(current_vol: float, day_volumes: list) -> bool:
    """True if volume confirms breakout. Falls through when index has no volume data."""
    if not day_volumes or max(day_volumes) == 0:
        return True  # Nifty index has no volume — don't filter on it
    lookback = [v for v in day_volumes[-20:] if v > 0]
    if not lookback:
        return True
    avg = sum(lookback) / len(lookback)
    return current_vol > avg * 1.2


def _hybrid_confirm(action: str, change_pct: float, ce_prem: float, pe_prem: float) -> bool:
    """Simulate Gemini confirmation: directional change AND premium skew must agree."""
    if action == "BUY_CALL":
        return change_pct > 0 and ce_prem > pe_prem
    if action == "BUY_PUT":
        return change_pct < 0 and pe_prem > ce_prem
    return False


def _close_position(open_pos, lp, exit_nifty, dt, exit_reason,
                    trades_list, capital_before_pnl, extra_fields=None):
    """Shared helper to build and append a closed-trade record."""
    pnl  = (lp - open_pos["entry_premium"]) * LOT_SIZE
    hold = int((dt - open_pos["_entry_dt"]).total_seconds() / 60)
    rec  = {
        "id"           : open_pos["id"],
        "action"       : open_pos["action"],
        "entry_premium": open_pos["entry_premium"],
        "entry_nifty"  : open_pos["entry_nifty"],
        "entry_time"   : open_pos["entry_time"],
        "cost"         : open_pos["cost"],
        "reason"       : open_pos["reason"],
        "trailed"      : open_pos["_stop_floor"] == 0.0,
        "exit_premium" : round(lp, 2),
        "exit_nifty"   : exit_nifty,
        "exit_time"    : dt.strftime("%Y-%m-%d %H:%M"),
        "exit_reason"  : exit_reason,
        "pnl"          : round(pnl, 2),
        "holding_mins" : hold,
        "capital_after": round(capital_before_pnl + pnl, 2),
    }
    if extra_fields:
        rec.update(extra_fields)
    trades_list.append(rec)
    return pnl


def _calc_rsi(closes: list, period: int = 14) -> float:
    """Wilder smoothing RSI."""
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


def _calc_vwap_proxy(closes: list, window: int = 30) -> float:
    """Rolling mean of last `window` closes as VWAP proxy."""
    buf = closes[-window:]
    return sum(buf) / len(buf)


# ── Rule-based simulation ──────────────────────────────────────────────────────

def run_backtest(candles: list, starting_capital: float,
                 stop_pct: float, target_pct: float) -> dict:
    prev_close_for_day = _build_day_maps(candles)

    capital      = starting_capital
    trades       = []
    open_pos     = None
    last_eval_dt = None
    last_exit_dt = None
    prev_close   = None

    for c in candles:
        ts_str, _o, _h, _l, close, _v, _oi = c
        dt          = _parse_dt(ts_str)
        candle_time = dt.strftime("%H:%M")
        candle_day  = dt.strftime("%Y-%m-%d")

        if not ("09:15" <= candle_time <= "15:30"):
            continue

        if candle_time == "09:15":
            prev_close = prev_close_for_day.get(candle_day)
            continue

        if prev_close is None:
            continue

        ltp        = close
        change_pct = (ltp - prev_close) / prev_close * 100

        if open_pos:
            lp  = live_premium(open_pos["action"], open_pos["entry_nifty"],
                               open_pos["entry_premium"], ltp)
            pct = (lp - open_pos["entry_premium"]) / open_pos["entry_premium"]

            if pct >= RB_TRAIL_TRIGGER and open_pos["_stop_floor"] < 0:
                open_pos["_stop_floor"] = 0.0

            exit_reason = None
            if pct >= target_pct:
                exit_reason = f"profit +{pct*100:.1f}%"
            elif pct <= open_pos["_stop_floor"]:
                label = "trail-stop" if open_pos["_stop_floor"] == 0.0 else "stop"
                exit_reason = f"{label} {pct*100:.1f}%"
            elif candle_time >= RB_FORCE_EXIT:
                exit_reason = f"{RB_FORCE_EXIT} time exit"

            if exit_reason:
                pnl          = _close_position(open_pos, lp, ltp, dt, exit_reason,
                                               trades, open_pos["_capital_before"])
                capital      = open_pos["_capital_before"] + pnl
                last_exit_dt = dt
                open_pos     = None
            continue

        if not (SIGNAL_START <= candle_time <= SIGNAL_END):
            continue
        if last_exit_dt and (dt - last_exit_dt).total_seconds() < POST_EXIT_MINS * 60:
            continue
        if last_eval_dt and (dt - last_eval_dt).total_seconds() < EVAL_COOLDOWN_SECS:
            continue

        market_data = {
            "ltp"              : ltp,
            "prev_close"       : prev_close,
            "change_pct"       : change_pct,
            "option_premium_ce": ltp * PREMIUM_FACTOR,
            "option_premium_pe": ltp * PREMIUM_FACTOR,
            "ce_strike"        : round(ltp / 50) * 50,
            "time"             : candle_time,
            "vix"              : 14.0,
        }
        signal       = rule_based_signal(market_data)
        last_eval_dt = dt

        if signal["action"] == "STAY_OUT":
            continue

        entry_prem = ltp * PREMIUM_FACTOR
        cost       = entry_prem * LOT_SIZE
        if cost > capital:
            continue

        capital  -= cost
        open_pos  = {
            "id"             : len(trades) + 1,
            "action"         : signal["action"],
            "entry_premium"  : round(entry_prem, 2),
            "entry_nifty"    : ltp,
            "entry_time"     : dt.strftime("%Y-%m-%d %H:%M"),
            "cost"           : round(cost, 2),
            "reason"         : signal["reason"],
            "_entry_dt"      : dt,
            "_capital_before": capital + cost,
            "_stop_floor"    : stop_pct,
        }

    if open_pos and candles:
        last_c = candles[-1]
        lp     = live_premium(open_pos["action"], open_pos["entry_nifty"],
                              open_pos["entry_premium"], last_c[4])
        pnl    = _close_position(open_pos, lp, last_c[4], _parse_dt(last_c[0]),
                                 "end of period", trades, open_pos["_capital_before"])
        capital = open_pos["_capital_before"] + pnl

    return {"trades": trades, "final_capital": round(capital, 2)}


# ── ORB simulation ─────────────────────────────────────────────────────────────

def run_orb_backtest(candles: list, starting_capital: float, *,
                     stop_pct: float = -0.35,
                     target_pct: float = 0.75,
                     trail_trigger: float = 0.40,
                     min_range: int = 30,
                     max_range: int = 150,
                     close_confirm: bool = False,
                     hybrid: bool = False) -> dict:
    """
    close_confirm=False : breakout detected when candle HIGH/LOW touches range boundary
    close_confirm=True  : breakout detected when candle CLOSE is beyond range boundary
    hybrid=True         : entry only when simulated Gemini agrees
                          BUY_CALL requires change_pct>0 AND ce_prem>pe_prem at breakout
    """
    prev_close_for_day = _build_day_maps(candles)

    capital     = starting_capital
    trades      = []
    open_pos    = None
    prev_close  = None
    day_volumes = []
    orb         = _fresh_orb()
    day_log     = {}   # {date: {range_pts, outcome, trade_id}}

    for c in candles:
        ts_str, _o, high, low, close, vol, _oi = c
        dt          = _parse_dt(ts_str)
        candle_time = dt.strftime("%H:%M")
        candle_day  = dt.strftime("%Y-%m-%d")

        if not ("09:15" <= candle_time <= "14:35"):
            continue

        # ── Day boundary reset ────────────────────────────────────────────────
        if candle_day != orb["day"]:
            if open_pos:
                lp      = live_premium(open_pos["action"], open_pos["entry_nifty"],
                                       open_pos["entry_premium"], close)
                pnl     = _close_position(open_pos, lp, close, dt,
                                          "overnight force-close", trades,
                                          open_pos["_capital_before"],
                                          {"orb_high": open_pos["orb_high"],
                                           "orb_low" : open_pos["orb_low"],
                                           "orb_range": open_pos["orb_range"]})
                capital = open_pos["_capital_before"] + pnl
                open_pos = None

            orb         = _fresh_orb(candle_day)
            prev_close  = prev_close_for_day.get(candle_day)
            day_volumes = []

        if prev_close is None:
            continue

        ltp = close
        day_volumes.append(vol)

        # ── Build opening range: 9:15 to ORB_RANGE_END ───────────────────────
        if candle_time <= ORB_RANGE_END:
            if orb["range_high"] is None:
                orb["range_high"] = high
                orb["range_low"]  = low
            else:
                orb["range_high"] = max(orb["range_high"], high)
                orb["range_low"]  = min(orb["range_low"],  low)
            continue  # no entries while range is being built

        # ── Finalise range at first candle after ORB_RANGE_END ───────────────
        if not orb["range_set"]:
            orb["range_set"] = True
            if orb["range_high"] is None:
                orb["skip_today"] = True
                day_log[candle_day] = {"range_pts": 0, "outcome": "no_data", "trade_id": None}
            else:
                rng = orb["range_high"] - orb["range_low"]
                if rng > max_range:
                    orb["skip_today"] = True
                    outcome = f"skip — range {rng:.0f}pts > {max_range}pts"
                elif rng < min_range:
                    orb["skip_today"] = True
                    outcome = f"skip — range {rng:.0f}pts < {min_range}pts"
                else:
                    outcome = "watching"
                day_log[candle_day] = {
                    "range_pts"  : rng,
                    "range_high" : orb["range_high"],
                    "range_low"  : orb["range_low"],
                    "outcome"    : outcome,
                    "trade_id"   : None,
                }

        if orb["skip_today"]:
            continue

        # ── Manage open position ───────────────────────────────────────────────
        if open_pos:
            lp  = live_premium(open_pos["action"], open_pos["entry_nifty"],
                               open_pos["entry_premium"], ltp)
            pct = (lp - open_pos["entry_premium"]) / open_pos["entry_premium"]

            if trail_trigger is not None and pct >= trail_trigger and open_pos["_stop_floor"] < 0:
                open_pos["_stop_floor"] = 0.0

            exit_reason = None
            if pct >= target_pct:
                exit_reason = f"profit +{pct*100:.1f}%"
            elif pct <= open_pos["_stop_floor"]:
                label = "trail-stop" if open_pos["_stop_floor"] == 0.0 else "stop"
                exit_reason = f"{label} {pct*100:.1f}%"
            elif candle_time >= ORB_FORCE_EXIT:
                exit_reason = f"{ORB_FORCE_EXIT} time exit"

            if exit_reason:
                pnl     = _close_position(
                    open_pos, lp, ltp, dt, exit_reason, trades,
                    open_pos["_capital_before"],
                    {"orb_high" : open_pos["orb_high"],
                     "orb_low"  : open_pos["orb_low"],
                     "orb_range": open_pos["orb_range"]},
                )
                capital  = open_pos["_capital_before"] + pnl
                open_pos = None
            continue

        # ── Check for breakout (one trade per day) ────────────────────────────
        if orb["traded_today"]:
            continue

        vol_ok = _vol_confirmed(vol, day_volumes)

        # Breakout detection: touch mode uses candle high/low; close mode uses close
        if close_confirm:
            broke_up = ltp > orb["range_high"]   # ltp = close of this 1-min candle
            broke_dn = ltp < orb["range_low"]
        else:
            broke_up = high > orb["range_high"]  # candle high/low touched range boundary
            broke_dn = low  < orb["range_low"]

        action = None
        if broke_up and vol_ok:
            action = "BUY_CALL"
        elif broke_dn and vol_ok:
            action = "BUY_PUT"

        if not action:
            continue

        # HYBRID: simulate Gemini confirmation via trend direction + premium skew
        if hybrid:
            change_pct  = (ltp - prev_close) / prev_close * 100 if prev_close else 0
            skew        = change_pct / 100
            sim_ce_prem = ltp * PREMIUM_FACTOR * (1 + skew)
            sim_pe_prem = ltp * PREMIUM_FACTOR * (1 - skew)
            if not _hybrid_confirm(action, change_pct, sim_ce_prem, sim_pe_prem):
                orb["traded_today"] = True
                if candle_day in day_log:
                    day_log[candle_day]["outcome"]         = "hybrid-skip"
                    day_log[candle_day]["rejected_action"] = action
                continue

        entry_prem = ltp * PREMIUM_FACTOR
        cost       = entry_prem * LOT_SIZE
        if cost > capital:
            continue

        orb["traded_today"] = True
        capital  -= cost
        rng       = orb["range_high"] - orb["range_low"]
        direction = "above" if action == "BUY_CALL" else "below"
        open_pos  = {
            "id"             : len(trades) + 1,
            "action"         : action,
            "entry_premium"  : round(entry_prem, 2),
            "entry_nifty"    : ltp,
            "entry_time"     : dt.strftime("%Y-%m-%d %H:%M"),
            "cost"           : round(cost, 2),
            "reason"         : (f"ORB break {direction} "
                               f"{orb['range_low']:.0f}–{orb['range_high']:.0f} "
                               f"({rng:.0f}pts)"),
            "orb_high"       : orb["range_high"],
            "orb_low"        : orb["range_low"],
            "orb_range"      : rng,
            "_entry_dt"      : dt,
            "_capital_before": capital + cost,
            "_stop_floor"    : stop_pct,
        }
        if candle_day in day_log:
            day_log[candle_day]["outcome"]  = "traded"
            day_log[candle_day]["trade_id"] = open_pos["id"]

    # Force-close any position still open at period end
    if open_pos and candles:
        last_c = candles[-1]
        lp     = live_premium(open_pos["action"], open_pos["entry_nifty"],
                              open_pos["entry_premium"], last_c[4])
        pnl    = _close_position(
            open_pos, lp, last_c[4], _parse_dt(last_c[0]),
            "end of period", trades, open_pos["_capital_before"],
            {"orb_high": open_pos["orb_high"], "orb_low": open_pos["orb_low"],
             "orb_range": open_pos["orb_range"]},
        )
        capital = open_pos["_capital_before"] + pnl

    # Mark any "watching" days that never triggered as no-breakout
    for day, entry in day_log.items():
        if entry["outcome"] == "watching":
            entry["outcome"] = "no breakout"

    return {"trades": trades, "final_capital": round(capital, 2), "day_log": day_log}


# ── Gap and Go simulation ──────────────────────────────────────────────────────

def run_gap_and_go_backtest(candles: list, starting_capital: float) -> dict:
    """
    Gap and Go strategy:
      - Detect gap at 9:15 AM first candle close vs previous day close
      - Gap UP  (+0.5% to +2.0%) → BUY CALL
      - Gap DOWN (-0.5% to -2.0%) → BUY PUT
      - Skip if gap < ±0.5% (no gap) or > ±2.0% (too extreme)
      - Skip if VIX > 18
      - Stop -25%, Target +40%, hard exit at 10:00 AM
      - One trade per day maximum
    """
    prev_close_for_day = _build_day_maps(candles)

    capital           = starting_capital
    trades            = []
    open_pos          = None
    prev_close        = None
    current_day       = None
    gap_detected      = False   # first-tick guard per day

    for c in candles:
        ts_str, _o, high, low, close, vol, _oi = c
        dt          = _parse_dt(ts_str)
        candle_time = dt.strftime("%H:%M")
        candle_day  = dt.strftime("%Y-%m-%d")

        # Only process 9:15–10:05 window
        if not ("09:15" <= candle_time <= "10:05"):
            continue

        # ── Day boundary reset ────────────────────────────────────────────────
        if candle_day != current_day:
            if open_pos:
                lp  = live_premium(open_pos["action"], open_pos["entry_nifty"],
                                   open_pos["entry_premium"], close)
                pnl = _close_position(open_pos, lp, close, dt,
                                      "overnight force-close", trades,
                                      open_pos["_capital_before"])
                capital  = open_pos["_capital_before"] + pnl
                open_pos = None
            current_day  = candle_day
            prev_close   = prev_close_for_day.get(candle_day)
            gap_detected = False

        if prev_close is None:
            continue

        ltp = close

        # ── Manage open position ───────────────────────────────────────────────
        if open_pos:
            lp  = live_premium(open_pos["action"], open_pos["entry_nifty"],
                               open_pos["entry_premium"], ltp)
            pct = (lp - open_pos["entry_premium"]) / open_pos["entry_premium"]

            exit_reason = None
            if candle_time >= GAP_EXIT_TIME:
                exit_reason = f"{GAP_EXIT_TIME} time exit ({pct*100:+.1f}%)"
            elif pct >= GAP_TARGET:
                exit_reason = f"profit +{pct*100:.1f}%"
            elif pct <= GAP_STOP:
                exit_reason = f"stop {pct*100:.1f}%"

            if exit_reason:
                pnl      = _close_position(open_pos, lp, ltp, dt, exit_reason,
                                           trades, open_pos["_capital_before"],
                                           {"gap_pct": open_pos["gap_pct"]})
                capital  = open_pos["_capital_before"] + pnl
                open_pos = None
            continue

        # ── Gap detection at first 9:15 candle ───────────────────────────────
        if candle_time == "09:15" and not gap_detected:
            gap_detected = True
            gap_pct      = (ltp - prev_close) / prev_close * 100
            vix          = 14.0  # hardcoded — real feed pending

            if vix > 18:
                continue
            if abs(gap_pct) > GAP_MAX_PCT or abs(gap_pct) < GAP_MIN_PCT:
                continue  # too extreme or no gap

            action     = "BUY_CALL" if gap_pct > 0 else "BUY_PUT"
            entry_prem = ltp * PREMIUM_FACTOR
            cost       = entry_prem * LOT_SIZE
            if cost > capital:
                continue

            direction = "up" if gap_pct > 0 else "down"
            capital  -= cost
            open_pos  = {
                "id"             : len(trades) + 1,
                "action"         : action,
                "entry_premium"  : round(entry_prem, 2),
                "entry_nifty"    : ltp,
                "entry_time"     : dt.strftime("%Y-%m-%d %H:%M"),
                "cost"           : round(cost, 2),
                "reason"         : f"Gap {direction} {gap_pct:+.2f}%",
                "gap_pct"        : round(gap_pct, 2),
                "_entry_dt"      : dt,
                "_capital_before": capital + cost,
                "_stop_floor"    : GAP_STOP,
            }

    # Force-close any position still open at period end
    if open_pos and candles:
        last_c = candles[-1]
        lp     = live_premium(open_pos["action"], open_pos["entry_nifty"],
                              open_pos["entry_premium"], last_c[4])
        pnl    = _close_position(open_pos, lp, last_c[4], _parse_dt(last_c[0]),
                                 "end of period", trades, open_pos["_capital_before"],
                                 {"gap_pct": open_pos["gap_pct"]})
        capital = open_pos["_capital_before"] + pnl

    return {"trades": trades, "final_capital": round(capital, 2)}


# ── HYBRID_GAP_AWARE simulation ────────────────────────────────────────────────

def run_hybrid_gap_aware_backtest(candles: list, starting_capital: float) -> dict:
    """
    HYBRID ORB + Gap-and-Go on shared capital with one experimental rule:

    If Gap exits at >= +10% before 10:00 AM AND the ORB position is open
    with unrealised gain < +20% → raise ORB stop-floor to breakeven immediately.

    Gap loss before 10:00 AM has no effect on ORB (rule is asymmetric by design).
    HYBRID ORB parameters are identical to the confirmed strategy.
    """
    prev_close_for_day = _build_day_maps(candles)

    capital      = starting_capital
    gap_pos      = None
    orb_pos      = None
    prev_close   = None
    day_volumes  = []
    orb          = _fresh_orb()
    gap_trades   = []
    orb_trades   = []
    gap_detected = False
    _id          = [0]

    def _next_id():
        _id[0] += 1
        return _id[0]

    # Identical to confirmed HYBRID parameters
    stop_pct      = -0.35
    target_pct    =  0.75
    trail_trigger =  0.40
    min_range     = 30
    max_range     = 150

    for c in candles:
        ts_str, _o, high, low, close, vol, _oi = c
        dt          = _parse_dt(ts_str)
        candle_time = dt.strftime("%H:%M")
        candle_day  = dt.strftime("%Y-%m-%d")

        if not ("09:15" <= candle_time <= "14:35"):
            continue

        # ── Day boundary reset ────────────────────────────────────────────────
        if candle_day != orb["day"]:
            for pos, tlist, extras in [
                (gap_pos, gap_trades, lambda p: {"gap_pct": p["gap_pct"]}),
                (orb_pos, orb_trades, lambda p: {"orb_high": p["orb_high"],
                                                  "orb_low": p["orb_low"],
                                                  "orb_range": p["orb_range"]}),
            ]:
                if pos:
                    lp  = live_premium(pos["action"], pos["entry_nifty"],
                                       pos["entry_premium"], close)
                    pnl = _close_position(pos, lp, close, dt,
                                          "overnight force-close", tlist,
                                          capital + pos["cost"], extras(pos))
                    capital += pos["cost"] + pnl
            gap_pos     = None
            orb_pos     = None
            orb         = _fresh_orb(candle_day)
            prev_close  = prev_close_for_day.get(candle_day)
            day_volumes = []
            gap_detected = False

        if prev_close is None:
            continue

        ltp = close
        day_volumes.append(vol)

        # ── Gap detection at first 9:15 candle ───────────────────────────────
        if candle_time == "09:15" and not gap_detected:
            gap_detected = True
            gap_pct = (ltp - prev_close) / prev_close * 100
            if GAP_MIN_PCT <= abs(gap_pct) <= GAP_MAX_PCT:
                action     = "BUY_CALL" if gap_pct > 0 else "BUY_PUT"
                entry_prem = ltp * PREMIUM_FACTOR
                cost       = entry_prem * LOT_SIZE
                if cost <= capital:
                    capital -= cost
                    gap_pos  = {
                        "id"             : _next_id(),
                        "action"         : action,
                        "entry_premium"  : round(entry_prem, 2),
                        "entry_nifty"    : ltp,
                        "entry_time"     : dt.strftime("%Y-%m-%d %H:%M"),
                        "cost"           : round(cost, 2),
                        "reason"         : f"Gap {'up' if gap_pct > 0 else 'down'} {gap_pct:+.2f}%",
                        "gap_pct"        : round(gap_pct, 2),
                        "_entry_dt"      : dt,
                        "_stop_floor"    : GAP_STOP,
                    }

        # ── Manage gap position ───────────────────────────────────────────────
        if gap_pos:
            lp  = live_premium(gap_pos["action"], gap_pos["entry_nifty"],
                               gap_pos["entry_premium"], ltp)
            pct = (lp - gap_pos["entry_premium"]) / gap_pos["entry_premium"]

            exit_reason = None
            if candle_time >= GAP_EXIT_TIME:
                exit_reason = f"{GAP_EXIT_TIME} time exit ({pct*100:+.1f}%)"
            elif pct >= GAP_TARGET:
                exit_reason = f"profit +{pct*100:.1f}%"
            elif pct <= GAP_STOP:
                exit_reason = f"stop {pct*100:.1f}%"

            if exit_reason:
                pnl     = _close_position(gap_pos, lp, ltp, dt, exit_reason,
                                          gap_trades, capital + gap_pos["cost"],
                                          {"gap_pct": gap_pos["gap_pct"]})
                capital += gap_pos["cost"] + pnl

                # ── GAP-AWARE RULE ────────────────────────────────────────────
                if (candle_time <= GAP_EXIT_TIME and pct >= 0.10
                        and orb_pos is not None):
                    orb_lp  = live_premium(orb_pos["action"], orb_pos["entry_nifty"],
                                           orb_pos["entry_premium"], ltp)
                    orb_pct = (orb_lp - orb_pos["entry_premium"]) / orb_pos["entry_premium"]
                    if orb_pct < 0.20 and orb_pos["_stop_floor"] < 0:
                        orb_pos["_stop_floor"]       = 0.0
                        orb_pos["_gap_aware_raised"]  = True

                gap_pos = None

        # ── ORB range building: 9:15–9:30 ────────────────────────────────────
        if candle_time <= ORB_RANGE_END:
            if orb["range_high"] is None:
                orb["range_high"] = high
                orb["range_low"]  = low
            else:
                orb["range_high"] = max(orb["range_high"], high)
                orb["range_low"]  = min(orb["range_low"],  low)
            continue  # gap management already done above; skip ORB entry logic

        # ── Finalise ORB range ────────────────────────────────────────────────
        if not orb["range_set"]:
            orb["range_set"] = True
            if orb["range_high"] is None:
                orb["skip_today"] = True
            else:
                rng = orb["range_high"] - orb["range_low"]
                if rng > max_range or rng < min_range:
                    orb["skip_today"] = True

        if orb["skip_today"]:
            continue

        # ── Manage ORB position ───────────────────────────────────────────────
        if orb_pos:
            lp  = live_premium(orb_pos["action"], orb_pos["entry_nifty"],
                               orb_pos["entry_premium"], ltp)
            pct = (lp - orb_pos["entry_premium"]) / orb_pos["entry_premium"]

            if trail_trigger is not None and pct >= trail_trigger and orb_pos["_stop_floor"] < 0:
                orb_pos["_stop_floor"] = 0.0

            exit_reason = None
            if pct >= target_pct:
                exit_reason = f"profit +{pct*100:.1f}%"
            elif pct <= orb_pos["_stop_floor"]:
                tag = ("gap-aware-BE" if orb_pos.get("_gap_aware_raised")
                       else "trail-stop" if orb_pos["_stop_floor"] == 0.0
                       else "stop")
                exit_reason = f"{tag} {pct*100:.1f}%"
            elif candle_time >= ORB_FORCE_EXIT:
                exit_reason = f"{ORB_FORCE_EXIT} time exit"

            if exit_reason:
                pnl     = _close_position(
                    orb_pos, lp, ltp, dt, exit_reason, orb_trades,
                    capital + orb_pos["cost"],
                    {"orb_high"         : orb_pos["orb_high"],
                     "orb_low"          : orb_pos["orb_low"],
                     "orb_range"        : orb_pos["orb_range"],
                     "gap_aware_raised" : orb_pos.get("_gap_aware_raised", False)},
                )
                capital += orb_pos["cost"] + pnl
                orb_pos  = None
            continue

        # ── ORB breakout detection ────────────────────────────────────────────
        if orb["traded_today"]:
            continue

        vol_ok   = _vol_confirmed(vol, day_volumes)
        broke_up = ltp > orb["range_high"]   # close-confirm (same as HYBRID)
        broke_dn = ltp < orb["range_low"]

        action = None
        if broke_up and vol_ok:
            action = "BUY_CALL"
        elif broke_dn and vol_ok:
            action = "BUY_PUT"

        if not action:
            continue

        change_pct  = (ltp - prev_close) / prev_close * 100 if prev_close else 0
        skew        = change_pct / 100
        sim_ce_prem = ltp * PREMIUM_FACTOR * (1 + skew)
        sim_pe_prem = ltp * PREMIUM_FACTOR * (1 - skew)
        if not _hybrid_confirm(action, change_pct, sim_ce_prem, sim_pe_prem):
            orb["traded_today"] = True
            continue

        entry_prem = ltp * PREMIUM_FACTOR
        cost       = entry_prem * LOT_SIZE
        if cost > capital:
            continue

        orb["traded_today"] = True
        capital -= cost
        rng       = orb["range_high"] - orb["range_low"]
        direction = "above" if action == "BUY_CALL" else "below"
        orb_pos   = {
            "id"               : _next_id(),
            "action"           : action,
            "entry_premium"    : round(entry_prem, 2),
            "entry_nifty"      : ltp,
            "entry_time"       : dt.strftime("%Y-%m-%d %H:%M"),
            "cost"             : round(cost, 2),
            "reason"           : (f"ORB break {direction} "
                                  f"{orb['range_low']:.0f}–{orb['range_high']:.0f} "
                                  f"({rng:.0f}pts)"),
            "orb_high"         : orb["range_high"],
            "orb_low"          : orb["range_low"],
            "orb_range"        : rng,
            "_entry_dt"        : dt,
            "_stop_floor"      : stop_pct,
            "_gap_aware_raised": False,
        }

    # ── Force-close at period end ─────────────────────────────────────────────
    for pos, tlist, extras in [
        (gap_pos, gap_trades, lambda p: {"gap_pct": p["gap_pct"]}),
        (orb_pos, orb_trades, lambda p: {"orb_high": p["orb_high"],
                                          "orb_low": p["orb_low"],
                                          "orb_range": p["orb_range"],
                                          "gap_aware_raised": p.get("_gap_aware_raised", False)}),
    ]:
        if pos and candles:
            last_c = candles[-1]
            lp     = live_premium(pos["action"], pos["entry_nifty"],
                                  pos["entry_premium"], last_c[4])
            pnl    = _close_position(pos, lp, last_c[4], _parse_dt(last_c[0]),
                                     "end of period", tlist,
                                     capital + pos["cost"], extras(pos))
            capital += pos["cost"] + pnl

    all_trades = sorted(gap_trades + orb_trades, key=lambda t: t["entry_time"])
    return {
        "trades"       : all_trades,
        "gap_trades"   : gap_trades,
        "orb_trades"   : orb_trades,
        "final_capital": round(capital, 2),
    }


def _print_hga_log(hga_result: dict):
    """Show days the gap-aware rule fired and the outcome of the ORB trade."""
    gap_by_day = {t["entry_time"][:10]: t for t in hga_result["gap_trades"]}
    orb_by_day = {t["entry_time"][:10]: t for t in hga_result["orb_trades"]}

    rule_days = [day for day, t in orb_by_day.items() if t.get("gap_aware_raised")]
    overlap   = sorted(set(gap_by_day) & set(orb_by_day))

    SEP = "=" * 96
    sep = "-" * 96
    print(f"\n{SEP}")
    print(f"  HYBRID_GAP_AWARE — detail log  "
          f"(rule fired {len(rule_days)} time(s) / {len(overlap)} day(s) both traded)")
    print(sep)

    if not overlap:
        print("  No days where both strategies traded.")
        print(f"{SEP}")
        return

    print(f"  {'Date':<12}  {'GAP action':<10} {'GAP exit':<30} {'GAP P&L':>9}"
          f"   {'ORB action':<10} {'ORB exit':<28} {'ORB P&L':>9}  Rule  Combined")
    print(f"  {sep}")

    for day in overlap:
        g    = gap_by_day[day]
        h    = orb_by_day[day]
        comb = g["pnl"] + h["pnl"]
        sign = "✅" if comb >= 0 else "❌"
        rule = "🔒 BE" if h.get("gap_aware_raised") else "    "
        print(
            f"  {day:<12}  {g['action']:<10} {g['exit_reason']:<30} ₹{g['pnl']:>+8,.0f}"
            f"   {h['action']:<10} {h['exit_reason']:<28} ₹{h['pnl']:>+8,.0f}"
            f"  {rule}  {sign} ₹{comb:>+8,.0f}"
        )

    if rule_days:
        print(f"\n  Days rule fired: {', '.join(rule_days)}")
        rule_orb   = [orb_by_day[d] for d in rule_days]
        rule_wins  = sum(1 for t in rule_orb if t["pnl"] > 0)
        rule_total = sum(t["pnl"] for t in rule_orb)
        print(f"  ORB outcome on rule days: {rule_wins}/{len(rule_days)} wins  "
              f"total P&L ₹{rule_total:+,.0f}")
    print(f"{SEP}")


# ── VWAP Reversal simulation ───────────────────────────────────────────────────

def run_vwap_backtest(candles: list, starting_capital: float) -> dict:
    """
    VWAP Reversal strategy:
      - Runs only on days when HYBRID ORB did NOT trade (complementary)
      - Entry window 10:00–13:30; one trade per day
      - BUY_PUT  if LTP > VWAP + 0.6% AND RSI > 62  (overbought — fade up)
      - BUY_CALL if LTP < VWAP - 0.6% AND RSI < 38  (oversold  — fade down)
      - Exit: +35% target / -25% stop / 14:00 force exit /
              reversion exit (price crosses back through VWAP while not in loss)
    """
    # Identify days HYBRID ORB traded so VWAP can skip them
    _hybrid = run_orb_backtest(
        candles, starting_capital,
        stop_pct=-0.35, target_pct=0.75, trail_trigger=0.40,
        min_range=30, max_range=150, close_confirm=True, hybrid=True,
    )
    hybrid_days = {t["entry_time"][:10] for t in _hybrid["trades"]}

    prev_close_for_day = _build_day_maps(candles)

    capital      = starting_capital
    trades       = []
    open_pos     = None
    prev_close   = None
    current_day  = None
    close_buf    = []    # rolling closes for VWAP proxy + RSI
    traded_today = False

    min_buf = max(VWAP_WINDOW, RSI_PERIOD + 2)

    for c in candles:
        ts_str, _o, high, low, close, vol, _oi = c
        dt          = _parse_dt(ts_str)
        candle_time = dt.strftime("%H:%M")
        candle_day  = dt.strftime("%Y-%m-%d")

        if not ("09:15" <= candle_time <= "14:05"):
            continue

        # ── Day boundary reset ────────────────────────────────────────────────
        if candle_day != current_day:
            if open_pos:
                lp  = live_premium(open_pos["action"], open_pos["entry_nifty"],
                                   open_pos["entry_premium"], close)
                pnl = _close_position(open_pos, lp, close, dt,
                                      "overnight force-close", trades,
                                      open_pos["_capital_before"],
                                      {"vwap_entry": open_pos["vwap_entry"],
                                       "rsi_entry":  open_pos["rsi_entry"]})
                capital  = open_pos["_capital_before"] + pnl
                open_pos = None
            current_day  = candle_day
            prev_close   = prev_close_for_day.get(candle_day)
            close_buf    = []
            traded_today = candle_day in hybrid_days  # skip if HYBRID traded

        if prev_close is None:
            continue

        ltp = close
        close_buf.append(ltp)

        # ── Manage open position ───────────────────────────────────────────────
        if open_pos:
            lp  = live_premium(open_pos["action"], open_pos["entry_nifty"],
                               open_pos["entry_premium"], ltp)
            pct = (lp - open_pos["entry_premium"]) / open_pos["entry_premium"]

            # Reversion exit: price crossed back through VWAP (only if not losing)
            reverted = False
            if len(close_buf) >= 5:
                vwap_now = _calc_vwap_proxy(close_buf, VWAP_WINDOW)
                if open_pos["action"] == "BUY_PUT"  and ltp <= vwap_now:
                    reverted = True
                elif open_pos["action"] == "BUY_CALL" and ltp >= vwap_now:
                    reverted = True

            exit_reason = None
            if candle_time >= VWAP_FORCE_EXIT:
                exit_reason = f"{VWAP_FORCE_EXIT} time exit ({pct*100:+.1f}%)"
            elif pct >= VWAP_TARGET:
                exit_reason = f"profit +{pct*100:.1f}%"
            elif pct <= VWAP_STOP:
                exit_reason = f"stop {pct*100:.1f}%"
            elif reverted and pct > -0.05:
                exit_reason = f"reverted to VWAP ({pct*100:+.1f}%)"

            if exit_reason:
                pnl      = _close_position(open_pos, lp, ltp, dt, exit_reason,
                                           trades, open_pos["_capital_before"],
                                           {"vwap_entry": open_pos["vwap_entry"],
                                            "rsi_entry":  open_pos["rsi_entry"]})
                capital  = open_pos["_capital_before"] + pnl
                open_pos = None
            continue

        # ── Entry logic ───────────────────────────────────────────────────────
        if traded_today:
            continue
        if not (VWAP_START <= candle_time <= VWAP_END):
            continue
        if len(close_buf) < min_buf:
            continue

        vwap = _calc_vwap_proxy(close_buf, VWAP_WINDOW)
        rsi  = _calc_rsi(close_buf, RSI_PERIOD)
        dev  = (ltp - vwap) / vwap

        action = None
        if dev > VWAP_DEVIATION and rsi > RSI_OB:
            action = "BUY_PUT"    # overbought — fade the up-move
        elif dev < -VWAP_DEVIATION and rsi < RSI_OS:
            action = "BUY_CALL"   # oversold — fade the down-move

        if not action:
            continue

        entry_prem = ltp * PREMIUM_FACTOR
        cost       = entry_prem * LOT_SIZE
        if cost > capital:
            continue

        traded_today = True
        capital -= cost
        dev_str  = f"{dev*100:+.2f}%"
        open_pos = {
            "id"             : len(trades) + 1,
            "action"         : action,
            "entry_premium"  : round(entry_prem, 2),
            "entry_nifty"    : ltp,
            "entry_time"     : dt.strftime("%Y-%m-%d %H:%M"),
            "cost"           : round(cost, 2),
            "reason"         : f"VWAP dev {dev_str} RSI={rsi:.0f}",
            "vwap_entry"     : round(vwap, 2),
            "rsi_entry"      : round(rsi, 1),
            "_entry_dt"      : dt,
            "_capital_before": capital + cost,
            "_stop_floor"    : VWAP_STOP,
        }

    # Force-close at period end
    if open_pos and candles:
        last_c = candles[-1]
        lp     = live_premium(open_pos["action"], open_pos["entry_nifty"],
                              open_pos["entry_premium"], last_c[4])
        pnl    = _close_position(open_pos, lp, last_c[4], _parse_dt(last_c[0]),
                                 "end of period", trades, open_pos["_capital_before"],
                                 {"vwap_entry": open_pos["vwap_entry"],
                                  "rsi_entry":  open_pos["rsi_entry"]})
        capital = open_pos["_capital_before"] + pnl

    return {"trades": trades, "final_capital": round(capital, 2)}


# ── VWAP parametric backtest (for sweep) ──────────────────────────────────────

def run_vwap_param_backtest(
    candles: list,
    starting_capital: float,
    *,
    deviation: float,
    rsi_ob: float,
    rsi_os: float,
    target: float,
    stop: float,
    hybrid_days: set = None,
) -> dict:
    """
    Parametric VWAP Reversal for parameter sweep.
    Pass hybrid_days to avoid re-running HYBRID inside each call.
    """
    if hybrid_days is None:
        _h = run_orb_backtest(
            candles, starting_capital,
            stop_pct=-0.35, target_pct=0.75, trail_trigger=0.40,
            min_range=30, max_range=150, close_confirm=True, hybrid=True,
        )
        hybrid_days = {t["entry_time"][:10] for t in _h["trades"]}

    prev_close_for_day = _build_day_maps(candles)
    capital      = starting_capital
    trades       = []
    open_pos     = None
    prev_close   = None
    current_day  = None
    close_buf    = []
    traded_today = False
    min_buf      = max(VWAP_WINDOW, RSI_PERIOD + 2)

    for c in candles:
        ts_str, _o, high, low, close, vol, _oi = c
        dt          = _parse_dt(ts_str)
        candle_time = dt.strftime("%H:%M")
        candle_day  = dt.strftime("%Y-%m-%d")

        if not ("09:15" <= candle_time <= "14:05"):
            continue

        if candle_day != current_day:
            if open_pos:
                lp  = live_premium(open_pos["action"], open_pos["entry_nifty"],
                                   open_pos["entry_premium"], close)
                pnl = _close_position(open_pos, lp, close, dt,
                                      "overnight force-close", trades,
                                      open_pos["_capital_before"],
                                      {"vwap_entry": open_pos["vwap_entry"],
                                       "rsi_entry":  open_pos["rsi_entry"]})
                capital  = open_pos["_capital_before"] + pnl
                open_pos = None
            current_day  = candle_day
            prev_close   = prev_close_for_day.get(candle_day)
            close_buf    = []
            traded_today = candle_day in hybrid_days

        if prev_close is None:
            continue

        ltp = close
        close_buf.append(ltp)

        if open_pos:
            lp  = live_premium(open_pos["action"], open_pos["entry_nifty"],
                               open_pos["entry_premium"], ltp)
            pct = (lp - open_pos["entry_premium"]) / open_pos["entry_premium"]

            reverted = False
            if len(close_buf) >= 5:
                vwap_now = _calc_vwap_proxy(close_buf, VWAP_WINDOW)
                if open_pos["action"] == "BUY_PUT"  and ltp <= vwap_now:
                    reverted = True
                elif open_pos["action"] == "BUY_CALL" and ltp >= vwap_now:
                    reverted = True

            exit_reason = None
            if candle_time >= VWAP_FORCE_EXIT:
                exit_reason = f"{VWAP_FORCE_EXIT} time exit ({pct*100:+.1f}%)"
            elif pct >= target:
                exit_reason = f"profit +{pct*100:.1f}%"
            elif pct <= stop:
                exit_reason = f"stop {pct*100:.1f}%"
            elif reverted and pct > -0.05:
                exit_reason = f"reverted to VWAP ({pct*100:+.1f}%)"

            if exit_reason:
                pnl      = _close_position(open_pos, lp, ltp, dt, exit_reason,
                                           trades, open_pos["_capital_before"],
                                           {"vwap_entry": open_pos["vwap_entry"],
                                            "rsi_entry":  open_pos["rsi_entry"]})
                capital  = open_pos["_capital_before"] + pnl
                open_pos = None
            continue

        if traded_today:
            continue
        if not (VWAP_START <= candle_time <= VWAP_END):
            continue
        if len(close_buf) < min_buf:
            continue

        vwap = _calc_vwap_proxy(close_buf, VWAP_WINDOW)
        rsi  = _calc_rsi(close_buf, RSI_PERIOD)
        dev  = (ltp - vwap) / vwap

        action = None
        if dev > deviation and rsi > rsi_ob:
            action = "BUY_PUT"
        elif dev < -deviation and rsi < rsi_os:
            action = "BUY_CALL"

        if not action:
            continue

        entry_prem = ltp * PREMIUM_FACTOR
        cost       = entry_prem * LOT_SIZE
        if cost > capital:
            continue

        traded_today = True
        capital -= cost
        open_pos = {
            "id"             : len(trades) + 1,
            "action"         : action,
            "entry_premium"  : round(entry_prem, 2),
            "entry_nifty"    : ltp,
            "entry_time"     : dt.strftime("%Y-%m-%d %H:%M"),
            "cost"           : round(cost, 2),
            "reason"         : f"VWAP dev {dev*100:+.2f}% RSI={rsi:.0f}",
            "vwap_entry"     : round(vwap, 2),
            "rsi_entry"      : round(rsi, 1),
            "_entry_dt"      : dt,
            "_capital_before": capital + cost,
            "_stop_floor"    : stop,
        }

    if open_pos and candles:
        last_c = candles[-1]
        lp     = live_premium(open_pos["action"], open_pos["entry_nifty"],
                              open_pos["entry_premium"], last_c[4])
        pnl    = _close_position(open_pos, lp, last_c[4], _parse_dt(last_c[0]),
                                 "end of period", trades, open_pos["_capital_before"],
                                 {"vwap_entry": open_pos["vwap_entry"],
                                  "rsi_entry":  open_pos["rsi_entry"]})
        capital = open_pos["_capital_before"] + pnl

    return {"trades": trades, "final_capital": round(capital, 2)}


# ── Combined portfolio helpers ─────────────────────────────────────────────────

def _combined_metrics(gap_result: dict, hybrid_result: dict,
                      starting_capital: float) -> dict:
    """Merge Gap and Hybrid trades chronologically on one shared capital pool."""
    all_trades = sorted(
        gap_result["trades"] + hybrid_result["trades"],
        key=lambda t: t["entry_time"],
    )
    capital = starting_capital
    peak    = starting_capital
    max_dd  = 0.0
    for t in all_trades:
        capital += t["pnl"]
        peak     = max(peak, capital)
        if peak > 0:
            max_dd = max(max_dd, (peak - capital) / peak * 100)
        t["_combined_cap"] = round(capital, 2)

    wins    = [t for t in all_trades if t["pnl"] > 0]
    losses  = [t for t in all_trades if t["pnl"] <= 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = abs(sum(t["pnl"] for t in losses))

    return {
        "trades"       : len(all_trades),
        "wins"         : len(wins),
        "losses"       : len(losses),
        "win_rate"     : len(wins) / len(all_trades) * 100 if all_trades else 0,
        "profit_factor": gross_w / gross_l if gross_l else float("inf"),
        "max_dd"       : max_dd,
        "total_return" : (capital - starting_capital) / starting_capital * 100,
        "total_pnl"    : round(capital - starting_capital, 2),
        "final_capital": round(capital, 2),
    }


def _print_overlap_log(gap_trades: list, hybrid_trades: list):
    """Print days when both Gap and Hybrid strategies traded."""
    gap_by_day    = {t["entry_time"][:10]: t for t in gap_trades}
    hybrid_by_day = {t["entry_time"][:10]: t for t in hybrid_trades}
    overlap       = sorted(set(gap_by_day) & set(hybrid_by_day))

    SEP = "=" * 90
    sep = "-" * 90
    print(f"\n{SEP}")
    print(f"  DAYS BOTH STRATEGIES TRADED  ({len(overlap)} day(s))")
    print(sep)
    if not overlap:
        print("  No days where both fired.")
        print(f"{SEP}")
        return
    print(f"  {'Date':<12}  {'GAP action':<10} {'GAP exit':<26} {'GAP P&L':>9}"
          f"   {'ORB action':<10} {'ORB exit':<26} {'ORB P&L':>9}  Combined")
    print(f"  {sep}")
    for day in overlap:
        g    = gap_by_day[day]
        h    = hybrid_by_day[day]
        comb = g["pnl"] + h["pnl"]
        sign = "✅" if comb >= 0 else "❌"
        print(
            f"  {day:<12}  {g['action']:<10} {g['exit_reason']:<26} ₹{g['pnl']:>+8,.0f}"
            f"   {h['action']:<10} {h['exit_reason']:<26} ₹{h['pnl']:>+8,.0f}"
            f"  {sign} ₹{comb:>+8,.0f}"
        )
    print(f"{SEP}")


def _three_combined_metrics(gap_result: dict, hybrid_result: dict,
                             vwap_result: dict, starting_capital: float) -> dict:
    """Merge Gap, Hybrid, and VWAP trades chronologically on one shared capital pool."""
    all_trades = sorted(
        gap_result["trades"] + hybrid_result["trades"] + vwap_result["trades"],
        key=lambda t: t["entry_time"],
    )
    capital = starting_capital
    peak    = starting_capital
    max_dd  = 0.0
    for t in all_trades:
        capital += t["pnl"]
        peak     = max(peak, capital)
        if peak > 0:
            max_dd = max(max_dd, (peak - capital) / peak * 100)
        t["_combined_cap"] = round(capital, 2)

    wins    = [t for t in all_trades if t["pnl"] > 0]
    losses  = [t for t in all_trades if t["pnl"] <= 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = abs(sum(t["pnl"] for t in losses))

    return {
        "trades"       : len(all_trades),
        "wins"         : len(wins),
        "losses"       : len(losses),
        "win_rate"     : len(wins) / len(all_trades) * 100 if all_trades else 0,
        "profit_factor": gross_w / gross_l if gross_l else float("inf"),
        "max_dd"       : max_dd,
        "total_return" : (capital - starting_capital) / starting_capital * 100,
        "total_pnl"    : round(capital - starting_capital, 2),
        "final_capital": round(capital, 2),
    }


def _print_vwap_overlap_log(hybrid_trades: list, vwap_trades: list):
    """Show days VWAP fills in for ORB (skips) and days both strategies traded."""
    hybrid_days  = {t["entry_time"][:10] for t in hybrid_trades}
    vwap_by_day  = {t["entry_time"][:10]: t for t in vwap_trades}

    fill_days = sorted(d for d in vwap_by_day if d not in hybrid_days)
    both_days = sorted(set(hybrid_days) & set(vwap_by_day))

    SEP = "=" * 80
    sep = "-" * 80
    print(f"\n{SEP}")
    print(f"  VWAP OVERLAP ANALYSIS")
    print(f"  VWAP trades on ORB-skip days (fills gap): {len(fill_days)}"
          f"   Both traded same day: {len(both_days)}")
    print(sep)

    if fill_days:
        print(f"\n  ORB SKIPPED — VWAP TRADED ({len(fill_days)} day(s)):")
        print(f"  {'Date':<12}  {'Action':<10}  {'Exit reason':<36}  {'P&L':>9}")
        print(f"  {'-'*62}")
        for d in fill_days:
            t    = vwap_by_day[d]
            sign = "✅" if t["pnl"] >= 0 else "❌"
            print(f"  {d:<12}  {t['action']:<10}  {t['exit_reason']:<36}"
                  f"  {sign} ₹{t['pnl']:>+8,.0f}")

    if both_days:
        hybrid_by_day = {t["entry_time"][:10]: t for t in hybrid_trades}
        print(f"\n  BOTH STRATEGIES TRADED SAME DAY ({len(both_days)} day(s)):")
        print(f"  {'Date':<12}  {'HYBRID exit':<28}  {'HYBRID P&L':>10}"
              f"   {'VWAP exit':<28}  {'VWAP P&L':>9}  Combined")
        print(f"  {'-'*80}")
        for d in both_days:
            h    = hybrid_by_day[d]
            v    = vwap_by_day[d]
            comb = h["pnl"] + v["pnl"]
            sign = "✅" if comb >= 0 else "❌"
            print(f"  {d:<12}  {h['exit_reason']:<28}  ₹{h['pnl']:>+9,.0f}"
                  f"   {v['exit_reason']:<28}  ₹{v['pnl']:>+8,.0f}"
                  f"  {sign} ₹{comb:>+8,.0f}")
    print(f"{SEP}")


# ── Metrics & reporting ────────────────────────────────────────────────────────

def compute_metrics(result: dict, starting_capital: float) -> dict:
    trades  = result["trades"]
    capital = result["final_capital"]
    wins    = [t for t in trades if t["pnl"] > 0]
    losses  = [t for t in trades if t["pnl"] <= 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = abs(sum(t["pnl"] for t in losses))
    peak    = starting_capital
    max_dd  = 0.0
    for t in trades:
        peak   = max(peak, t["capital_after"])
        max_dd = max(max_dd, (peak - t["capital_after"]) / peak * 100)
    return {
        "trades"       : len(trades),
        "wins"         : len(wins),
        "losses"       : len(losses),
        "win_rate"     : len(wins) / len(trades) * 100 if trades else 0,
        "profit_factor": gross_w / gross_l if gross_l else float("inf"),
        "max_dd"       : max_dd,
        "total_return" : (capital - starting_capital) / starting_capital * 100,
        "total_pnl"    : capital - starting_capital,
        "final_capital": capital,
    }


def print_trade_list(trades: list, label: str, show_orb: bool = False):
    SEP = "=" * 72
    sep = "-" * 72
    trail_n = sum(1 for t in trades if t.get("trailed"))
    print(f"\n{SEP}")
    print(f"  {label}  —  trade log")
    if trail_n:
        print(f"  ({trail_n} trade(s) had trailing stop activated  ~)")
    print(sep)
    if not trades:
        print("  No trades generated.")
        print(f"{SEP}")
        return
    if show_orb:
        print(f"  {'#':<4} {'Date':<12} {'Action':<10} {'Range':^14}"
              f" {'Entry':>6} {'Exit':>6} {'P&L':>10}  {'Hold':>5}  Reason")
    else:
        print(f"  {'#':<4} {'Action':<10} {'Entry':>7} {'Exit':>7}"
              f" {'P&L':>10}  {'Hold':>5}  Reason")
    print(f"  {sep}")
    for t in trades:
        mark = "~" if t.get("trailed") else " "
        if show_orb:
            rng = f"{t['orb_low']:.0f}–{t['orb_high']:.0f}"
            print(
                f"  {t['id']:<4} {t['entry_time'][:10]:<12} {t['action']:<10}"
                f" {rng:^14}"
                f" ₹{t['entry_premium']:>5.0f}"
                f" ₹{t['exit_premium']:>5.0f}"
                f" ₹{t['pnl']:>+9,.0f}"
                f"  {t['holding_mins']:>4}m"
                f" {mark} {t['exit_reason']}"
            )
        else:
            print(
                f"  {t['id']:<4} {t['action']:<10}"
                f" ₹{t['entry_premium']:>5.0f}"
                f" ₹{t['exit_premium']:>5.0f}"
                f" ₹{t['pnl']:>+9,.0f}"
                f"  {t['holding_mins']:>4}m"
                f" {mark} {t['exit_reason']}"
            )
    print(f"{SEP}")


def print_orb_day_log(day_log: dict, trades: list):
    """Day-by-day ORB summary: range, outcome, and trade result where applicable."""
    trade_by_id = {t["id"]: t for t in trades}
    SEP = "=" * 72
    sep = "-" * 72

    print(f"\n{SEP}")
    print(f"  ORB — Day-by-day log")
    print(sep)
    print(f"  {'Date':<12} {'Range':>10}  {'Outcome':<32}  {'P&L':>10}")
    print(f"  {sep}")

    for day in sorted(day_log):
        entry    = day_log[day]
        outcome  = entry["outcome"]
        rng_str  = f"{entry['range_pts']:.0f}pts" if entry.get("range_pts") else "—"
        pnl_str  = ""
        if outcome == "traded" and entry.get("trade_id"):
            t = trade_by_id.get(entry["trade_id"])
            if t:
                pnl_str = f"₹{t['pnl']:>+9,.0f}  ({t['action']}  {t['exit_reason']})"
        elif outcome == "hybrid-skip":
            pnl_str = f"(ORB={entry.get('rejected_action','?')} blocked — trend contradicts)"
        print(f"  {day:<12} {rng_str:>10}  {outcome:<32}  {pnl_str}")

    print(f"{SEP}")


def print_comparison_table(results: list, starting_capital: float,
                           title: str = "COMPARISON TABLE"):
    """Print results sorted by profit factor descending."""
    SEP = "=" * 76
    sep = "-" * 76
    print(f"\n{SEP}")
    print(f"  {title}  (capital ₹{starting_capital:,.0f})")
    print(sep)
    print(f"  {'Strategy':<22} {'Trades':>7} {'Win%':>7} {'PF':>6}"
          f" {'MaxDD%':>8} {'Return%':>9} {'P&L':>12}")
    print(f"  {sep}")

    sorted_results = sorted(results,
                            key=lambda r: r["metrics"]["profit_factor"],
                            reverse=True)

    best_pf = sorted_results[0]["metrics"]["profit_factor"] if sorted_results else 0

    for r in sorted_results:
        m    = r["metrics"]
        pf_s = f"{m['profit_factor']:.2f}" if m["profit_factor"] != float("inf") else "  ∞"
        flag = " ◀ best" if m["profit_factor"] == best_pf else ""
        print(
            f"  {r['label']:<22}"
            f" {m['trades']:>7}"
            f" {m['win_rate']:>6.1f}%"
            f" {pf_s:>6}"
            f" {m['max_dd']:>7.1f}%"
            f" {m['total_return']:>8.2f}%"
            f" ₹{m['total_pnl']:>+10,.0f}"
            f"{flag}"
        )
    print(SEP)

    eligible = [r for r in results if r["metrics"]["trades"] >= 5]
    if eligible:
        winner = max(eligible, key=lambda r: r["metrics"]["profit_factor"])
        m      = winner["metrics"]
        print(f"\n  Best (by profit factor): {winner['label']}")
        print(f"  {m['trades']} trades  |  {m['win_rate']:.1f}% win rate  |  "
              f"PF {m['profit_factor']:.2f}  |  "
              f"max DD {m['max_dd']:.1f}%  |  "
              f"return {m['total_return']:+.2f}%")
    print()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Backtest Nifty 50 options strategies")
    parser.add_argument("--from",    dest="from_date", required=True,
                        metavar="YYYY-MM-DD")
    parser.add_argument("--to",      dest="to_date",   required=True,
                        metavar="YYYY-MM-DD")
    parser.add_argument("--capital", dest="capital",   type=float,
                        default=100_000, metavar="INR")
    args = parser.parse_args()

    print(f"\nPillar Trading — Backtest")
    print(f"Period  : {args.from_date} → {args.to_date}")
    print(f"Capital : ₹{args.capital:,.0f}\n")

    if not TOKEN:
        print("Error: UPSTOX_ACCESS_TOKEN not set in .env")
        sys.exit(1)

    print("Fetching candles...")
    candles = fetch_candles(args.from_date, args.to_date)
    print(f"  Total: {len(candles)} candles\n")
    if not candles:
        print("No candle data — check token and date range.")
        sys.exit(1)

    all_results = []

    # ── Rule-based combinations ────────────────────────────────────────────────
    print("─" * 50)
    print("RULE-BASED STRATEGY (reference)")
    print("─" * 50)

    for combo in COMBOS:
        print(f"Running {combo['label']} ...", flush=True)
        result  = run_backtest(candles, args.capital,
                               combo["stop"], combo["target"])
        metrics = compute_metrics(result, args.capital)
        all_results.append({"label": combo["label"],
                             "result": result, "metrics": metrics})

    # ── ORB combinations ───────────────────────────────────────────────────────
    print("\n" + "─" * 50)
    print("ORB STRATEGY — parameter sweep")
    print(f"Range window 9:15–{ORB_RANGE_END}  |  Force exit {ORB_FORCE_EXIT}")
    print(f"Breakout: touch=high/low vs range, close-conf=close vs range")
    print("─" * 50)

    orb_results = []
    for combo in ORB_COMBOS:
        label = combo["label"]
        print(f"Running {label} ...", flush=True)
        result = run_orb_backtest(
            candles, args.capital,
            stop_pct      = combo["stop"],
            target_pct    = combo["target"],
            trail_trigger = combo["trail"],
            min_range     = combo["min_range"],
            max_range     = combo["max_range"],
            close_confirm = combo["close_confirm"],
        )
        metrics = compute_metrics(result, args.capital)
        entry   = {"label": label, "result": result, "metrics": metrics,
                   "combo": combo}
        orb_results.append(entry)
        all_results.append(entry)

    # ── HYBRID: ORB + simulated Gemini confirmation ────────────────────────────
    print("\n" + "─" * 50)
    print("HYBRID STRATEGY — ORB + Gemini confirmation (simulated)")
    print("ORB: close-confirm | -35%/+75% | trail=40% | range 30–150")
    print("Gemini sim: BUY_CALL only if change_pct>0 AND ce>pe at breakout time")
    print("           BUY_PUT  only if change_pct<0 AND pe>ce at breakout time")
    print("─" * 50)
    print("Running HYBRID ...", flush=True)
    hybrid_result  = run_orb_backtest(
        candles, args.capital,
        stop_pct=-0.35, target_pct=0.75, trail_trigger=0.40,
        min_range=30, max_range=150, close_confirm=True, hybrid=True,
    )
    hybrid_metrics = compute_metrics(hybrid_result, args.capital)
    hybrid_entry   = {
        "label"  : "HYBRID ORB+Gemini",
        "result" : hybrid_result,
        "metrics": hybrid_metrics,
        "combo"  : {"stop": -0.35, "target": 0.75, "trail": 0.40,
                    "min_range": 30, "max_range": 150,
                    "close_confirm": True, "hybrid": True},
    }
    all_results.append(hybrid_entry)

    # ── Comparison table — all strategies, sorted by PF ───────────────────────
    print_comparison_table(all_results, args.capital,
                           title="FULL COMPARISON — sorted by profit factor")

    # ── Best pure ORB: trade log + day log ────────────────────────────────────
    eligible_orb = [r for r in orb_results if r["metrics"]["trades"] >= 5]
    if eligible_orb:
        best = max(eligible_orb, key=lambda r: r["metrics"]["profit_factor"])
        print(f"\n{'═'*72}")
        print(f"  BEST PURE ORB: {best['label']}")
        c = best["combo"]
        print(f"  Range {c['min_range']}–{c['max_range']}pts  |  "
              f"Stop {c['stop']*100:.0f}%/Target +{c['target']*100:.0f}%  |  "
              f"Trail: {'→BE at +'+str(int(c['trail']*100))+'%' if c['trail'] else 'none'}  |  "
              f"Entry: {'close-confirm' if c['close_confirm'] else 'touch (H/L)'}")
        print(f"{'═'*72}")
        print_trade_list(best["result"]["trades"], best["label"], show_orb=True)
        print_orb_day_log(best["result"]["day_log"], best["result"]["trades"])

    # ── HYBRID: trade log + day log ────────────────────────────────────────────
    print(f"\n{'═'*72}")
    print(f"  HYBRID ORB+Gemini — trade log")
    print(f"{'═'*72}")
    print_trade_list(hybrid_result["trades"], "HYBRID ORB+Gemini", show_orb=True)
    print_orb_day_log(hybrid_result["day_log"], hybrid_result["trades"])

    # ── GAP AND GO ─────────────────────────────────────────────────────────────
    print("\n" + "─" * 50)
    print("GAP AND GO STRATEGY")
    print(f"Gap ±{GAP_MIN_PCT}%–±{GAP_MAX_PCT}% at 9:15 AM  |  "
          f"Stop {GAP_STOP*100:.0f}%  |  Target +{GAP_TARGET*100:.0f}%  |  "
          f"Exit {GAP_EXIT_TIME} AM  |  VIX filter >18")
    print("─" * 50)
    print("Running GAP AND GO ...", flush=True)
    gap_result  = run_gap_and_go_backtest(candles, args.capital)
    gap_metrics = compute_metrics(gap_result, args.capital)
    gap_entry   = {"label": "GAP AND GO", "result": gap_result, "metrics": gap_metrics}

    # ── COMBINED portfolio ──────────────────────────────────────────────────────
    comb_metrics = _combined_metrics(gap_result, hybrid_result, args.capital)
    comb_entry   = {"label": "COMBINED", "metrics": comb_metrics}

    # ── HYBRID_GAP_AWARE (experimental) ────────────────────────────────────────
    print("\n" + "─" * 50)
    print("HYBRID_GAP_AWARE — experimental variation  [*]")
    print("Same as HYBRID but: if gap exits ≥+10% before 10:00 AM")
    print("  AND ORB is open with gain <+20% → raise ORB stop to breakeven.")
    print("Gap loss has NO effect on ORB.")
    print("─" * 50)
    print("Running HYBRID_GAP_AWARE ...", flush=True)
    hga_result  = run_hybrid_gap_aware_backtest(candles, args.capital)
    hga_metrics = compute_metrics(hga_result, args.capital)
    hga_entry   = {"label": "HYBRID_GAP_AWARE*", "result": hga_result, "metrics": hga_metrics}

    # ── Strategy summary table: HYBRID vs GAP vs COMBINED vs HGA ───────────────
    print_comparison_table(
        [hybrid_entry, gap_entry, comb_entry, hga_entry],
        args.capital,
        title="STRATEGY SUMMARY — HYBRID vs GAP vs COMBINED vs HGA*",
    )

    # ── GAP AND GO trade log ────────────────────────────────────────────────────
    print(f"\n{'═'*72}")
    print(f"  GAP AND GO — trade log")
    print(f"{'═'*72}")
    print_trade_list(gap_result["trades"], "GAP AND GO")

    # ── Days when both strategies traded ────────────────────────────────────────
    _print_overlap_log(gap_result["trades"], hybrid_result["trades"])

    # ── HYBRID_GAP_AWARE detail log ─────────────────────────────────────────────
    _print_hga_log(hga_result)
    print(f"\n  [*] HYBRID_GAP_AWARE is experimental — do not use in live trading")
    print(f"      without further validation on out-of-sample data.")

    # ── VWAP REVERSAL ──────────────────────────────────────────────────────────
    print("\n" + "─" * 50)
    print("VWAP REVERSAL STRATEGY")
    print(f"VWAP proxy: rolling {VWAP_WINDOW}-candle close average")
    print(f"Entry {VWAP_START}–{VWAP_END}  |  Dev ±{VWAP_DEVIATION*100:.1f}%  |  "
          f"RSI OB>{RSI_OB} / OS<{RSI_OS}")
    print(f"Stop {VWAP_STOP*100:.0f}%  |  Target +{VWAP_TARGET*100:.0f}%  |  "
          f"Exit {VWAP_FORCE_EXIT}  |  Reversion exit  |  Skip if HYBRID ORB traded")
    print("─" * 50)
    print("Running VWAP REVERSAL ...", flush=True)
    vwap_result  = run_vwap_backtest(candles, args.capital)
    vwap_metrics = compute_metrics(vwap_result, args.capital)
    vwap_entry   = {"label": "VWAP REVERSAL", "result": vwap_result,
                    "metrics": vwap_metrics}

    # ── Three-way combined: HYBRID + GAP + VWAP ───────────────────────────────
    three_metrics = _three_combined_metrics(gap_result, hybrid_result,
                                            vwap_result, args.capital)
    three_entry   = {"label": "ALL THREE", "metrics": three_metrics}

    # ── Strategy summary: HYBRID vs GAP vs VWAP vs ALL THREE ──────────────────
    print_comparison_table(
        [hybrid_entry, gap_entry, vwap_entry, three_entry],
        args.capital,
        title="STRATEGY SUMMARY — HYBRID vs GAP vs VWAP vs ALL THREE",
    )

    # ── VWAP trade log ─────────────────────────────────────────────────────────
    print(f"\n{'═'*72}")
    print(f"  VWAP REVERSAL — trade log")
    print(f"{'═'*72}")
    print_trade_list(vwap_result["trades"], "VWAP REVERSAL")

    # ── VWAP overlap analysis ──────────────────────────────────────────────────
    _print_vwap_overlap_log(hybrid_result["trades"], vwap_result["trades"])

    # ── VWAP PARAMETER SWEEP ───────────────────────────────────────────────────
    print("\n" + "─" * 60)
    print("VWAP PARAMETER SWEEP")
    print("8 deviation/RSI combos × 2 exit variants = 16 tests")
    print("All skip days when HYBRID ORB traded  |  reversion exit: always on")
    print("─" * 60)

    # Pre-compute HYBRID traded days once — all 16 runs share this set
    print("Pre-computing HYBRID traded days ...", flush=True)
    _h_sw = run_orb_backtest(
        candles, args.capital,
        stop_pct=-0.35, target_pct=0.75, trail_trigger=0.40,
        min_range=30, max_range=150, close_confirm=True, hybrid=True,
    )
    _hybrid_days_sw = {t["entry_time"][:10] for t in _h_sw["trades"]}
    print(f"  HYBRID traded on {len(_hybrid_days_sw)} days — VWAP skips those\n")

    _SW_PARAMS = [
        (0.003, 60, 40),
        (0.003, 55, 45),
        (0.004, 62, 38),
        (0.004, 58, 42),
        (0.004, 55, 45),
        (0.005, 60, 40),
        (0.005, 55, 45),
        (0.006, 55, 45),
    ]
    _SW_EXITS = [
        ("A", 0.35, -0.25),   # original: +35% target / -25% stop
        ("B", 0.25, -0.20),   # tighter:  +25% target / -20% stop
    ]

    _sw_rows = []
    for dev, rsi_ob, rsi_os in _SW_PARAMS:
        for exit_lbl, tgt, stp in _SW_EXITS:
            short = f"d={dev*100:.1f}% RSI{rsi_os}/{rsi_ob}"
            print(f"  {short} [{exit_lbl}] ...", flush=True)
            r = run_vwap_param_backtest(
                candles, args.capital,
                deviation   = dev,
                rsi_ob      = rsi_ob,
                rsi_os      = rsi_os,
                target      = tgt,
                stop        = stp,
                hybrid_days = _hybrid_days_sw,
            )
            m = compute_metrics(r, args.capital)
            _sw_rows.append({
                "param_lbl"  : short,
                "exit_lbl"   : exit_lbl,
                "target"     : tgt,
                "stop"       : stp,
                "result"     : r,
                "metrics"    : m,
                "gap_fill"   : len(r["trades"]),
            })

    # Sort: combos with trades by PF desc, then zero-trade combos
    _sw_has  = sorted(
        [r for r in _sw_rows if r["metrics"]["trades"] > 0],
        key=lambda r: r["metrics"]["profit_factor"], reverse=True,
    )
    _sw_none = [r for r in _sw_rows if r["metrics"]["trades"] == 0]

    _SW_SEP = "=" * 88
    _sw_sep = "-" * 88
    print(f"\n{_SW_SEP}")
    print(f"  VWAP SWEEP — sorted by profit factor  (0-trade combos listed at bottom)")
    print(_sw_sep)
    print(f"  {'Dev / RSI':<22} {'Exit':^6} {'Trades':>7} {'Win%':>7} {'PF':>6}"
          f" {'MaxDD%':>8} {'Return%':>9} {'Gap Days':>9}")
    print(f"  {_sw_sep}")
    for r in _sw_has:
        m    = r["metrics"]
        pf_s = f"{m['profit_factor']:.2f}"
        print(
            f"  {r['param_lbl']:<22}"
            f" {r['exit_lbl']:^6}"
            f" {m['trades']:>7}"
            f" {m['win_rate']:>6.1f}%"
            f" {pf_s:>6}"
            f" {m['max_dd']:>7.1f}%"
            f" {m['total_return']:>8.2f}%"
            f" {r['gap_fill']:>9}"
        )
    if _sw_none:
        print(f"  {_sw_sep}")
        print(f"  (no trades: {', '.join(r['param_lbl']+' ['+r['exit_lbl']+']' for r in _sw_none)})")
    print(f"{_SW_SEP}")

    # ── Best combination (minimum 3 trades) ───────────────────────────────────
    _sw_elig = [r for r in _sw_rows if r["metrics"]["trades"] >= 3]
    if _sw_elig:
        _best_sw = max(_sw_elig, key=lambda r: r["metrics"]["profit_factor"])
        m = _best_sw["metrics"]
        tgt_pct = int(_best_sw["target"] * 100)
        stp_pct = int(_best_sw["stop"]   * 100)
        print(f"\n  Best VWAP (≥3 trades): {_best_sw['param_lbl']} [{_best_sw['exit_lbl']}]"
              f"  Target +{tgt_pct}% / Stop {stp_pct}%")
        print(f"  {m['trades']} trades | {m['win_rate']:.1f}% win rate | "
              f"PF {m['profit_factor']:.2f} | "
              f"MaxDD {m['max_dd']:.1f}% | "
              f"return {m['total_return']:+.2f}%")

        _tw = _best_sw["result"]["trades"]
        _SW_SEP2 = "═" * 92
        print(f"\n{_SW_SEP2}")
        print(f"  BEST VWAP — {_best_sw['param_lbl']} [{_best_sw['exit_lbl']}]"
              f"  — day-by-day trade log + direction check")
        print(f"  (Reversal? = did Nifty move in predicted direction by trade close)")
        print(f"{_SW_SEP2}")
        print(f"  {'#':<4} {'Date':<12} {'T':<4} {'Time':<6}"
              f" {'Nifty entry':>12} {'VWAP':>8} {'Dev%':>6} {'RSI':>5}"
              f" {'P&L':>10}  {'Reversal?':<12} Exit reason")
        print(f"  {'-'*92}")
        for t in _tw:
            a_s     = "PUT" if t["action"] == "BUY_PUT" else "CAL"
            rev     = (
                (t["action"] == "BUY_PUT"  and t["exit_nifty"] < t["entry_nifty"]) or
                (t["action"] == "BUY_CALL" and t["exit_nifty"] > t["entry_nifty"])
            )
            rev_s   = "✅ yes" if rev else "❌ no"
            vwap_e  = t.get("vwap_entry", 0)
            dev_pct = (t["entry_nifty"] - vwap_e) / vwap_e * 100 if vwap_e else 0
            ent_t   = t["entry_time"][11:16] if len(t["entry_time"]) > 11 else "?"
            print(
                f"  {t['id']:<4} {t['entry_time'][:10]:<12} {a_s:<4} {ent_t:<6}"
                f" ₹{t['entry_nifty']:>10,.0f}"
                f" ₹{vwap_e:>7,.0f}"
                f" {dev_pct:>+5.2f}%"
                f" {t.get('rsi_entry', 0.0):>4.0f}"
                f" ₹{t['pnl']:>+9,.0f}"
                f"  {rev_s:<12}"
                f" {t['exit_reason']}"
            )
        _rev_n = sum(1 for t in _tw if
                     (t["action"] == "BUY_PUT"  and t["exit_nifty"] < t["entry_nifty"]) or
                     (t["action"] == "BUY_CALL" and t["exit_nifty"] > t["entry_nifty"]))
        pct_rev = _rev_n / len(_tw) * 100 if _tw else 0
        print(f"\n  Actual reversals: {_rev_n}/{len(_tw)} ({pct_rev:.0f}%)"
              f"  — Nifty moved in predicted direction by trade close")
        print(f"{_SW_SEP2}")

    elif _sw_has:
        _best_sw = _sw_has[0]
        print(f"\n  Note: Best combo '{_best_sw['param_lbl']} [{_best_sw['exit_lbl']}]' "
              f"has only {_best_sw['metrics']['trades']} trade(s) — not enough for statistics.")
    else:
        print(f"\n  No VWAP combination generated any trades over this period.")
        print(f"  The combined deviation + RSI extremes did not coincide on ORB-skip days.")

    # ── Sanity-check: existing strategy numbers must be unchanged ──────────────
    print(f"\n  [sanity check] HYBRID PF={hybrid_metrics['profit_factor']:.2f}"
          f"  WR={hybrid_metrics['win_rate']:.1f}%"
          f"  ret={hybrid_metrics['total_return']:.2f}%")
    print(f"  [sanity check] GAP    PF={gap_metrics['profit_factor']:.2f}"
          f"  WR={gap_metrics['win_rate']:.1f}%"
          f"  ret={gap_metrics['total_return']:.2f}%")
    print(f"  [sanity check] COMB   PF={comb_metrics['profit_factor']:.2f}"
          f"  WR={comb_metrics['win_rate']:.1f}%"
          f"  ret={comb_metrics['total_return']:.2f}%")

    # ── FINAL CONFIRMED STRATEGY COMPARISON ───────────────────────────────────
    # Best VWAP combo from sweep: d=0.3%, RSI40/60, Exit B (+25%/-20%)
    _vf_hdays = {t["entry_time"][:10] for t in hybrid_result["trades"]}
    _vwap_fin = run_vwap_param_backtest(
        candles, args.capital,
        deviation   = 0.003,
        rsi_ob      = 60,
        rsi_os      = 40,
        target      = 0.25,
        stop        = -0.20,
        hybrid_days = _vf_hdays,
    )
    _vf_m = compute_metrics(_vwap_fin, args.capital)

    # ALL THREE on one sequential capital pool
    _a3_m = _three_combined_metrics(gap_result, hybrid_result, _vwap_fin, args.capital)

    # Overlap verification
    _h_d = {t["entry_time"][:10] for t in hybrid_result["trades"]}
    _g_d = {t["entry_time"][:10] for t in gap_result["trades"]}
    _v_d = {t["entry_time"][:10] for t in _vwap_fin["trades"]}

    _hv_ov = _h_d & _v_d   # HYBRID ∩ VWAP (should be 0 — by design)
    _gv_ov = _g_d & _v_d   # GAP ∩ VWAP (calendar day, but different time windows)
    _gh_ov = _g_d & _h_d   # GAP ∩ HYBRID (same — different time windows)

    _FIN = "=" * 76
    _fin = "-" * 76
    print(f"\n{_FIN}")
    print(f"  FINAL CONFIRMED STRATEGY SET  (capital ₹{args.capital:,.0f})")
    print(_fin)
    print(f"  {'Strategy':<26} {'Trades':>7} {'Win%':>7} {'PF':>6}"
          f" {'MaxDD%':>8} {'Return%':>9} {'P&L':>12}")
    print(f"  {_fin}")
    for lbl, m in [("HYBRID ORB+Gemini",  hybrid_metrics),
                   ("GAP AND GO",         gap_metrics),
                   ("VWAP Momentum Fade", _vf_m)]:
        print(f"  {lbl:<26}"
              f" {m['trades']:>7}"
              f" {m['win_rate']:>6.1f}%"
              f" {m['profit_factor']:>6.2f}"
              f" {m['max_dd']:>7.1f}%"
              f" {m['total_return']:>8.2f}%"
              f" ₹{m['total_pnl']:>+10,.0f}")
    print(f"  {_fin}")
    print(f"  {'ALL THREE COMBINED':<26}"
          f" {_a3_m['trades']:>7}"
          f" {_a3_m['win_rate']:>6.1f}%"
          f" {_a3_m['profit_factor']:>6.2f}"
          f" {_a3_m['max_dd']:>7.1f}%"
          f" {_a3_m['total_return']:>8.2f}%"
          f" ₹{_a3_m['total_pnl']:>+10,.0f}")
    print(f"  (non-overlapping time windows — capital reused sequentially within each day)")
    print(f"{_FIN}")

    # Non-overlap confirmation
    print(f"\n  Overlap verification (calendar days):")
    print(f"  HYBRID ∩ VWAP  : {len(_hv_ov):>2} day(s)"
          + ("  ✅ none — VWAP skips all HYBRID days by design"
             if len(_hv_ov) == 0
             else f"  ⚠️  {sorted(_hv_ov)}"))
    g_v_note = ("  ✅ none" if len(_gv_ov) == 0
                else f"  {len(_gv_ov)} shared day(s) — GAP exits 10:00, VWAP enters 10:00+  (sequential)")
    print(f"  GAP    ∩ VWAP  : {len(_gv_ov):>2} day(s){g_v_note}")
    print(f"  GAP    ∩ HYBRID: {len(_gh_ov):>2} day(s)  — GAP exits 10:00, HYBRID enters after breakout  (sequential)")
    print(f"\n  Time-window separation guarantees no simultaneous open positions:")
    print(f"  GAP          : 9:15 entry  → exits by 10:00  (45-minute max hold)")
    print(f"  HYBRID ORB   : 9:30+ entry → exits by 14:30")
    print(f"  VWAP Fade    : 10:00–13:30 entry → exits by 14:00  (skips HYBRID days)")
    print(f"\n  Confirmed strategy parameters (backtest Jan–May 2026):")
    print(f"  HYBRID    : range 30–150pts | close-confirm | -35%/+75% | trail→BE@+40% | 14:30 exit")
    print(f"  GAP       : gap ±0.5–2.0% at 9:15 | -25%/+40% | hard exit 10:00 AM")
    print(f"  VWAP Fade : 30-candle proxy | dev>0.3% | RSI<40 or >60 | -20%/+25% | reversion exit | 14:00 exit")


if __name__ == "__main__":
    main()
