# HYPER_ETH — Data Flow & Architecture Reference

## Table of Contents

- [1. System Overview](#1-system-overview)
- [2. File Map](#2-file-map)
- [3. Startup Sequence](#3-startup-sequence)
- [4. WebSocket Data Flows](#4-websocket-data-flows)
  - [4.1 AllMids → Price Ticks](#41-allmids--price-ticks)
  - [4.2 Candle WS → Strategy Tick](#42-candle-ws--strategy-tick)
  - [4.3 OrderUpdates → Position Close](#43-orderupdates--position-close)
- [5. REST Fallback & Emergency Modes](#5-rest-fallback--emergency-modes)
- [6. Strategy Engine Lifecycle](#6-strategy-engine-lifecycle)
  - [6.1 Warmup](#61-warmup)
  - [6.2 tick() — Candle Close Processing](#62-tick--candle-close-processing)
  - [6.3 on_price_update() — Real-Time SL Management](#63-on_price_update--real-time-sl-management)
  - [6.4 Entry Signal Logic](#64-entry-signal-logic)
  - [6.5 Position Exit Paths](#65-position-exit-paths)
- [7. Position Sync (Startup + Runtime)](#7-position-sync-startup--runtime)
- [8. Emergency Close Pipeline](#8-emergency-close-pipeline)
- [9. Order Lifecycle on Exchange](#9-order-lifecycle-on-exchange)
- [10. State Persistence](#10-state-persistence)
- [11. Risk Management](#11-risk-management)
- [12. Threading Model](#12-threading-model)
- [13. Key Constants & Thresholds](#13-key-constants--thresholds)

---

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        run.py (main loop)                       │
│   • Startup, warmup, heartbeat, WS health, REST fallback       │
│   • Emergency REST mode, Flash Crash Red Button                 │
└───────┬──────────┬──────────────┬──────────────┬────────────────┘
        │          │              │              │
   ┌────▼────┐ ┌──▼───────┐ ┌───▼──────┐ ┌────▼─────┐
   │ WS Mgr  │ │ Strategy │ │  Trader  │ │ Notifier │
   │ (thread)│ │ Engine   │ │ (REST)   │ │ (Tg bot) │
   └────┬────┘ └──────────┘ └──────────┘ └──────────┘
        │         ▲                ▲
        │         │ callbacks      │ API calls
        ▼         │                │
   ┌─────────────────────────────────┐
   │   HyperLiquid Exchange (L1)    │
   │   WS: AllMids, Candle, Orders  │
   │   REST: candles, user_state,   │
   │         orders, market_close   │
   └─────────────────────────────────┘
```

**Основной принцип:** WebSocket — primary, REST — fallback. Биржа выполняет SL/TP ордера, бот только мониторит и обновляет их.

---

## 2. File Map

| File | Lines | Responsibility |
|------|-------|----------------|
| `run.py` | ~409 | Entry point. Startup, main loop, WS health, REST fallback, emergency modes |
| `core/strategy.py` | ~1081 | **Core logic.** Warmup, indicators, entries, exits, trailing SL, emergency close |
| `core/trader.py` | ~424 | REST API: orders, positions, candles. Cached `user_state` (5s TTL) |
| `core/ws_manager.py` | ~253 | WS subscriptions: AllMids, OrderUpdates, Candle. Candle buffer management |
| `core/notifier.py` | ~37 | Telegram notifications via aiohttp |
| `core/signer.py` | ~57 | Wallet signing, Exchange + Info init, Unified Account equity |
| `config/settings.py` | ~58 | Strategy params (mirrors Pine Script inputs), risk params, global config |
| `config/credentials.py` | ~19 | Env vars: wallet, keys, Telegram tokens |
| `data/state_ETH.json` | — | Persisted `TradeState` (position, risk, OB/FVG levels, bar counter) |
| `data/trades.json` | — | Trade history log (last 500 trades) |

---

## 3. Startup Sequence

```
run.py::main()
│
├─ 1. Create HLTrader, Notifier
├─ 2. Create StrategyEngine per symbol (loads state_ETH.json)
├─ 3. Create HLWebSocketManager(loop)
│     ├─ Register callbacks: on_price, on_order, on_candle_close
│     └─ ws.start() → subscribes AllMids + OrderUpdates + Candle
│
├─ 4. Log equity, prices, positions
├─ 5. Send Telegram startup notification
│
├─ 6. FOR EACH SYMBOL:
│     ├─ trader.get_candles(sym)          ← REST: fetch 300 candles
│     ├─ engine.warmup(candles)           ← replay history for OB/FVG
│     │   ├─ Detect pivots → bullOB, bearOB
│     │   ├─ Detect FVGs → bull_fvg, bear_fvg
│     │   ├─ Set bar_counter = len(candles) - 1
│     │   ├─ Set _last_candle_t = candles[-2]["t"]
│     │   ├─ POSITION SYNC (4 scenarios):
│     │   │   ├─ State=IN, Exchange=NONE → clear state
│     │   │   ├─ State=FLAT, Exchange=HAS → close orphan
│     │   │   ├─ State=IN, Exchange=HAS, side mismatch → close + clear
│     │   │   └─ State=IN, Exchange=HAS, OK → verify size, log
│     │   └─ _warmup_done = True  ← WS callbacks now active
│     └─ ws.seed_candles(sym, candles)    ← seed WS buffer
│
├─ 7. MAIN LOOP (every 15s):
│     ├─ REST fallback candle check (60s normal / 3s emergency)
│     ├─ WS health check (every 15s)
│     │   ├─ Dead? → emergency mode + reconnect + backoff
│     │   ├─ 60s silence + position? → RED BUTTON close
│     │   └─ Alive after emergency? → 3 pongs → exit emergency
│     └─ Heartbeat log (every 120s)
```

### Warmup Detail (`strategy.py` L223-380)

```python
# Warmup walks candles[0..N-2] (excludes forming candle[-1])
for i in range(plen*2, len(candles) - 1):
    # Pivot detection → bullOB, bearOB
    # FVG detection → bull_fvg_*, bear_fvg_*
```

**Critical:** Warmup stops at `len(candles) - 1` because `candles[-1]` is the forming (unclosed) candle. Including it would create false FVG/OB levels from incomplete data.

---

## 4. WebSocket Data Flows

### 4.1 AllMids → Price Ticks

```
HyperLiquid WS                    ws_manager.py                    strategy.py
     │                                  │                               │
     │ {'allMids': {'ETH': '1950'}}     │                               │
     ├─────────────────────────────────►│                               │
     │                           _on_all_mids()                         │
     │                           filter symbols                         │
     │                           update _prices[sym]                    │
     │                           update _last_price_time                │
     │                                  │                               │
     │                           for cb in _on_price_cbs:               │
     │                                  │  cb(sym, price)               │
     │                                  ├──────────────────────────────►│
     │                                  │                    on_price_update(price)
     │                                  │                    [SYNC method]
     │                                  │                               │
     │                                  │    if returns coroutine ──────┤
     │                                  │    (SL update / emergency)    │
     │                                  │◄──────────────────────────────┤
     │                           run_coroutine_threadsafe(coro, loop)   │
```

**Key:** `on_price_update()` is **synchronous** — runs on WS thread. If it needs async work (exchange SL update, emergency close), it **returns a coroutine** which WS manager schedules on the asyncio loop.

**Frequency:** Every price change (typically every 1-5 seconds for ETH).

### 4.2 Candle WS → Strategy Tick

```
HyperLiquid WS                    ws_manager.py                    strategy.py
     │                                  │                               │
     │ {'candle': {t: 1000, c: 1950}}   │                               │
     ├─────────────────────────────────►│                               │
     │                           _on_candle_msg()                       │
     │                                  │                               │
     │     CASE A: same timestamp       │                               │
     │     → buf[-1] = candle (update)  │ (no callback)                 │
     │                                  │                               │
     │     CASE B: new timestamp        │                               │
     │     → buf.append(candle)         │                               │
     │     → trim if > max_len          │                               │
     │     → candles_copy = list(buf)   │                               │
     │                                  │                               │
     │                           for cb in _on_candle_close_cbs:        │
     │                                  │  cb(sym, candles_copy)        │
     │                                  ├──────────────────────────────►│
     │                                  │                     tick(candles)
     │                                  │                     [ASYNC coroutine]
     │                                  │◄──────────────────────────────┤
     │                           run_coroutine_threadsafe(coro, loop)   │
```

**Candle buffer layout at callback time:**
```
buf = [..., candle[-3], candle[-2], candle[-1]]
                         ↑ CLOSED      ↑ FORMING (just started)
```

`tick()` receives full buffer. It uses `candles[:-1]` (closed only) for all indicator/signal logic.

### 4.3 OrderUpdates → Position Close

```
HyperLiquid WS                    ws_manager.py                    strategy.py
     │                                  │                               │
     │ {'orderUpdates': [{              │                               │
     │    coin: 'ETH',                  │                               │
     │    status: 'filled',             │                               │
     │    orderType: 'Stop Market'      │                               │
     │  }]}                             │                               │
     ├─────────────────────────────────►│                               │
     │                          _on_order_update()                      │
     │                                  │                               │
     │                           for cb in _on_order_cbs:               │
     │                                  │  cb(order_data)               │
     │                                  ├──────────────────────────────►│
     │                                  │              on_order_triggered(data)
     │                                  │              if status='filled'
     │                                  │              AND 'Stop'|'Take Profit':
     │                                  │                               │
     │                                  │              cancel_all_orders()
     │                                  │              verify_no_reverse()
     │                                  │              _on_position_closed()
```

**Purpose:** Instant detection when exchange SL/TP fires. Faster than waiting for next candle close `_sync_position()`.

---

## 5. REST Fallback & Emergency Modes

### Normal REST Fallback (every 60s)
```
run.py main loop → trader.get_candles(sym)
  → compare REST last_closed_t vs WS buf last_closed_t
  → if REST newer: WS missed a candle close!
    → ws.seed_candles() (resync buffer)
    → engine.tick(candles) (run strategy on REST data)
```

### Emergency REST Mode (every 3s)
**Triggered when:** WS dead for >45s OR `ws.connected = False`.

```
ws_emergency_mode = True
  → REST candle polling 3s instead of 60s
  → Reconnect WS with exponential backoff (2s → 4s → 8s → ... → 30s max)
  → After reconnect:
    → Re-seed candle buffers from REST
    → Post-reconnect SL sanity check (read open_orders, verify SL exists & matches)
  → Exit emergency after 3 consecutive WS pongs
```

### Flash Crash Red Button (60s silence)
```
IF ws._last_price_time > 60s ago AND engine.state.in_position:
  → market_close ALL positions
  → cancel_all_orders
  → Telegram alert
  → _on_position_closed()
```

---

## 6. Strategy Engine Lifecycle

### 6.1 Warmup

**Where:** `strategy.py::warmup()` L223

**Input:** 300 candles from REST API.

**Process:**
1. Walk candles `[plen*2 .. len-2]` (skip first few, exclude forming candle)
2. For each bar: detect pivot highs/lows → set `bearOB` / `bullOB`
3. For each bar: detect FVGs → set `bull_fvg_*` / `bear_fvg_*`
4. Set `bar_counter = len(candles) - 1`
5. Set `_last_candle_t = candles[-2]["t"]` (prevents re-processing first WS candle)
6. **Position sync** (see Section 7)
7. Set `_warmup_done = True` → WS callbacks unlocked

### 6.2 tick() — Candle Close Processing

**Where:** `strategy.py::tick()` L385

**Triggered by:** WS candle close callback OR REST fallback.

```
tick(candles)
│
├─ Guard: enough candles? (ATR_LENGTH + PIVOT_LENGTH + 10)
├─ Guard: new candle? (closed_t != _last_candle_t)
│
├─ bar_counter++
├─ closed_candles = candles[:-1]     ← exclude forming candle
│
├─ 1. _update_indicators(closed_candles)
│     ├─ Pivot high → bearOB = candles[pivot_idx]["h"]
│     ├─ Pivot low  → bullOB = candles[pivot_idx]["l"]
│     ├─ Bullish FVG: candles[-1].l > candles[-3].h
│     └─ Bearish FVG: candles[-1].h < candles[-3].l
│
├─ 2. _sync_position(closed_candles)
│     ├─ Exchange=NONE, State=IN → position closed externally → cleanup
│     └─ Exchange=HAS, State=FLAT → orphan → close immediately
│
├─ 3. IF in_position → _manage_position_on_candle()
│     ├─ Verify position still exists
│     └─ Sync exchange SL if trailing moved
│
└─ 4. IF flat → _check_entries(closed_candles)
      └─ (see Section 6.4)
```

### 6.3 on_price_update() — Real-Time SL Management

**Where:** `strategy.py::on_price_update()` L784

**Triggered by:** Every AllMids WS price tick (~1-5s).

**Guards:** `_warmup_done` AND `in_position` AND NOT `_lock`.

```
on_price_update(price)
│
├─ 1. EMERGENCY CHECK: price past SL?
│     ├─ YES, first time → start timer, log warning
│     ├─ YES, ≥10s → EMERGENCY CLOSE (return _close_and_handle coroutine)
│     ├─ YES, <10s → wait (return None)
│     └─ NO → reset timer
│
├─ 2. BREAKEVEN CHECK:
│     ├─ Long: price ≥ entry + sl_distance → SL = max(initial_sl, zone_level - buffer)
│     └─ Short: price ≤ entry - sl_distance → SL = min(initial_sl, zone_level + buffer)
│
├─ 3. TRAILING SL:
│     ├─ trail_activation = sl_distance × RR_RATIO × 0.3
│     ├─ trail_offset = sl_distance × 0.2
│     ├─ Long: price ≥ entry + trail_activation → SL = price - trail_offset
│     └─ Short: price ≤ entry - trail_activation → SL = price + trail_offset
│
└─ 4. IF SL changed:
      ├─ _save_state()
      └─ Throttled (15s min): return _update_exchange_sl() coroutine
```

**Return values:**
- `None` — no action needed (WS manager ignores)
- Coroutine — WS manager schedules on asyncio loop via `run_coroutine_threadsafe`

### 6.4 Entry Signal Logic

**Where:** `strategy.py::_check_entries()` L598

**Input:** `closed_candles` — all candles up to last closed (forming excluded).

```
c = closed_candles[-1]   ← the just-closed candle

ATR = compute_atr(candles, 14)     # Wilder's RMA
EMA50 = compute_ema(closes, 50)    # Standard EMA
sl_distance = 1.5 × ATR

┌─────────────── ORDER BLOCK CONDITIONS ──────────────────┐
│ OB Long:  bullOB > 0 AND c.low < bullOB AND c.close > bullOB    │
│ OB Short: bearOB > 0 AND c.high > bearOB AND c.close < bearOB   │
└──────────────────────────────────────────────────────────┘

┌─────────────── FVG CONDITIONS ──────────────────────────┐
│ Valid: (bar_counter - fvg_bar) ≤ 10                     │
│                                                          │
│ FVG Long:  bull_fvg valid                               │
│   AND c.low ≤ bull_fvg_top                              │
│   AND c.close > bull_fvg_bottom                         │
│   AND c.close > c.open (bullish candle)                 │
│   AND c.close > EMA50                                   │
│                                                          │
│ FVG Short: bear_fvg valid                               │
│   AND c.high ≥ bear_fvg_bottom                          │
│   AND c.close < bear_fvg_top                            │
│   AND c.close < c.open (bearish candle)                 │
│   AND c.close < EMA50                                   │
└──────────────────────────────────────────────────────────┘

┌─────────────── STRONG CANDLE FILTER ────────────────────┐
│ body = |close - open|                                    │
│ threshold = ATR × 0.35                                   │
│                                                          │
│ Strong Bull: close > open AND body > threshold           │
│ Strong Bear: close < open AND body > threshold           │
└──────────────────────────────────────────────────────────┘

FINAL:
  Long  = (OB_long OR FVG_long)  AND Strong_bull
  Short = (OB_short OR FVG_short) AND Strong_bear
```

### 6.5 Position Exit Paths

There are **5 distinct ways** a position can close:

| # | Path | Trigger | Speed | Code Location |
|---|------|---------|-------|---------------|
| 1 | **Exchange SL/TP** | HL exchange trigger fires | ~instant | Detected by `OrderUpdates` WS → `on_order_triggered()` |
| 2 | **Candle sync** | `_sync_position()` sees no position | ~15min max | `tick()` → `_sync_position()` |
| 3 | **Emergency close** | Price past SL for >10s | 10s delay | `on_price_update()` → `_close_and_handle()` |
| 4 | **Red Button** | 60s price silence + position | 60s | `run.py` main loop |
| 5 | **Orphan close** | State=FLAT but exchange has position | On tick | `_sync_position()` or warmup |

**Normal flow:** Exchange SL/TP (path 1) fires → `on_order_triggered()` detects immediately → cancel remaining orders → verify no reverse → `_on_position_closed()`.

**Backup flow if WS misses order fill:** Next candle close → `_sync_position()` reads exchange → no position → `_on_position_closed()`.

---

## 7. Position Sync (Startup + Runtime)

### Startup Sync (in `warmup()`)

After warmup indicators are computed, **4 scenarios** are checked:

```
Exchange position?  │  State says in_position?  │  Action
─────────────────────┼──────────────────────────┼──────────────────────
     NO              │         YES               │  Clear state (closed while offline)
     YES             │         NO                │  CLOSE orphan (market_close)
     YES             │  YES, wrong side          │  CLOSE + clear state
     YES             │  YES, size differs >10%   │  Update size, log warning
     YES             │  YES, matches             │  Log verified, continue
     NO              │         NO                │  Nothing (normal FLAT)
```

### Runtime Sync (in `tick()` → `_sync_position()`)

Called on **every candle close**:

```
Exchange position?  │  State says in_position?  │  Action
─────────────────────┼──────────────────────────┼──────────────────────
     NO              │         YES               │  Position closed externally → cleanup
     YES             │         NO                │  ALWAYS orphan → close immediately
     YES             │         YES               │  Normal (no action)
     NO              │         NO                │  Normal (no action)
```

**Design rule:** Bot NEVER "adopts" unknown positions. If state says FLAT and exchange has a position, it's a ghost from a race condition — close it.

---

## 8. Emergency Close Pipeline

**Where:** `strategy.py::_close_and_handle()` L948

```
_close_and_handle(exit_price)
│
├─ Guard: _lock (prevent re-entrance)
│
├─ 1. market_close(symbol)          ← reduce-only via SDK, FASTEST path
├─ 2. cancel_all_orders(symbol)     ← kill orphan SL/TP triggers
├─ 3. sleep(1.0)                    ← wait for settlement
├─ 4. invalidate_cache()
├─ 5. _verify_no_reverse_position() ← 3 retries with fresh reads
│     └─ If reverse found → market_close again
├─ 6. cancel_all_orders(symbol)     ← catch any race condition orders
└─ 7. _on_position_closed([], exit_price)
```

**Why this order?** Close first, clean up after. The exchange SL trigger might fire at any moment during our close operation — creating a reverse position. That's why step 5 exists.

---

## 9. Order Lifecycle on Exchange

### Entry
```
_enter_position()
  → trader.open_position(symbol, side, size, sl, tp)
    → exchange.update_leverage(5, symbol)
    → exchange.order(symbol, is_buy, size, limit_px, {limit: {tif: "Ioc"}})
    → wait fill
    → _place_trigger(symbol, sl, tpsl="sl")    ← Stop Market, reduce-only
    → _place_trigger(symbol, tp, tpsl="tp")    ← Take Profit Market, reduce-only
```

### Trailing SL Update
```
on_price_update() detects SL moved
  → _update_exchange_sl()
    → trader.replace_sl_tp(symbol, size, side, new_sl, tp)
      → cancel_all_orders(symbol)       ← kill old SL+TP
      → _place_trigger(new_sl, "sl")    ← new SL
      → _place_trigger(tp, "tp")        ← re-place same TP
```

**Throttled:** Minimum 15s between exchange SL updates. `_last_exchange_sl` deduplicates same-value updates.

### Exit (by exchange)
```
Exchange fills SL/TP trigger
  → OrderUpdates WS fires
  → on_order_triggered()
    → cancel_all_orders()    ← remove remaining trigger
    → verify_no_reverse()
    → _on_position_closed()
```

---

## 10. State Persistence

### TradeState (`data/state_ETH.json`)

```json
{
  "in_position": true,
  "position_side": "long",
  "entry_price": 1941.6,
  "entry_size": 0.0298,
  "entry_source": "FVG",
  "zone_level": 1936.8,
  "initial_sl": 1925.05,
  "current_sl": 1930.2,
  "take_profit": 1995.4,
  "sl_distance": 16.75,
  "breakeven_applied": true,
  "trailing_active": false,
  "entry_time": 1771534702.617,
  "current_risk": 3.61,
  "last_exit_bar": 311,
  "bull_ob": 1910.1,
  "bear_ob": 1947.0,
  "bull_fvg_top": 1937.3,
  "bull_fvg_bottom": 1936.8,
  "bull_fvg_bar": 310,
  "bear_fvg_top": 0.0,
  "bear_fvg_bottom": 0.0,
  "bear_fvg_bar": 0,
  "total_trades": 5,
  "wins": 2,
  "losses": 3,
  "bar_counter": 312
}
```

**When saved:** After warmup, after position open, after SL move, after position close, after risk adjustment.

**When loaded:** On `StrategyEngine.__init__()` — before warmup. Warmup overwrites OB/FVG/bar_counter but preserves position + risk state.

### Trade Log (`data/trades.json`)

Each trade appends:
```json
{
  "symbol": "ETH",
  "side": "long",
  "entry_source": "FVG",
  "entry_price": 1941.6,
  "exit_price": 1960.3,
  "size": 0.0298,
  "pnl_pct": 0.89,
  "result": "win",
  "risk_after": 4.0,
  "entry_time": 1771534702.617,
  "exit_time": 1771548887.62,
  "ema_deviation_pct": -0.041,
  "atr_pct": 0.575,
  "ema50_at_entry": 1941.0
}
```

Max 500 entries. Oldest trimmed on save.

---

## 11. Risk Management

### Smooth Dynamic Risk (mirrors Pine Script)

```
Starting risk:    5%
Min risk:         2%
Max risk:        10%
Adjustment rate: 10% per trade

After WIN:  risk *= 1.10   (max 10%)
After LOSS: risk *= 0.90   (min 2%)

Position size = (equity × risk%) / sl_distance
  Clamped to: [$11 min notional, $100 max notional, 95% equity cap]
```

### SL/TP Calculation

```
ATR = RMA(True Range, 14)            # Wilder's smoothing
sl_distance = ATR × 1.5

Long:
  SL = entry - sl_distance
  TP = entry + sl_distance × 3.2

Short:
  SL = entry + sl_distance
  TP = entry - sl_distance × 3.2
```

### Breakeven Logic

```
# Triggered when price moves 1× sl_distance in profit direction
Long:  price ≥ entry + sl_distance → SL = max(initial_sl, zone_level - 2% buffer)
Short: price ≤ entry - sl_distance → SL = min(initial_sl, zone_level + 2% buffer)
```

### Trailing Stop

```
trail_activation = sl_distance × 3.2 × 0.3   (30% of TP distance)
trail_offset     = sl_distance × 0.2

Long:  price ≥ entry + trail_activation → SL = price - trail_offset
Short: price ≤ entry - trail_activation → SL = price + trail_offset
```

---

## 12. Threading Model

```
┌────────────────────────────┐    ┌─────────────────────────────────┐
│      WS THREAD             │    │       ASYNCIO LOOP (main)       │
│  (hyperliquid SDK)         │    │                                 │
│                            │    │                                 │
│  _on_all_mids() ───────────┼──► │  on_price_update() → sync      │
│    → callbacks             │    │    returns coro? → schedule     │
│                            │    │                                 │
│  _on_candle_msg() ─────────┼──► │  tick() → async                │
│    → candle close detect   │    │    → _sync_position()          │
│    → callbacks             │    │    → _check_entries()           │
│                            │    │    → open_position()            │
│  _on_order_update() ───────┼──► │  on_order_triggered() → async  │
│    → callbacks             │    │    → _close_and_handle()        │
│                            │    │                                 │
└────────────────────────────┘    │  main loop (run.py):            │
                                  │    → REST fallback              │
                                  │    → WS health check            │
                                  │    → heartbeat                  │
                                  └─────────────────────────────────┘
```

**Thread safety:**
- `on_price_update()` is **sync** — runs on WS thread, reads state atomically
- If it returns a coroutine → WS manager uses `run_coroutine_threadsafe()` to schedule on asyncio loop
- `_lock` flag prevents re-entrance (emergency close + SL update + order triggered racing)
- `seed_candles()` — atomic list replacement + in-place update of old buffer reference
- REST cache (`_user_state_cache`) — single-threaded access from asyncio loop only

---

## 13. Key Constants & Thresholds

| Constant | Value | Where | Purpose |
|----------|-------|-------|---------|
| `TIMEFRAME` | `15m` | settings.py | Candle interval |
| `LEVERAGE` | `5x` | settings.py | HyperLiquid leverage |
| `ATR_LENGTH` | `14` | settings.py | ATR calculation period |
| `SL_MULTIPLIER` | `1.5` | settings.py | SL = 1.5× ATR |
| `RR_RATIO` | `3.2` | settings.py | TP = 3.2× SL distance |
| `PIVOT_LENGTH` | `6` | settings.py | Pivot detection lookback |
| `FVG_LOOKBACK` | `10` | settings.py | FVG validity in bars |
| `STRONG_CANDLE_ATR_RATIO` | `0.35` | settings.py | Body > 35% ATR for entry |
| `TRAIL_ACTIVATION_RATIO` | `0.3` | settings.py | 30% of TP distance |
| `TRAIL_OFFSET_RATIO` | `0.2` | settings.py | 20% of SL distance |
| `COOLDOWN_BARS` | `0` | settings.py | Bars to wait after exit |
| `CANDLE_FETCH_LIMIT` | `300` | settings.py | REST candles to fetch |
| `LOOP_INTERVAL` | `15s` | settings.py | Main loop sleep |
| `WS_CHECK_INTERVAL` | `15s` | run.py | WS health check frequency |
| `WS_STALE_THRESHOLD` | `45s` | run.py | WS considered dead after |
| `EMERGENCY_REST_INTERVAL` | `3s` | run.py | REST polling when WS dead |
| `SILENCE_CLOSE_THRESHOLD` | `60s` | run.py | Red button: close all |
| Emergency SL timer | `10s` | strategy.py | Force close if price past SL |
| SL update throttle | `15s` | strategy.py | Min interval between exchange SL updates |
| user_state cache TTL | `5s` | trader.py | REST cache for positions/equity |
| meta cache TTL | `300s` | trader.py | REST cache for asset metadata |
| WS backoff | `2-30s` | run.py | Exponential reconnect backoff |
| WS recovery pongs | `3` | run.py | WS pongs before exiting emergency |

---

## Appendix: Complete Call Graph

```
run.py::main()
  ├─ StrategyEngine(sym, trader, notifier)
  │     └─ _load_state()
  │
  ├─ ws.start()
  │     ├─ subscribe(AllMids)      → _on_all_mids       → on_price_update()
  │     ├─ subscribe(OrderUpdates) → _on_order_update    → on_order_triggered()
  │     └─ subscribe(Candle)       → _on_candle_msg      → tick()
  │
  ├─ warmup(candles)
  │     ├─ _update OB/FVG from history
  │     ├─ position sync (4 scenarios)
  │     └─ _warmup_done = True
  │
  └─ main loop (15s)
        ├─ REST fallback → tick()
        ├─ WS health → reconnect / emergency / red button
        └─ heartbeat log
  
tick(candles)
  ├─ _update_indicators(closed_candles)
  ├─ _sync_position(closed_candles)
  ├─ _manage_position_on_candle()         [if in_position]
  └─ _check_entries(closed_candles)       [if flat]
        └─ _enter_position()
              └─ trader.open_position()

on_price_update(price)
  ├─ emergency check → _close_and_handle()
  ├─ breakeven logic
  ├─ trailing SL logic
  └─ _update_exchange_sl()
        └─ trader.replace_sl_tp()

on_order_triggered(data)
  ├─ cancel_all_orders()
  ├─ _verify_no_reverse_position()
  └─ _on_position_closed()
        ├─ risk adjustment
        ├─ _save_trade()
        ├─ Telegram notification
        └─ _save_state()
  
_close_and_handle(exit_price)
  ├─ exchange.market_close()
  ├─ cancel_all_orders()
  ├─ _verify_no_reverse_position()
  ├─ cancel_all_orders()
  └─ _on_position_closed()
```
