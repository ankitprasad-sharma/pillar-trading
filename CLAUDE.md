# PILLAR TRADING — Claude Code Context

## Project Overview
Pillar Trading is a semi-automated options trading system for Indian markets (NSE) built on:
- **Upstox API** — live market data (WebSocket) + order execution
- **Gemini 2.5 Flash** — AI signal generation
- **Python** — core engine running on Ubuntu (MacBook Air 2015)
- **Paper trading** — currently in simulation mode, not live orders

The system watches Nifty 50, generates BUY_CALL / BUY_PUT / STAY_OUT signals every 60 seconds,
shows them to the user for approval (Y/N), and tracks paper P&L.

---

## Project Structure

```
~/pillar-trading/
├── market_feed.py      # Main entry point — WebSocket feed + dashboard UI
├── main.py             # Signal logic — cooldown, approval flow, pending state
├── ai_router.py        # Gemini API call + rule-based fallback
├── option_chain.py     # Option contract lookup + premium fetch (cached)
├── paper_trader.py     # Paper trade ledger — open/close/summary
├── order_manager.py    # Live order execution via Upstox v3 (LIVE_TRADING flag)
├── risk_manager.py     # Position size / daily loss / trade count limits
├── upstox_auth.py      # Daily OAuth flow to refresh Upstox access token
├── paper_trades.json   # Persistent paper trade ledger
├── .env                # API keys (never modify or expose)
├── silence.py          # Suppress noisy library loggers
└── venv/               # Python virtual environment
```

---

## Environment Setup

```bash
cd ~/pillar-trading
source venv/bin/activate

# Daily startup sequence
python3 upstox_auth.py    # Refresh Upstox token (expires midnight)
python3 market_feed.py    # Start dashboard
```

### API Keys in .env
```
GEMINI_API_KEY=...
UPSTOX_API_KEY=...
UPSTOX_API_SECRET=...
UPSTOX_ACCESS_TOKEN=...   # Refreshed daily via upstox_auth.py
```

---

## Architecture — Data Flow

```
Upstox WebSocket (MarketDataStreamerV3)
    │
    ├── Nifty 50 index ticks (NSE_INDEX|Nifty 50)
    │       → updates state[ltp, prev_close, change_pct]
    │       → triggers subscribe_options() for ATM CE/PE
    │       → triggers run_signal() every 60s
    │
    ├── ATM option ticks (NSE_FO|XXXXX CE + PE)
    │       → updates state[ce_premium, pe_premium]
    │
    └── Position contract ticks (NSE_FO|XXXXX — bought contract)
            → updates state[pos_premium_live]
            → run_signal() checks stop loss / profit target

Gemini 2.5 Flash API
    → receives market_data dict
    → returns {action, strike, reason, confidence}
    → result set as state[pending_signal]

User keyboard input (tty.setcbreak)
    Y → open_trade() → subscribe bought contract
    N → clear pending signal
    X → manual exit at current premium
    S → show summary
    Q → quit
```

---

## State Dictionary (market_feed.py)

All live data lives in `state` dict. Never use global variables elsewhere.

```python
state = {
    # Market
    "ltp"             : float,    # Nifty 50 last traded price
    "prev_close"      : float,    # LOCKED at startup from historical API
    "prev_close_locked": bool,    # True after historical fetch
    "change_pct"      : float,    # % change from prev_close
    "last_update"     : str,      # HH:MM:SS of last tick
    "market_status"   : str,      # NORMAL_OPEN / NORMAL_CLOSE etc

    # ATM options (current strike based on LTP)
    "ce_premium"      : float,    # Current ATM call premium
    "pe_premium"      : float,    # Current ATM put premium
    "ce_strike"       : int,      # ATM strike price
    "pe_strike"       : int,      # ATM strike price (same as ce_strike)
    "ce_instrument"   : str,      # e.g. "NSE_FO|51364"
    "pe_instrument"   : str,
    "ce_symbol"       : str,      # e.g. "NIFTY 23650 CE 19 MAY 26"
    "pe_symbol"       : str,

    # Open position tracking
    "pos_instrument"  : str,      # Instrument key of BOUGHT contract
    "pos_premium_live": float,    # Live premium of bought contract

    # Signal flow
    "pending_signal"  : dict,     # Set by run_signal, cleared by Y/N
    "pending_option"  : dict,     # Option data for pending signal
    "last_signal"     : dict,     # Last generated signal (for display)
    "last_signal_time": str,

    # ORB state (Opening Range Breakout)
    "orb_day"         : str,      # Trading date current range belongs to (YYYY-MM-DD)
    "orb_high"        : float,    # Range high — built 9:15–9:30, locked after
    "orb_low"         : float,    # Range low
    "orb_range"       : float,    # Locked range size in points
    "orb_status"      : str,      # BUILDING / WATCHING / BROKE UP|DN @ XXXXX / SKIP-WIDE / SKIP-NARROW / HYBRID-SKIP
    "orb_traded_today": bool,     # True once a trade or skip-decision has been made

    # Control
    "running"         : bool,
    "paused"          : bool,     # True during summary display
    "strategy_mode"   : str,      # Mirror of main.STRATEGY_MODE — "ORB" / "HYBRID" / "GEMINI"
}
```

---

## Critical Rules — Always Follow These

### Instrument Keys
- **NEVER hardcode instrument keys** (e.g. NSE_FO|51364)
- Always look up dynamically from `get_all_contracts()` using strike + expiry + type
- Keys change between weekly expiries
- On startup, `restore_position_subscription()` must look up fresh key even if JSON has one saved
- After finding key, update `paper_trades.json` with correct key

### Lot Size
- **Always use quantity = 65** (current Nifty lot size as of May 2026)
- Old trades used 75 — this was wrong
- `paper_trader.py open_trade()` must hardcode 65, never 75

### Previous Close
- **Always fetch from historical API at startup** — do not trust WebSocket `cp` field
- Lock it in `state["prev_close"]` with `state["prev_close_locked"] = True`
- Once locked, WebSocket `cp` field must NOT overwrite it
- Endpoint: `GET /v2/historical-candle/NSE_INDEX%7CNifty%2050/day/{today}/{yesterday}`

### Pending Signal Race Condition
- `state["pending_signal"]` is set in `run_signal()` thread
- `handle_key()` reads it in input thread
- Rule: if `state["pending_signal"]` is not None, `run_signal()` must return immediately
- Rule: only set pending_signal if it is currently None (atomic check)
- Never clear pending_signal from any thread except handle_key()

### Position P&L
- **Bought contract P&L** uses `state["pos_premium_live"]` — from WebSocket tick of exact bought instrument
- **Current ATM** is displayed separately for context only
- Stop loss (-30%) and profit target (+50%) always measured against bought contract, not ATM
- If `pos_premium_live` is None, display "(entry)" label and show entry price

### Token Refresh
- Upstox access token expires at midnight every day
- Must run `python3 upstox_auth.py` every morning before market open
- On 401 error in WebSocket, log "Token expired — run upstox_auth.py" and stop

---

## Trading Strategy Context

### Strategy Mode (`main.py: STRATEGY_MODE`)
Four modes selectable at the top of `main.py`:
- **`"HYBRID"`** (default, live-ready) — ORB breakout entry + Gemini must confirm direction. Backtested PF 1.50, WR 56.2%, +29.74% on 48 trades (Jan–May 2026). **Confirmed primary strategy.**
- **`"ORB"`** — ORB breakout only, no Gemini confirmation. Backtested PF 1.25, WR 53.1%, 64 trades.
- **`"VWAP_REVERSAL"`** — VWAP Momentum Fade on ORB-skip days. Backtested PF 1.93, WR 62.5%, +5.98% on 16 trades (Jan–May 2026). Complementary to HYBRID — never fires on same days.
- **`"GEMINI"`** — Legacy mode: Gemini/rule-based signal on change_pct threshold, fires every 60 s.

Signal routing in `run_signal()` (`market_feed.py`):
```python
if strategy_mode == "GEMINI":         result = _main.on_market_data(market_data)
elif strategy_mode == "ORB":          result = _check_orb_signal(market_data, hybrid=False)
elif strategy_mode == "HYBRID":       result = _check_orb_signal(market_data, hybrid=True)
elif strategy_mode == "GAP_AND_GO":   result = _check_gap_signal(market_data)
elif strategy_mode == "VWAP_REVERSAL":result = _check_vwap_signal(market_data)
```

### Primary Strategy: ORB / HYBRID (Opening Range Breakout)
- Build Nifty range from 9:15–9:30 AM ticks (high/low of every tick)
- Lock range at first tick after 9:30
- Skip if range < 30 pts (too narrow) or > 150 pts (too wide — volatile open)
- Close-confirmation breakout: LTP must cross range high/low after 9:30
- One trade per day maximum (`orb_traded_today` flag)
- Confirmed parameters (backtested Jan–May 2026):
  - Range filter: 30–150 pts (skip if too narrow or too wide)
  - Entry: close-confirmation (LTP tick must cross range boundary)
  - Stop: −35% (`ORB_STOP_LOSS`)
  - Target: +75% (`ORB_PROFIT_TARGET`)
  - Trailing stop: move stop to breakeven at +40% gain (`orb_trail_active`)
  - Force-close: any open position is closed at 14:30 (`run_signal` time check)
  - No new entries after 14:30 (`_check_orb_signal` time guard)
  - Manual: X key (exit at current price)

### VWAP Momentum Fade Strategy (`STRATEGY_MODE = "VWAP_REVERSAL"`)
Fires on ORB-skip days only (guaranteed non-overlapping with HYBRID).

**Confirmed parameters (backtested Jan–May 2026, parameter sweep):**
- VWAP proxy: rolling 30-candle (30-minute) mean of Nifty LTP ticks
- RSI: Wilder smoothing, 14-period
- Entry window: 10:00–13:30 (skip before 10:00 — too close to open; skip after 13:30 — theta risk)
- Entry condition: `dev > 0.3% AND RSI > 60` → BUY_PUT (fade overbought extension)
- Entry condition: `dev < -0.3% AND RSI < 40` → BUY_CALL (fade oversold extension)
- Skip if `orb_traded_today` is True (ORB/HYBRID took a trade)
- One trade per day maximum (`vwap_traded` flag)
- Stop: −20% (`VWAP_STOP_LOSS`)
- Target: +25% (`VWAP_PROFIT_TARGET`)
- Reversion exit: if price crosses back through VWAP while not in loss (pct > −5%), exit
- Force-close: 14:00 (`run_signal` time check)
- Manual: X key (exit at current price)

**Constants in `main.py`:**
```python
VWAP_PROFIT_TARGET =  25   # %
VWAP_STOP_LOSS     = -20   # %
VWAP_DEVIATION     = 0.003 # 0.3% from 30-candle mean
VWAP_RSI_OB        = 60    # fade when RSI > this
VWAP_RSI_OS        = 40    # fade when RSI < this
```

**State keys (market_feed.py):**
```python
"vwap"           : float,   # current 30-candle VWAP proxy
"rsi"            : float,   # current RSI(14)
"vwap_deviation" : float,   # (ltp - vwap) / vwap
"vwap_signal"    : str,     # BUY_CALL / BUY_PUT / None
"vwap_traded"    : bool,    # True once trade or skip-decision made today
"vwap_status"    : str,     # WAITING / COMPUTING / WATCHING / TRADED / SKIP
```

**Backtested results (Jan–May 2026, d=0.3%, RSI 40/60, Exit B +25%/−20%):**
- 16 trades, WR 62.5%, PF 1.93, MaxDD 3.2%, +5.98%
- HYBRID∩VWAP overlap = 0 days (confirmed non-overlapping by design)
- ALL THREE COMBINED (GAP + HYBRID + VWAP): 94 trades, WR 54.3%, PF 1.38, MaxDD 12.1%, +38.19%

### Legacy Strategy: Gemini Directional
- Buy ATM or near-ATM call/put based on Gemini signal
- Entry: on signal with HIGH or MEDIUM confidence
- Exit rules:
  - Auto: +50% premium gain (profit target)
  - Auto: -30% premium loss (stop loss)
  - Manual: X key (exit at current price)
  - Time: system should warn after 2:30 PM — theta decay accelerates

### Signal Generation (ai_router.py)
Gemini receives this market data dict (built in `market_feed.py:on_message`):
```python
{
    "ltp"              : 23650.0,   # Nifty current price
    "prev_close"       : 23412.60,  # Yesterday's close (locked at startup)
    "change_pct"       : 1.01,      # % change from prev_close
    "option_premium_ce": 196.0,     # Current ATM call premium
    "option_premium_pe": 184.0,     # Current ATM put premium
    "option_premium"   : 196.0,     # Same as CE (legacy field — keep for compat)
    "ce_strike"        : 23650,     # ATM strike price
    "volume"           : 0,         # Placeholder — add Nifty futures volume later
    "vix"              : 14.0,      # Hardcoded — real feed pending
    "time"             : "11:35",   # HH:MM at time of signal generation
}
```

Gemini returns:
```json
{
    "action": "BUY_CALL",
    "strike": 23650,
    "reason": "one line reason",
    "confidence": "HIGH",
    "source": "gemini"
}
```

Only HIGH and MEDIUM confidence signals proceed. LOW confidence is blocked by RiskManager.

### Risk Parameters (risk_manager.py)
```python
max_daily_loss    = ₹5,000    # One bad trade shouldn't end the day
max_trades_per_day = 1        # ORB = strictly one trade per day
max_position_size = ₹16,250   # 250 max premium × 65 lots
```
- ORB/HYBRID blocks second trade with reason "ORB allows only 1 trade per day"
- `reset_daily_counters()` called in `on_open()` each session — resets loss, trade count, and dedup set
- Telegram limit alert fires once per session (dedup cleared on reset, so it fires again next day)

---

## Known Issues & Tech Debt

1. **VIX hardcoded at 14.0** — needs real feed (see Additional APIs below)
2. **Volume always 0** — Nifty index doesn't have volume; use futures volume instead
3. **No time-based exit** — should auto-exit positions after 2:30 PM (ORB exit is manual via X or auto stop/target only)
4. **Single position only** — no multi-position support
5. **No partial exit** — only full exit via X key or auto-exit rules

---

## Additional APIs to Integrate

### 1. India VIX (Real-time)
India VIX is the fear index — critical for strategy selection.
- VIX < 13: sell options (low volatility, theta decay strategy)
- VIX 13-18: directional buying (current strategy)
- VIX > 18: avoid or buy straddles (high volatility events)

```python
# Fetch India VIX via Upstox WebSocket
VIX_KEY = "NSE_INDEX|India VIX"
# Subscribe alongside Nifty in MarketDataStreamerV3
streamer.subscribe(["NSE_INDEX|India VIX"], "ltpc")
# Parse same as indexFF ltpc
```

### 2. Nifty Futures (Volume + OI)
Index has no volume. Nifty futures give real volume and open interest.
```python
# Near month Nifty futures instrument key (changes monthly)
# Fetch from option_chain equivalent for futures
# Use for volume confirmation in signals
```

### 3. Option Chain Greeks (Upstox)
Delta, gamma, theta, vega for better signal quality.
```python
# Upstox option chain endpoint
GET /v2/option/chain?instrument_key=NSE_INDEX|Nifty 50&expiry_date=2026-05-19
# Returns full chain with greeks
# Use: high delta (>0.4) = better directional trade
# Use: high gamma = fast premium movement
# Use: high theta = avoid buying, good for selling
```

### 4. NSE Option Chain (Public — No Auth)
NSE website provides free option chain data with PCR (Put-Call Ratio).
```python
import requests
r = requests.get(
    "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com"}
)
# PCR > 1.2 = bearish sentiment (more puts than calls)
# PCR < 0.8 = bullish sentiment
# Use PCR as confirmation for Gemini signal
```

### 5. Economic Calendar (For Event Avoidance)
Avoid trading on high-impact events:
- RBI MPC meetings (6 per year)
- Union Budget (February)
- US Fed meetings (8 per year)
- CPI/WPI data releases

```python
# Free API: https://api.tradingeconomics.com/calendar
# Or maintain a hardcoded list of known event dates
# Rule: STAY_OUT on event days unless specifically trading the event
```

### 6. Technical Indicators (For Better Signals)
Add to market_data dict before sending to Gemini:
```python
import pandas as pd

def calculate_indicators(candles: list) -> dict:
    df = pd.DataFrame(candles, columns=['ts','open','high','low','close','vol','oi'])
    df['ema9']  = df['close'].ewm(span=9).mean()
    df['ema21'] = df['close'].ewm(span=21).mean()
    df['rsi']   = calculate_rsi(df['close'], 14)
    df['vwap']  = (df['close'] * df['vol']).cumsum() / df['vol'].cumsum()
    return {
        "ema9"       : df['ema9'].iloc[-1],
        "ema21"      : df['ema21'].iloc[-1],
        "rsi"        : df['rsi'].iloc[-1],
        "vwap"       : df['vwap'].iloc[-1],
        "trend"      : "UP" if df['ema9'].iloc[-1] > df['ema21'].iloc[-1] else "DOWN",
        "rsi_signal" : "OVERBOUGHT" if df['rsi'].iloc[-1] > 70 else
                       "OVERSOLD"   if df['rsi'].iloc[-1] < 30 else "NEUTRAL"
    }
```

---

## Strategies to Implement (Priority Order)

### 1. Opening Range Breakout (ORB) — HIGH PRIORITY
Best fit for current semi-auto setup.
```
9:15 AM - 9:30 AM: Record Nifty high and low (opening range)
9:30 AM onwards:
    - If LTP breaks above range high → BUY CALL (ATM)
    - If LTP breaks below range low  → BUY PUT (ATM)
    - If no breakout by 10:00 AM     → STAY OUT for the day
Exit:
    - 50% premium gain (target)
    - 30% premium loss (stop)
    - 2:30 PM time exit
    - Opposite breakout (range reversal)
```

Implementation needed in `main.py`:
```python
ORB_STATE = {
    "range_high": None,
    "range_low": None,
    "range_set": False,
    "traded_today": False,
    "range_end_time": "09:30",
    "no_trade_after": "10:00"
}
```

### 2. Trend Confirmation Filter — MEDIUM PRIORITY
Before any BUY_CALL signal is approved:
- EMA9 must be above EMA21 (uptrend)
- RSI must be between 40-70 (not overbought)
- LTP must be above VWAP

Before any BUY_PUT signal is approved:
- EMA9 must be below EMA21 (downtrend)
- RSI must be between 30-60 (not oversold)
- LTP must be below VWAP

### 3. VIX-Based Strategy Switching — MEDIUM PRIORITY
```python
def get_strategy_mode(vix: float) -> str:
    if vix < 13:
        return "SELL_PREMIUM"    # Iron condor / straddle selling
    elif vix < 18:
        return "BUY_DIRECTIONAL" # Current strategy
    else:
        return "AVOID"           # High volatility — stay out or buy straddle
```

### 4. PCR Confirmation — LOW PRIORITY
Use NSE option chain PCR to confirm signals:
- Gemini says BUY_CALL + PCR < 0.8 (bullish) → STRONG BUY
- Gemini says BUY_CALL + PCR > 1.2 (bearish) → SKIP (contradiction)

### 5. Time-Based Rules — IMPLEMENT IMMEDIATELY
```python
from datetime import datetime

def is_trading_allowed() -> tuple[bool, str]:
    now = datetime.now()
    t   = now.strftime("%H:%M")

    if t < "09:20":
        return False, "Pre-market — wait for 9:20"
    if t > "15:00":
        return False, "Post 3 PM — avoid theta risk"
    if "09:20" <= t <= "09:30":
        return False, "Opening volatility — building ORB range"
    return True, "OK"
```

---

## Gemini Prompt Engineering

**Implemented** in `ai_router.py`. The prompt uses a `_SYSTEM_PROMPT` constant with explicit trading rules, followed by structured labelled market data fields. Do not revert to a raw `json.dumps(market_data)` approach — LLMs respond better to labelled lines than compact JSON blobs.

Current prompt structure:
```
{_SYSTEM_PROMPT}          ← rules block (9 numbered constraints)

Current market data:
- Nifty LTP      : ₹{ltp:,.2f}
- Previous close : ₹{prev:,.2f}
- Change         : {chg:+.2f}%
- India VIX      : {vix}
- ATM strike     : {strike}
- ATM Call (CE) premium : ₹{ce_prem}
- ATM Put  (PE) premium : ₹{pe_prem}
- Time           : {cur_time}

Respond ONLY in this JSON format: ...
```

When technical indicators are available (EMA, RSI, VWAP, PCR), add them as additional labelled lines here — do not change the surrounding structure:
```
- EMA9 vs EMA21  : {trend}        # "UP" or "DOWN"
- RSI(14)        : {rsi}
- VWAP           : ₹{vwap:,.2f}
- PCR            : {pcr}
```

---

## Paper Trade Ledger Schema (paper_trades.json)

Every trade must have ALL these fields:
```json
{
  "id": 1,
  "action": "BUY_CALL or BUY_PUT",
  "strike": 23650,
  "trading_symbol": "NIFTY 23650 CE 19 MAY 26",
  "instrument_key": "NSE_FO|51364",
  "entry_premium": 210.65,
  "quantity": 65,
  "cost": 13692.25,
  "entry_ltp": 23670.35,
  "entry_time": "2026-05-14 12:01:19",
  "exit_premium": null,
  "exit_ltp": null,
  "exit_time": null,
  "pnl": null,
  "status": "OPEN or CLOSED",
  "reason": "signal reason",
  "confidence": "HIGH or MEDIUM or LOW"
}
```

Rules:
- `instrument_key` must NEVER be null or empty string on a new trade
- `strike` must NEVER be null on a new trade
- `quantity` must always be 65
- `cost` = entry_premium × quantity
- `pnl` = (exit_premium - entry_premium) × quantity

---

## Common Issues & Fixes

### "Can't restore position — no strike info"
→ `paper_trades.json` open_position has `strike: null`
→ Fix: `restore_position_subscription()` must use `entry_ltp` to derive strike
→ Permanent fix: `open_trade()` must always save strike before writing JSON

### "Bought contract: (entry) — not updating"
→ `pos_instrument` not subscribed or wrong instrument key
→ Fix: always look up key from `get_all_contracts()` dynamically
→ Never trust saved instrument_key — always verify against live contracts

### "CE/PE showing ₹117 (estimate)"
→ REST API rate limited (429)
→ Fix: use WebSocket subscription for ATM options, not REST polling
→ WebSocket has no rate limit for subscribed instruments

### "No pending signal when Y pressed"
→ Race condition: `run_signal` thread clears pending between signal and keypress
→ Fix: `run_signal` must check `if state["pending_signal"]: return` at start
→ Fix: only set pending_signal atomically when currently None

### "Dashboard not updating (stale feed)"
→ Upstox token expired (midnight) or WebSocket silently disconnected
→ Fix: heartbeat detects no tick in 60s and logs warning
→ Solution: restart with `python3 upstox_auth.py && python3 market_feed.py`

### "Prev close changing mid-session"
→ Upstox WebSocket `cp` field updates during session
→ Fix: fetch from historical API at startup, lock with `prev_close_locked = True`
→ Never let WebSocket overwrite a locked prev_close

---

## Performance Tracking

Current paper trading stats (as of May 14, 2026):
- Starting capital: ₹1,00,000
- Current capital: ₹64,767
- Total P&L: -₹1,748.50
- Trades: 3 closed, 1 open
- Win rate: 33.3% (1 win, 2 losses)
- Best trade: +₹2,808 (23600 CE, 22% gain)
- Worst trade: -₹4,556 (23600 PE, bought wrong direction)

Target metrics before going live:
- Minimum 20 paper trades
- Win rate > 55%
- Profit factor > 1.5 (total wins / total losses)
- Max drawdown < 20% of starting capital

---

## Going Live Checklist (Do NOT skip any)

### Paper Trading Validation
- [ ] 20+ paper trades on HYBRID strategy (currently 0 — reset 2026-05-14)
- [ ] Win rate > 55%
- [ ] Profit factor > 1.5 (total wins / total losses)
- [ ] Max drawdown < 20% of starting capital

### Code Readiness
- [x] Real order placement via Upstox Order API v3 (`order_manager.py`)
- [x] Telegram notifications wired end-to-end
- [x] Risk manager with 1-trade/day ORB cap and daily loss limit
- [x] Auto stop loss, profit target, trailing stop, 14:30 force-exit
- [ ] Add order status tracking — poll `get_order_status()` after placing to confirm filled/rejected
- [ ] Add slippage buffer — assume 2-5 pts worse than signal price on entry/exit
- [ ] Add daily loss auto-shutdown — auto-exit open position if total_pnl crosses -₹5,000
- [ ] Test Upstox kill switch (segment disable via Upstox app) before first live trade
- [ ] Exchange-side SL order (bracket order flow):
  - After BUY MARKET fills: immediately place SL-M SELL order with `trigger_price = entry_premium * 0.65` (-35%)
  - Store the SL `order_id` in the position record (`paper_trades.json` open_position)
  - On software target hit (+75%): place SELL MARKET to exit, then cancel SL order via `order_id`
  - On exchange SL execution first: Upstox sends order update via WebSocket → detect it, sync paper_trader records, clear position state
  - This ensures the stop is protected at the exchange level even if our process crashes

### Account & Capital
- [ ] Fund Upstox account with ₹25,000 minimum
- [ ] Confirm Nifty F&O segment is activated in Upstox account
- [ ] Start with 1 lot (65 qty) only — already hardcoded
- [ ] Never trade on event days (RBI MPC, Union Budget, US Fed, election results)

### Final Gate
- [ ] Set `LIVE_TRADING = True` in `order_manager.py` — human-only, never automated
- [ ] Have manual override (X key or Upstox app) ready at all times

---

## Claude Code Instructions

When making changes to this codebase:

1. **Always read the file first** before editing — never assume content
2. **Never hardcode instrument keys** — always use `get_all_contracts()`
3. **Test after every change** — run `python3 market_feed.py` briefly
4. **Preserve the state dict structure** — other files depend on exact key names
5. **Keep threading safe** — use `_signal_lock.acquire(blocking=False)` pattern
6. **Log all important events** — use `add_log()` not print()
7. **Never modify .env directly** — use `set_key()` from dotenv
8. **Paper trades JSON** — always validate all required fields before writing
9. **Lot size is 65** — reject any change that uses 75
10. **Prev close must be locked** — never allow WebSocket to overwrite it

