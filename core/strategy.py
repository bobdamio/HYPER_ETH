"""
HYPER_ETH — Strategy Engine
Exact translation of ETH_main.pine (OB + FVG + Smooth Dynamic Risk).One instance per symbol."""

import asyncio
import datetime
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict

from config.settings import STRATEGY, RISK, GLOBAL
from core.trader import HLTrader
from core.notifier import Notifier

logger = logging.getLogger("Strategy")

# ────────────────────────────────────────────────────────────
#                      DATA CLASSES
# ────────────────────────────────────────────────────────────


@dataclass
class OrderBlock:
    """A detected pivot-based Order Block."""
    price: float          # bullOB = low[pivotLen], bearOB = high[pivotLen]
    side: str             # 'bull' or 'bear'
    bar_index: int        # bar where OB was detected
    valid: bool = True


@dataclass
class FVG:
    """A detected Fair Value Gap."""
    top: float            # bullFVG: low[0], bearFVG: low[2]
    bottom: float         # bullFVG: high[2], bearFVG: high[0]
    side: str             # 'bull' or 'bear'
    bar_index: int
    valid: bool = True


@dataclass
class TradeState:
    """Persistent state across strategy ticks."""
    in_position: bool = False
    position_side: str = ""          # 'long' or 'short'
    entry_price: float = 0.0
    entry_size: float = 0.0
    entry_source: str = ""           # 'OB' or 'FVG'
    zone_level: float = 0.0         # OB or FVG level for breakeven
    initial_sl: float = 0.0
    current_sl: float = 0.0
    take_profit: float = 0.0
    sl_distance: float = 0.0
    breakeven_applied: bool = False
    trailing_active: bool = False
    trailing_sl: float = 0.0          # Internal trailing SL (NOT on exchange)
    trailing_peak: float = 0.0        # Highest high (long) / lowest low (short) since trail activation
    exit_reason: str = ""             # SL/TP/trailing/breakeven/emergency/sync
    entry_time: float = 0.0

    # Smooth dynamic risk
    current_risk: float = RISK.BASE_RISK_PCT

    # Cooldown
    last_exit_bar: int = 0
    last_exit_t: int = 0             # timestamp (ms) of last exit candle

    # Order Blocks (latest detected)
    bull_ob: float = 0.0
    bear_ob: float = 0.0

    # FVGs (latest detected)
    bull_fvg_top: float = 0.0
    bull_fvg_bottom: float = 0.0
    bull_fvg_bar: int = 0
    bull_fvg_t: int = 0               # timestamp (ms) when bull FVG detected
    bear_fvg_top: float = 0.0
    bear_fvg_bottom: float = 0.0
    bear_fvg_bar: int = 0
    bear_fvg_t: int = 0               # timestamp (ms) when bear FVG detected

    # Trade counter
    total_trades: int = 0
    wins: int = 0
    losses: int = 0

    # Bar counter (persisted for FVG validity and cooldown)
    bar_counter: int = 0
    last_bar_t: int = 0               # timestamp (ms) of last processed candle

    # ── Pending exit levels (set at candle close, active during next bar) ──
    # These mirror Pine's strategy.exit() standing orders.
    pending_sl: float = 0.0
    pending_tp: float = 0.0
    pending_trail_activation: float = 0.0   # price level where trail activates
    pending_trail_offset: float = 0.0       # trail offset in price $


# ────────────────────────────────────────────────────────────
#                    INDICATOR HELPERS
# ────────────────────────────────────────────────────────────


def compute_atr(candles: List[dict], length: int = 14) -> float:
    """ATR(length) — True Range averaged over `length` bars."""
    if len(candles) < length + 1:
        return 0.0

    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["h"]
        l = candles[i]["l"]
        pc = candles[i - 1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    # Simple RMA (Wilder's smoothing) — matches Pine's ta.atr
    if len(trs) < length:
        return sum(trs) / len(trs) if trs else 0.0

    # Seed with SMA of first `length` values
    atr = sum(trs[:length]) / length
    for tr in trs[length:]:
        atr = (atr * (length - 1) + tr) / length
    return atr


def compute_ema(values: List[float], length: int) -> float:
    """EMA(length) of a list of float values. Returns last EMA value."""
    if not values or len(values) < length:
        return values[-1] if values else 0.0

    k = 2 / (length + 1)
    ema = sum(values[:length]) / length  # seed with SMA
    for v in values[length:]:
        ema = v * k + ema * (1 - k)
    return ema


def detect_pivot_high(candles: List[dict], left: int, right: int) -> Optional[int]:
    """
    Detect pivot high at index [-(right+1)] looking left and right.
    Returns bar_index (list index) of the pivot, or None.
    Pine: ta.pivothigh(high, left, right) — confirmed `right` bars after.
    """
    if len(candles) < left + right + 1:
        return None

    pivot_idx = len(candles) - 1 - right  # the candidate bar
    pivot_high = candles[pivot_idx]["h"]

    # Check left bars (Pine: pivot must be STRICTLY higher than all neighbors)
    for i in range(1, left + 1):
        if candles[pivot_idx - i]["h"] >= pivot_high:
            return None
    # Check right bars
    for i in range(1, right + 1):
        if candles[pivot_idx + i]["h"] >= pivot_high:
            return None

    return pivot_idx


def detect_pivot_low(candles: List[dict], left: int, right: int) -> Optional[int]:
    """Detect pivot low at index [-(right+1)]."""
    if len(candles) < left + right + 1:
        return None

    pivot_idx = len(candles) - 1 - right
    pivot_low = candles[pivot_idx]["l"]

    # Pine: pivot must be STRICTLY lower than all neighbors
    for i in range(1, left + 1):
        if candles[pivot_idx - i]["l"] <= pivot_low:
            return None
    for i in range(1, right + 1):
        if candles[pivot_idx + i]["l"] <= pivot_low:
            return None

    return pivot_idx


# ────────────────────────────────────────────────────────────
#                    STRATEGY ENGINE
# ────────────────────────────────────────────────────────────


class StrategyEngine:
    """
    Full OB + FVG strategy — exact Pine Script translation.
    One instance per symbol.

    Lifecycle per tick:
      1. Update indicators (ATR, EMA50, OB/FVG detection)
      2. If in position → manage exit (breakeven, trailing)
      3. If flat → check entry signals → open if valid
    """

    def __init__(self, symbol: str, trader: HLTrader, notifier: Notifier):
        self.symbol = symbol
        self.trader = trader
        self.notifier = notifier
        self.state = TradeState()
        self.STATE_FILE = os.path.join(GLOBAL.DATA_DIR, f"state_{symbol}.json")
        self._load_state()
        self._last_candle_t = 0
        self._bar_ms = STRATEGY.BAR_INTERVAL_MS  # 1h = 3600000ms
        self._warmup_done = False  # Blocks all WS callbacks until warmup + sync complete
        self._lock = False  # Simple reentrance guard for async operations
        self._emergency_first_seen = 0.0  # Emergency backup timer
        self._exit_was_intra_bar = False  # True when SL/TP fired between candle closes
        self._last_sl_exchange_update = 0.0  # Throttle: last time we updated exchange SL
        self._sl_update_interval = 15  # Min seconds between exchange SL updates (breakeven only; trailing is on candle close)
        self._last_exchange_sl = 0.0  # Last SL value sent to exchange (dedup + race prevention)
        self._fill_cache: Dict[int, dict] = {}  # oid → fill data from UserFills WS
        self._emergency_timeout = 30  # Seconds past exchange SL before emergency close

    def _bars_between(self, t1: int, t2: int) -> int:
        """Compute number of bars between two timestamps (ms)."""
        if t1 <= 0 or t2 <= 0:
            return 9999  # treat missing timestamps as very old
        return abs(t2 - t1) // self._bar_ms

    # ── persistence ──────────────────────────────────────────
    def _load_state(self):
        os.makedirs(GLOBAL.DATA_DIR, exist_ok=True)
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE) as f:
                    data = json.load(f)
                for k, v in data.items():
                    if hasattr(self.state, k):
                        setattr(self.state, k, v)
                logger.info(f"State loaded [{self.symbol}]: risk={self.state.current_risk:.2f}%, trades={self.state.total_trades}")
            except Exception as e:
                logger.warning(f"Failed to load state: {e}")

    def _save_state(self):
        try:
            with open(self.STATE_FILE, "w") as f:
                json.dump(asdict(self.state), f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    # ── main tick ────────────────────────────────────────────
    async def warmup(self, candles: List[dict]):
        """
        Historical warmup — replay all candles bar-by-bar like Pine Script does.
        Populates OB, FVG levels from history so we don't wait hours for signals.
        Only runs on first startup (skipped if state already has bar_counter > 0).
        """
        # Always run warmup to populate OB/FVG levels — even if in_position
        # (position may close at any time, and we need levels for new entries)

        s = self.state
        plen = STRATEGY.PIVOT_LENGTH
        min_bars = plen * 2  # need at least pivotLen*2 bars for pivot detection

        ob_count = 0
        fvg_count = 0

        # Walk through candles sequentially, like Pine processes each bar
        # Stop at len-1: candles[-1] is the forming (unclosed) candle — skip it
        for i in range(min_bars, len(candles) - 1):
            bar_num = i  # use candle index as bar number during warmup
            window = candles[:i + 1]  # candles[0..i] — all bars up to current

            # ── Pivot-based Order Blocks ──
            # Check if there's a pivot at position [i - plen] confirmed by plen bars on each side
            if i >= plen * 2:
                candidate = i - plen
                is_pivot_high = True
                is_pivot_low = True

                for j in range(1, plen + 1):
                    if candles[candidate - j]["h"] >= candles[candidate]["h"]:
                        is_pivot_high = False
                    if candles[candidate + j]["h"] >= candles[candidate]["h"]:
                        is_pivot_high = False
                    if candles[candidate - j]["l"] <= candles[candidate]["l"]:
                        is_pivot_low = False
                    if candles[candidate + j]["l"] <= candles[candidate]["l"]:
                        is_pivot_low = False

                if is_pivot_high:
                    s.bear_ob = candles[candidate]["h"]
                    ob_count += 1
                if is_pivot_low:
                    s.bull_ob = candles[candidate]["l"]
                    ob_count += 1

            # ── FVG detection ──
            if STRATEGY.USE_FVG and i >= 2:
                c0 = candles[i]
                c2 = candles[i - 2]

                if c0["l"] > c2["h"]:  # Bullish FVG
                    s.bull_fvg_top = c0["l"]
                    s.bull_fvg_bottom = c2["h"]
                    s.bull_fvg_bar = bar_num
                    s.bull_fvg_t = candles[i]["t"]
                    fvg_count += 1

                if c0["h"] < c2["l"]:  # Bearish FVG
                    s.bear_fvg_top = c2["l"]
                    s.bear_fvg_bottom = c0["h"]
                    s.bear_fvg_bar = bar_num
                    s.bear_fvg_t = candles[i]["t"]
                    fvg_count += 1

        # Set bar_counter to match closed candle count (excluding forming candle)
        s.bar_counter = len(candles) - 1
        # Store last CLOSED candle timestamp (candles[-2], since candles[-1] is forming)
        # This ensures the first WS candle close is detected as a new candle
        last_closed_t = candles[-2]["t"] if len(candles) >= 2 else candles[-1]["t"]
        self._last_candle_t = last_closed_t
        s.last_bar_t = last_closed_t

        # Fix cooldown: use timestamps if available, fall back to bar_counter
        if s.last_exit_t > 0:
            # Timestamps survive restart — no reset needed
            pass
        elif s.last_exit_bar >= s.bar_counter:
            old_exit_bar = s.last_exit_bar
            s.last_exit_bar = max(0, s.bar_counter - 1)
            logger.info(
                f"[{self.symbol}] Cooldown reset: last_exit_bar {old_exit_bar} → {s.last_exit_bar} "
                f"(was ahead of bar_counter={s.bar_counter})"
            )

        self._save_state()

        # Sync loaded state with actual exchange (position may have closed while bot was down)
        actual_pos = self.trader.get_position(self.symbol)
        if actual_pos is None and s.in_position:
            logger.warning(
                f"[{self.symbol}] State says in_position but exchange has no position — clearing state"
            )
            s.in_position = False
            s.position_side = ""
            s.entry_price = 0.0
            s.entry_size = 0.0
            s.current_sl = 0.0
            s.take_profit = 0.0
            self._last_exchange_sl = 0.0
            self._save_state()
        elif actual_pos is not None and not s.in_position:
            # Exchange has a position but our state says FLAT.
            # This is an orphan from crash/race condition — close it immediately.
            szi = float(actual_pos.get("szi", 0))
            entry_px = float(actual_pos.get("entryPx", 0))
            side_str = "long" if szi > 0 else "short"
            logger.warning(
                f"[{self.symbol}] Exchange has {side_str} {abs(szi)} @ {entry_px} "
                f"but state says FLAT — closing orphan position on startup"
            )
            try:
                await self.trader.cancel_all_orders(self.symbol)
                await asyncio.to_thread(
                    self.trader.exchange.market_close, self.symbol
                )
                logger.info(f"✅ [{self.symbol}] Orphan position closed on startup.")
            except Exception as e:
                logger.error(f"❌ [{self.symbol}] Failed to close orphan on startup: {e}")
        elif actual_pos is not None and s.in_position:
            # Both agree there's a position — verify side and size match
            szi = float(actual_pos.get("szi", 0))
            actual_side = "long" if szi > 0 else "short"
            actual_size = abs(szi)
            if actual_side != s.position_side:
                logger.error(
                    f"[{self.symbol}] Side mismatch! State={s.position_side} Exchange={actual_side} "
                    f"— closing and clearing state"
                )
                try:
                    await self.trader.cancel_all_orders(self.symbol)
                    await asyncio.to_thread(
                        self.trader.exchange.market_close, self.symbol
                    )
                except Exception as e:
                    logger.error(f"[{self.symbol}] Failed to close mismatched position: {e}")
                s.in_position = False
                s.position_side = ""
                s.entry_price = 0.0
                s.entry_size = 0.0
                s.current_sl = 0.0
                s.take_profit = 0.0
                self._last_exchange_sl = 0.0
                self._save_state()
            elif abs(actual_size - s.entry_size) / max(s.entry_size, 1e-9) > 0.1:
                # Size differs by >10% — log warning but keep running
                logger.warning(
                    f"[{self.symbol}] Size mismatch: state={s.entry_size} exchange={actual_size} "
                    f"— updating to exchange value"
                )
                s.entry_size = actual_size
                self._save_state()
            else:
                logger.info(
                    f"[{self.symbol}] Position verified: {s.position_side} @ {s.entry_price} | "
                    f"SL={s.current_sl:.2f} TP={s.take_profit:.2f}"
                )

        # NOW safe to allow WS callbacks (on_price_update, etc.)
        self._warmup_done = True

        bull_age = self._bars_between(s.bull_fvg_t, s.last_bar_t) if s.bull_fvg_t > 0 else (s.bar_counter - s.bull_fvg_bar)
        bear_age = self._bars_between(s.bear_fvg_t, s.last_bar_t) if s.bear_fvg_t > 0 else (s.bar_counter - s.bear_fvg_bar)
        logger.info(
            f"[{self.symbol}] Warmup complete: {len(candles)} bars | "
            f"OBs detected: {ob_count} | FVGs detected: {fvg_count} | "
            f"bull_ob={s.bull_ob:.2f} | bear_ob={s.bear_ob:.2f} | "
            f"bull_fvg=[{s.bull_fvg_bottom:.2f}-{s.bull_fvg_top:.2f}] (age={bull_age}) | "
            f"bear_fvg=[{s.bear_fvg_bottom:.2f}-{s.bear_fvg_top:.2f}] (age={bear_age})"
        )

        # Place exchange SL/TP safety net for existing position
        if s.in_position and s.pending_sl > 0:
            await self._update_exchange_sltp()

    async def tick(self, candles: List[dict]):
        """
        Called on each new closed candle (detected by run.py or polling fallback).
        HL API returns current unclosed candle as candles[-1].
        We use candles[-2] (last CLOSED candle) for signals/indicators.

        All exits are evaluated at candle close only (Pine: process_orders_on_close=true).
        SL/TP checked intra-bar via on_price_update(). Trailing stop checked on
        candle close only (in _manage_position_on_candle) to match Pine behavior.
        """
        if len(candles) < STRATEGY.ATR_LENGTH + STRATEGY.PIVOT_LENGTH + 10:
            logger.warning(f"[{self.symbol}] Not enough candles ({len(candles)})")
            return

        # The last CLOSED candle is candles[-2] (candles[-1] is still forming)
        closed_t = candles[-2]["t"]
        is_new_candle = closed_t != self._last_candle_t

        if not is_new_candle:
            return  # No new candle — WS handles inter-candle monitoring

        self._last_candle_t = closed_t
        self.state.bar_counter += 1
        self.state.last_bar_t = closed_t

        # Use all candles up to and including last closed (exclude current unclosed)
        closed_candles = candles[:-1]

        # 1. Update indicators (on closed candles only)
        self._update_indicators(closed_candles)

        # 2. Sync position from exchange
        should_continue = await self._sync_position(closed_candles)
        if not should_continue:
            return  # orphan closed — skip signals this candle

        # 3. If in position → update exchange SL/TP on candle close
        if self.state.in_position:
            await self._manage_position_on_candle(closed_candles)
            return

        # 4. If flat → check entry signals
        await self._check_entries(closed_candles)
        # Entry fills at bar close. Exit orders placed by _enter_position() are
        # pending — they get checked starting from the NEXT bar (bar N+1).
        # No OHLC check on the entry bar: H and L happened BEFORE entry.

    # ── indicator update ─────────────────────────────────────
    def _update_indicators(self, candles: List[dict]):
        """Update OB, FVG, ATR, EMA50 from candles."""
        s = self.state
        plen = STRATEGY.PIVOT_LENGTH

        # Order Block detection
        # Pine: ph = ta.pivothigh(high, pivotLen, pivotLen)
        ph_idx = detect_pivot_high(candles, plen, plen)
        if ph_idx is not None:
            s.bear_ob = candles[ph_idx]["h"]  # bearOB := high[pivotLen]

        pl_idx = detect_pivot_low(candles, plen, plen)
        if pl_idx is not None:
            s.bull_ob = candles[pl_idx]["l"]  # bullOB := low[pivotLen]

        # FVG detection
        if STRATEGY.USE_FVG and len(candles) >= 3:
            c0 = candles[-1]  # current bar
            c2 = candles[-3]  # bar [2]

            # Bullish FVG: low > high[2]
            if c0["l"] > c2["h"]:
                s.bull_fvg_top = c0["l"]
                s.bull_fvg_bottom = c2["h"]
                s.bull_fvg_bar = s.bar_counter
                s.bull_fvg_t = c0["t"]

            # Bearish FVG: high < low[2]
            if c0["h"] < c2["l"]:
                s.bear_fvg_top = c2["l"]
                s.bear_fvg_bottom = c0["h"]
                s.bear_fvg_bar = s.bar_counter
                s.bear_fvg_t = c0["t"]

    # ── position sync ────────────────────────────────────────
    async def _sync_position(self, candles: List[dict]) -> bool:
        """Sync state.in_position with actual exchange position.
        Returns True if tick() should continue to signal generation,
        False if an orphan was found/closed (skip signals this candle).
        """
        pos = self.trader.get_position(self.symbol)

        if pos is None and self.state.in_position:
            # Position was closed externally (SL/TP hit)
            logger.info(f"[{self.symbol}] Position closed externally (SL/TP hit).")
            # Cancel remaining orders (HL does NOT auto-cancel on position close)
            try:
                await self.trader.cancel_all_orders(self.symbol)
                logger.info(f"[{self.symbol}] Cancelled remaining orders after position close.")
            except Exception as e:
                logger.error(f"[{self.symbol}] Failed to cancel orders after close: {e}")
            await self._on_position_closed(candles)
            return True

        elif pos is not None and not self.state.in_position:
            # Position exists but state says flat — this is ALWAYS an orphan.
            # Bot only creates positions through _enter_position() which sets state.
            # If state is flat, this position came from a race condition, crash, or manual trade.
            szi = float(pos["szi"])
            entry_px = float(pos["entryPx"])
            size = abs(szi)
            side_str = "long" if szi > 0 else "short"
            
            logger.warning(
                f"[{self.symbol}] Orphan position detected: {side_str} {size} @ {entry_px} "
                f"(state=FLAT) — closing immediately"
            )
            await self.trader.cancel_all_orders(self.symbol)
            try:
                await asyncio.to_thread(
                    self.trader.exchange.market_close, self.symbol
                )
                logger.info(f"✅ [{self.symbol}] Orphan position closed.")
            except Exception as e:
                logger.error(f"❌ [{self.symbol}] Failed to close orphan: {e}")
            # Skip signal generation this candle — position may still be settling
            return False

        return True

    async def _on_position_closed(self, candles: List[dict], exit_price: float = None):
        """Handle position closure — adjust risk, save trade."""
        s = self.state
        # Determine exit price: explicit > candle close > entry price (last resort)
        if exit_price is not None and exit_price > 0:
            last_price = exit_price
        elif candles:
            last_price = candles[-1]["c"]
        else:
            last_price = s.entry_price  # fallback: PnL = 0

        # Determine PnL from real exchange data if possible
        pos = self.trader.get_position(self.symbol)
        if pos and "unrealizedPnl" in pos:
            pnl_usd = float(pos["unrealizedPnl"])
        else:
            if s.position_side == "long":
                pnl_usd = (last_price - s.entry_price) * s.entry_size
            else:
                pnl_usd = (s.entry_price - last_price) * s.entry_size

        is_win = pnl_usd > 0
        pnl_pct = ((last_price - s.entry_price) / s.entry_price * 100) if s.position_side == "long" else ((s.entry_price - last_price) / s.entry_price * 100)

        # Smooth risk adjustment (mirrors Pine)
        adj = RISK.ADJUSTMENT_RATE / 100
        if is_win:
            s.current_risk *= (1 + adj)
            if s.current_risk > RISK.MAX_RISK_PCT:
                s.current_risk = RISK.MAX_RISK_PCT
            s.wins += 1
        else:
            s.current_risk *= (1 - adj)
            if s.current_risk < RISK.MIN_RISK_PCT:
                s.current_risk = RISK.MIN_RISK_PCT
            s.losses += 1

        s.total_trades += 1
        result_emoji = "🟢" if is_win else "🔴"

        # Save trade record
        self._save_trade({
            "symbol": self.symbol,
            "side": s.position_side,
            "entry_source": s.entry_source,
            "entry_price": s.entry_price,
            "exit_price": last_price,
            "size": s.entry_size,
            "pnl_pct": round(pnl_pct, 3),
            "result": "win" if is_win else "loss",
            "risk_after": round(s.current_risk, 2),
            "entry_time": s.entry_time,
            "exit_time": time.time(),
            # Data-driven tuning fields
            "ema_deviation_pct": round(getattr(s, '_entry_ema_dev', 0), 3),
            "atr_pct": round(getattr(s, '_entry_atr_pct', 0), 3),
            "ema50_at_entry": round(getattr(s, '_entry_ema50', 0), 2),
            "exit_reason": s.exit_reason or "exchange_sltp",
        })

        # Notify
        exit_reason_str = s.exit_reason or "SL/TP"
        balance = self.trader.get_equity()
        await self.notifier.send(
            f"{result_emoji} *{self.symbol} Trade Closed ({exit_reason_str})*\n"
            f"Side: `{s.position_side.upper()}`\n"
            f"Source: `{s.entry_source}`\n"
            f"Entry: `{s.entry_price}` → Exit: `{last_price}`\n"
            f"PnL: `{pnl_pct:+.2f}%` (`{pnl_usd:+.2f}$`)\n"
            f"Balance: `{balance:.2f}$`\n"
            f"Risk now: `{s.current_risk:.1f}%` | "
            f"W/L: `{s.wins}/{s.losses}` ({s.total_trades} total)"
        )

        logger.info(
            f"{result_emoji} [{self.symbol}] Closed {s.position_side} | PnL={pnl_pct:+.2f}% | "
            f"Risk→{s.current_risk:.1f}%"
        )

        # Reset position state
        s.in_position = False
        s.position_side = ""
        s.entry_price = 0.0
        s.entry_size = 0.0
        s.entry_source = ""
        s.zone_level = 0.0
        s.initial_sl = 0.0
        s.current_sl = 0.0
        s.take_profit = 0.0
        s.sl_distance = 0.0
        s.breakeven_applied = False
        s.trailing_active = False
        s.trailing_sl = 0.0
        s.trailing_peak = 0.0
        s.exit_reason = ""
        s.entry_time = 0.0
        # Clear pending exit levels
        s.pending_sl = 0.0
        s.pending_tp = 0.0
        s.pending_trail_activation = 0.0
        s.pending_trail_offset = 0.0
        # Pine: lastExitBar := bar_index (= N, the bar where position closes).
        # Use timestamp so cooldown survives restarts.
        s.last_exit_bar = s.bar_counter
        s.last_exit_t = s.last_bar_t
        self._last_exchange_sl = 0.0  # Reset for next position

        self._save_state()

    # ── entry signals ────────────────────────────────────────
    async def _check_entries(self, candles: List[dict]):
        """Check for OB and FVG entry signals — mirrors Pine Script.
        candles[-1] here is the last CLOSED candle (current unclosed already stripped)."""
        s = self.state
        c = candles[-1]  # last CLOSED candle

        # Cooldown check (use timestamps if available, fall back to bar_counter)
        if s.last_exit_t > 0 and s.last_bar_t > 0:
            bars_since_exit = self._bars_between(s.last_exit_t, s.last_bar_t)
        else:
            bars_since_exit = s.bar_counter - s.last_exit_bar
        if bars_since_exit <= STRATEGY.COOLDOWN_BARS:
            return

        # ATR & EMA50
        atr = compute_atr(candles, STRATEGY.ATR_LENGTH)
        if atr <= 0:
            return

        closes = [x["c"] for x in candles]
        ema50 = compute_ema(closes, 50)

        sl_distance = STRATEGY.SL_MULTIPLIER * atr

        # ── OB conditions ──
        # Pine: obLongCondition = not na(bullOB) and low < bullOB and close > bullOB
        ob_long = s.bull_ob > 0 and c["l"] < s.bull_ob and c["c"] > s.bull_ob
        # Pine: obShortCondition = not na(bearOB) and high > bearOB and close < bearOB
        ob_short = s.bear_ob > 0 and c["h"] > s.bear_ob and c["c"] < s.bear_ob

        # ── FVG conditions ──
        fvg_long = False
        fvg_short = False

        if STRATEGY.USE_FVG:
            # Use timestamps for FVG age (survives restarts)
            if s.bull_fvg_t > 0 and s.last_bar_t > 0:
                bull_fvg_age = self._bars_between(s.bull_fvg_t, s.last_bar_t)
            else:
                bull_fvg_age = s.bar_counter - s.bull_fvg_bar

            if s.bear_fvg_t > 0 and s.last_bar_t > 0:
                bear_fvg_age = self._bars_between(s.bear_fvg_t, s.last_bar_t)
            else:
                bear_fvg_age = s.bar_counter - s.bear_fvg_bar

            bull_fvg_valid = (
                s.bull_fvg_bottom > 0
                and bull_fvg_age <= STRATEGY.FVG_LOOKBACK
            )
            bear_fvg_valid = (
                s.bear_fvg_top > 0
                and bear_fvg_age <= STRATEGY.FVG_LOOKBACK
            )

            # Pine: fvgLongCondition = bullFVG_valid and low <= bullFVG_top and close > bullFVG_bottom
            #        and close > open and close > ema50
            if bull_fvg_valid:
                fvg_long = (
                    c["l"] <= s.bull_fvg_top
                    and c["c"] > s.bull_fvg_bottom
                    and c["c"] > c["o"]
                    and c["c"] > ema50
                )

            # Pine: fvgShortCondition = bearFVG_valid and high >= bearFVG_bottom and close < bearFVG_top
            #        and close < open and close < ema50
            if bear_fvg_valid:
                fvg_short = (
                    c["h"] >= s.bear_fvg_bottom
                    and c["c"] < s.bear_fvg_top
                    and c["c"] < c["o"]
                    and c["c"] < ema50
                )

        # ── Combined conditions ──
        long_cond = ob_long or fvg_long
        short_cond = ob_short or fvg_short

        # ── Strong candle filter ──
        # Pine: strongBullishCandle = close > open and (close - open) > atr * 0.35
        candle_body = abs(c["c"] - c["o"])
        strong_threshold = atr * STRATEGY.STRONG_CANDLE_ATR_RATIO
        strong_bull = c["c"] > c["o"] and candle_body > strong_threshold
        strong_bear = c["c"] < c["o"] and candle_body > strong_threshold

        # Debug: log signal check details on each new candle
        logger.debug(
            f"[{self.symbol}] bar={s.bar_counter} | "
            f"C={c['c']:.2f} O={c['o']:.2f} H={c['h']:.2f} L={c['l']:.2f} | "
            f"ATR={atr:.4f} EMA50={ema50:.2f} | "
            f"bullOB={s.bull_ob:.2f} bearOB={s.bear_ob:.2f} | "
            f"obL={ob_long} obS={ob_short} fvgL={fvg_long} fvgS={fvg_short} | "
            f"strongB={strong_bull} strongS={strong_bear} body={candle_body:.4f} thr={strong_threshold:.4f}"
        )

        # Log when any condition partially matches (helps diagnose HL/Bybit divergence)
        partial_long = ob_long or fvg_long
        partial_short = ob_short or fvg_short
        if partial_long or partial_short:
            side_label = "LONG" if partial_long else "SHORT"
            src = ("OB" if (ob_long or ob_short) else "FVG")
            strong_ok = strong_bull if partial_long else strong_bear
            logger.info(
                f"🔍 [{self.symbol}] bar={s.bar_counter} {side_label} {src} zone hit | "
                f"C={c['c']:.2f} body={candle_body:.2f}/{strong_threshold:.2f} "
                f"strong={'✅' if strong_ok else '❌'} | "
                f"bullOB={s.bull_ob:.2f} bearOB={s.bear_ob:.2f} "
                f"bullFVG=[{getattr(s, 'bull_fvg_bottom', 0):.2f}-{getattr(s, 'bull_fvg_top', 0):.2f}] "
                f"bearFVG=[{getattr(s, 'bear_fvg_bottom', 0):.2f}-{getattr(s, 'bear_fvg_top', 0):.2f}]"
            )

        long_cond = long_cond and strong_bull
        short_cond = short_cond and strong_bear

        # ── Execute entry ──
        if long_cond:
            entry_source = "OB" if ob_long else "FVG"
            zone_level = s.bull_ob if ob_long else s.bull_fvg_bottom
            await self._enter_position("long", entry_source, zone_level, sl_distance, atr, candles)

        elif short_cond:
            entry_source = "OB" if ob_short else "FVG"
            zone_level = s.bear_ob if ob_short else s.bear_fvg_top
            await self._enter_position("short", entry_source, zone_level, sl_distance, atr, candles)

    async def _enter_position(
        self,
        side: str,
        entry_source: str,
        zone_level: float,
        sl_distance: float,
        atr: float,
        candles: List[dict],
    ):
        """Calculate position size, SL/TP, and open position."""
        s = self.state
        entry_price = candles[-1]["c"]  # approximate entry price

        # Data-driven metrics: save for post-analysis
        closes = [x["c"] for x in candles]
        ema50 = compute_ema(closes, 50)
        ema_deviation_pct = ((entry_price - ema50) / ema50) * 100 if ema50 > 0 else 0
        atr_pct = (atr / entry_price) * 100 if entry_price > 0 else 0

        # SL & TP (Pine: slDistance = slMultiplier * atr)
        if side == "long":
            sl_price = entry_price - sl_distance
            tp_price = entry_price + sl_distance * STRATEGY.RR_RATIO
        else:
            sl_price = entry_price + sl_distance
            tp_price = entry_price - sl_distance * STRATEGY.RR_RATIO

        # Position sizing: fixedRiskDollars / slDistance
        equity = self.trader.get_equity()
        risk_dollars = (equity * s.current_risk) / 100
        size_coin = risk_dollars / sl_distance

        # Clamp size: Pine — maxSize = (strategy.equity * 0.95) / close
        min_size = 0.001
        max_size = (equity * RISK.MAX_EQUITY_USAGE) / entry_price
        size_coin = max(min_size, min(size_coin, max_size))

        # Check minimum notional ($11)
        notional = size_coin * entry_price
        if notional < RISK.MIN_POSITION_USD:
            logger.info(
                f"[{self.symbol}] Notional ${notional:.2f} < ${RISK.MIN_POSITION_USD}. Adjusting up."
            )
            size_coin = RISK.MIN_POSITION_USD / entry_price

        # Check max position size
        notional = size_coin * entry_price
        if notional > RISK.MAX_POSITION_USD:
            size_coin = RISK.MAX_POSITION_USD / entry_price

        # Add candle timestamp for debugging (convert epoch ms to human time)
        candle_t_ms = candles[-1]["t"]
        candle_time_str = datetime.datetime.utcfromtimestamp(candle_t_ms / 1000).strftime("%H:%M")

        logger.info(
            f"📊 [{self.symbol}] Signal: {side.upper()} {entry_source} | "
            f"Candle={candle_time_str} bar={s.bar_counter} | "
            f"Price≈{entry_price:.2f} | SL={sl_price:.2f} | TP={tp_price:.2f} | "
            f"Size={size_coin:.4f} (${size_coin * entry_price:.2f}) | "
            f"Risk={s.current_risk:.1f}%"
        )

        # Notify before execution
        await self.notifier.send(
            f"📊 *{self.symbol} Signal: {side.upper()} ({entry_source})*\n"
            f"Price: `{entry_price:.2f}`\n"
            f"SL: `{sl_price:.2f}` | TP: `{tp_price:.2f}`\n"
            f"Size: `{size_coin:.4f}` (`${size_coin * entry_price:.2f}`)\n"
            f"Risk: `{s.current_risk:.1f}%`"
        )

        # Execute — exchange SL/TP placed as safety net after fill.
        result = await self.trader.open_position(self.symbol, side, size_coin, 0, 0)
        if not result:
            logger.error("Failed to open position.")
            return

        # Recalculate SL/TP from ACTUAL fill price (not candle close).
        # Pine uses strategy.position_avg_price — we must mirror that.
        actual_entry = result["entry_price"]
        if abs(actual_entry - entry_price) > 0.01:
            logger.info(
                f"🔧 [{self.symbol}] Adjusting SL/TP from fill price: "
                f"close={entry_price:.2f} → fill={actual_entry:.2f} (Δ={actual_entry - entry_price:+.2f})"
            )
            if side == "long":
                sl_price = actual_entry - sl_distance
                tp_price = actual_entry + sl_distance * STRATEGY.RR_RATIO
            else:
                sl_price = actual_entry + sl_distance
                tp_price = actual_entry - sl_distance * STRATEGY.RR_RATIO
            logger.info(
                f"🔧 [{self.symbol}] SL/TP recalculated from fill: SL={sl_price:.2f} TP={tp_price:.2f}"
            )

        # Update state
        s.in_position = True
        s.position_side = side
        s.entry_price = actual_entry
        s.entry_size = result["size"]
        s.entry_source = entry_source
        s.zone_level = zone_level
        s.sl_distance = sl_distance
        s.initial_sl = sl_price
        s.current_sl = sl_price
        s.take_profit = tp_price
        s.breakeven_applied = False
        s.trailing_active = False
        s.trailing_sl = 0.0
        s.trailing_peak = 0.0
        s.exit_reason = ""
        s.entry_time = time.time()
        self._last_exchange_sl = 0.0
        self._last_sl_exchange_update = 0.0
        self._emergency_first_seen = 0

        # Set initial pending exit levels (active from next candle close)
        # Pine: strategy.exit() is called on the same bar as entry,
        # so these levels are checked against the next bar's OHLC.
        trail_offset_ticks = sl_distance * STRATEGY.TRAIL_OFFSET_RATIO  # Pine: trailOffset = slDistance * 0.2 (ticks)
        trail_offset_price = trail_offset_ticks * 0.01  # Convert ticks to price: ticks × syminfo.mintick
        s.pending_sl = sl_price
        s.pending_tp = tp_price
        if side == "long":
            s.pending_trail_activation = actual_entry + sl_distance * STRATEGY.RR_RATIO * STRATEGY.TRAIL_ACTIVATION_RATIO
        else:
            s.pending_trail_activation = actual_entry - sl_distance * STRATEGY.RR_RATIO * STRATEGY.TRAIL_ACTIVATION_RATIO
        s.pending_trail_offset = trail_offset_price

        # Store entry analytics for data-driven tuning
        s._entry_ema_dev = ema_deviation_pct
        s._entry_atr_pct = atr_pct
        s._entry_ema50 = ema50

        self._save_state()
        logger.info(f"✅ [{self.symbol}] Position opened: {side.upper()} @ {s.entry_price}")

        # Place exchange SL/TP as safety net (bot checks internally, these are backup)
        await self._update_exchange_sltp()

    # ── position management ──────────────────────────────────

    def on_price_update(self, price: float):
        """
        Called from WebSocket on every mid-price tick (real-time).

        Checks SL and TP intra-bar (Pine broker emulator does this too).
        Trailing stop is checked on candle close only (in _manage_position_on_candle)
        to match Pine's bar-close evaluation behavior.
        """
        if not self._warmup_done or not self.state.in_position:
            return None
        s = self.state
        if not (s.pending_sl > 0 or s.pending_tp > 0):
            return None

        is_long = s.position_side == "long"

        # ── Check TP ──
        if s.pending_tp > 0:
            if (is_long and price >= s.pending_tp) or (not is_long and price <= s.pending_tp):
                logger.info(
                    f"🎯 [{self.symbol}] TP HIT (intra-bar): price={price:.2f} "
                    f"TP={s.pending_tp:.2f}"
                )
                s.exit_reason = "tp"
                self._exit_was_intra_bar = True
                return self._close_and_handle(s.pending_tp)

        # ── Check SL ──
        if s.pending_sl > 0:
            if (is_long and price <= s.pending_sl) or (not is_long and price >= s.pending_sl):
                logger.info(
                    f"🛑 [{self.symbol}] SL HIT (intra-bar): price={price:.2f} "
                    f"SL={s.pending_sl:.2f}"
                )
                s.exit_reason = "sl"
                self._exit_was_intra_bar = True
                return self._close_and_handle(s.pending_sl)

        # Trailing stop: activation tracking only (no exit on ticks).
        # Peak is updated tick-by-tick so we capture the true high/low,
        # but the actual trail exit decision happens on candle close.
        if s.pending_trail_activation > 0 and s.pending_trail_offset > 0:
            if is_long:
                if price >= s.pending_trail_activation and not s.trailing_active:
                    s.trailing_active = True
                    s.trailing_peak = price
                    logger.info(
                        f"📈 [{self.symbol}] Trail ACTIVATED (intra-bar): "
                        f"price={price:.2f} >= activation={s.pending_trail_activation:.2f}"
                    )
                elif s.trailing_active and price > s.trailing_peak:
                    s.trailing_peak = price
            else:  # short
                if price <= s.pending_trail_activation and not s.trailing_active:
                    s.trailing_active = True
                    s.trailing_peak = price
                    logger.info(
                        f"📉 [{self.symbol}] Trail ACTIVATED (intra-bar): "
                        f"price={price:.2f} <= activation={s.pending_trail_activation:.2f}"
                    )
                elif s.trailing_active and price < s.trailing_peak:
                    s.trailing_peak = price

        return None

    def on_user_fill(self, fill: dict):
        """
        Called synchronously from WS thread when a UserFills message arrives.
        Stores fill data in cache keyed by oid for later lookup.
        The 'px' field is the ACTUAL execution price.
        """
        coin = fill.get("coin", "")
        if coin != self.symbol:
            return
        oid = fill.get("oid")
        if oid is not None:
            self._fill_cache[oid] = fill
            logger.info(
                f"💰 [{self.symbol}] Fill cached: oid={oid} px={fill.get('px')} "
                f"dir={fill.get('dir')} pnl={fill.get('closedPnl')}"
            )
            # Keep cache small — remove entries older than 60s
            now_ms = int(time.time() * 1000)
            stale = [k for k, v in self._fill_cache.items()
                     if now_ms - v.get("time", now_ms) > 60_000]
            for k in stale:
                del self._fill_cache[k]

    async def _get_fill_price(self, oid) -> float:
        """
        Get actual fill price for an order.
        
        Priority:
          1. WS UserFills cache (instant, no API call)
          2. REST user_fills_by_time fallback
          3. Mid-market price fallback
        
        WS orderUpdates 'limitPx' for trigger orders is the far-limit
        (set 10% away from trigger for guaranteed fill), NOT the actual
        execution price. We MUST use the fill px instead.
        """
        oid_int = int(oid) if not isinstance(oid, int) else oid

        # 1. Check WS fill cache (should be populated by userFills subscription)
        cached = self._fill_cache.get(oid_int)
        if cached:
            real_px = float(cached["px"])
            logger.info(
                f"✅ [{self.symbol}] Fill price from WS cache: {real_px} "
                f"(oid={oid}, closedPnl={cached.get('closedPnl', '?')})"
            )
            return real_px

        # 2. WS fill may not have arrived yet — wait briefly then retry
        logger.info(f"⏳ [{self.symbol}] Fill not in WS cache yet for oid={oid}, waiting...")
        await asyncio.sleep(0.3)
        cached = self._fill_cache.get(oid_int)
        if cached:
            real_px = float(cached["px"])
            logger.info(
                f"✅ [{self.symbol}] Fill price from WS cache (after wait): {real_px} "
                f"(oid={oid})"
            )
            return real_px

        # 3. REST fallback
        logger.warning(f"⚠️ [{self.symbol}] WS fill cache miss — trying REST for oid={oid}")
        try:
            address = self.trader.signer.address
            start_ms = int((time.time() - 60) * 1000)
            fills = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.trader.info.user_fills_by_time(address, start_ms)
            )
            for fill in reversed(fills):
                if fill.get("coin") == self.symbol and fill.get("oid") == oid_int:
                    real_px = float(fill["px"])
                    logger.info(
                        f"✅ [{self.symbol}] Fill price from REST: {real_px} (oid={oid})"
                    )
                    return real_px

            # oid not found — match by coin + direction
            expected_dir = "Close Long" if self.state.position_side == "long" else "Close Short"
            for fill in reversed(fills):
                if fill.get("coin") == self.symbol and fill.get("dir") == expected_dir:
                    real_px = float(fill["px"])
                    logger.warning(
                        f"⚠️ [{self.symbol}] Fill oid mismatch — using {expected_dir} "
                        f"fill: px={real_px} (fill_oid={fill.get('oid')}, expected={oid})"
                    )
                    return real_px

            logger.warning(f"⚠️ [{self.symbol}] No fill in REST for oid={oid}")
        except Exception as e:
            logger.error(f"❌ [{self.symbol}] REST fill lookup failed: {e}")

        # 4. Last resort: mid-market price
        try:
            mid = self.trader.get_mark_price(self.symbol)
            logger.warning(f"⚠️ [{self.symbol}] Using mid-market fallback: {mid}")
            return mid
        except Exception:
            logger.error(f"❌ [{self.symbol}] All fill price lookups failed")
            return 0.0

    async def on_order_triggered(self, order_data: dict):
        """
        Called from WebSocket when an order update comes in.
        HL format: {'order': {'coin': 'ETH', 'side': 'A', 'limitPx': ..., 'sz': ..., 'oid': ...}, 
                    'status': 'filled'|'canceled'|'triggered'|..., 'statusTimestamp': ...}
        """
        if not self._warmup_done:
            return

        # HL nests order details inside 'order' dict
        order_info = order_data.get("order", {})
        coin = order_info.get("coin", "")
        if coin != self.symbol:
            return

        status = order_data.get("status", "")
        oid = order_info.get("oid", "?")
        side = order_info.get("side", "?")
        limit_px = order_info.get("limitPx", "?")
        cloid = order_info.get("cloid", "")

        logger.info(
            f"📋 [{self.symbol}] OrderUpdate: status={status} side={side} "
            f"px={limit_px} oid={oid} cloid={cloid}"
        )

        # HL sends 'filled' when a tpsl trigger order executes.
        # For limit orders it also sends 'filled'. We detect SL/TP by:
        # 1. The order was placed by us as reduce-only (SL/TP always are)
        # 2. Status is 'filled' and we're in a position
        # 3. Side is opposite to our position (sell for long, buy for short)
        is_close_fill = (
            status == "filled"
            and self.state.in_position
            and (
                (self.state.position_side == "long" and side == "A")  # sell = close long
                or (self.state.position_side == "short" and side == "B")  # buy = close short
            )
        )

        if is_close_fill:
            logger.info(
                f"⚡ [{self.symbol}] Exchange SL/TP filled! "
                f"status={status} side={side} px={limit_px} oid={oid}"
            )
            # Position was closed by exchange trigger — clean up remaining orders + state
            if self.state.in_position and not self._lock:
                self._lock = True
                self._exit_was_intra_bar = True  # SL/TP fired between candle closes
                try:
                    await self.trader.cancel_all_orders(self.symbol)
                    # Safety: wait briefly then verify no ghost position
                    await asyncio.sleep(0.5)
                    await self._verify_no_reverse_position()
                    # Get ACTUAL fill price — limitPx in orderUpdates is the far-limit
                    # for trigger orders (set 10% away), NOT the real execution price.
                    # Uses WS userFills cache first, REST fallback if needed.
                    exit_price = await self._get_fill_price(oid)
                    await self._on_position_closed([], exit_price=exit_price)
                finally:
                    self._lock = False

    async def _close_and_handle(self, exit_price: float):
        """Close position via market and handle state update.
        
        Strategy: close FIRST (fastest path), then clean up orders,
        then verify no reverse. Speed > order cancellation priority
        because the SL trigger may fire at any moment.
        """
        if self._lock:
            return
        self._lock = True
        try:
            # 1. Close position IMMEDIATELY via market_close (SDK handles reduce-only)
            try:
                result = await asyncio.to_thread(
                    self.trader.exchange.market_close, self.symbol
                )
                if result is None:
                    # SDK returns None when no position to close (exchange SL already triggered)
                    logger.info(f"[{self.symbol}] market_close returned None — position already closed by exchange.")
                elif result.get("status") == "ok":
                    logger.info(f"[{self.symbol}] Emergency market_close executed.")
                else:
                    logger.error(f"[{self.symbol}] market_close failed: {result}")
            except Exception as e:
                logger.error(f"[{self.symbol}] market_close error: {e}")

            # 2. Cancel ALL remaining trigger orders (SL/TP) — they're orphans now
            await self.trader.cancel_all_orders(self.symbol)

            # 3. Wait for settlement, then verify no reverse position
            await asyncio.sleep(1.0)
            self.trader.invalidate_cache()
            await self._verify_no_reverse_position()

            # 4. Double-check: cancel any orders that appeared from race
            await self.trader.cancel_all_orders(self.symbol)

            await self._on_position_closed([], exit_price=exit_price)
        finally:
            self._lock = False

    async def _verify_no_reverse_position(self):
        """Check for accidental reverse position from SL/TP race condition.
        Retries up to 3 times with fresh exchange reads."""
        for attempt in range(3):
            self.trader.invalidate_cache()
            pos = self.trader.get_position(self.symbol)
            if pos is None:
                return  # Clean — no position
            szi = float(pos.get("szi", 0))
            if szi == 0:
                return  # Clean
            logger.warning(
                f"⚠️ [{self.symbol}] Reverse position detected! size={szi} "
                f"— closing (attempt {attempt + 1}/3)"
            )
            await self.trader.cancel_all_orders(self.symbol)
            try:
                await asyncio.to_thread(
                    self.trader.exchange.market_close, self.symbol
                )
                logger.info(f"✅ [{self.symbol}] Reverse position closed.")
            except Exception as e:
                logger.error(f"❌ [{self.symbol}] Failed to close reverse: {e}")
            await asyncio.sleep(1.0)
        
        # Final check
        self.trader.invalidate_cache()
        pos = self.trader.get_position(self.symbol)
        if pos and float(pos.get("szi", 0)) != 0:
            logger.error(f"❌ [{self.symbol}] STILL have reverse position after 3 attempts!")

    async def _manage_position_on_candle(self, candles: List[dict]):
        """
        Called on new candle close.
        
        Pine flow: entries on close only, SL/TP intra-bar via on_price_update().
        Trailing stop checked here on candle close to match Pine bar-close semantics.
        Then recalculates exit levels (SL/TP/trail) for the next bar.
        """
        s = self.state
        if not s.in_position:
            return

        # Verify position still exists (may have been closed by exchange or RT monitor)
        pos = self.trader.get_position(self.symbol)
        if pos is None:
            logger.info(f"[{self.symbol}] Position closed externally.")
            try:
                await self.trader.cancel_all_orders(self.symbol)
            except Exception as e:
                logger.error(f"[{self.symbol}] Failed to cancel orders: {e}")
            await self._on_position_closed(candles, exit_price=candles[-1]["c"])
            return

        closed = candles[-1]
        close_price = closed["c"]
        high = closed["h"]
        low = closed["l"]
        entry_price = s.entry_price

        logger.info(
            f"📋 [{self.symbol}] Position check: {s.position_side.upper()} @ {entry_price:.2f} | "
            f"bar O={closed['o']:.2f} H={high:.2f} L={low:.2f} C={close_price:.2f} | "
            f"SL={s.pending_sl:.2f} TP={s.pending_tp:.2f} trail={'ON' if s.trailing_active else 'off'}"
        )

        # ── Trailing stop check on candle close ──
        # Pine broker emulator checks trail against bar OHLC, not ticks.
        # We check against the closed bar's high/low for activation and
        # close price for trail exit — matching Pine's bar-close semantics.
        if s.pending_trail_activation > 0 and s.pending_trail_offset > 0:
            is_long = s.position_side == "long"

            # Activation: use bar's high (long) or low (short)
            if is_long:
                if high >= s.pending_trail_activation and not s.trailing_active:
                    s.trailing_active = True
                    s.trailing_peak = high
                    logger.info(
                        f"📈 [{self.symbol}] Trail ACTIVATED (bar close): "
                        f"high={high:.2f} >= activation={s.pending_trail_activation:.2f}"
                    )
                elif s.trailing_active and high > s.trailing_peak:
                    s.trailing_peak = high
            else:
                if low <= s.pending_trail_activation and not s.trailing_active:
                    s.trailing_active = True
                    s.trailing_peak = low
                    logger.info(
                        f"📉 [{self.symbol}] Trail ACTIVATED (bar close): "
                        f"low={low:.2f} <= activation={s.pending_trail_activation:.2f}"
                    )
                elif s.trailing_active and low < s.trailing_peak:
                    s.trailing_peak = low

            # Trail exit: check if close crossed the trailing SL
            if s.trailing_active:
                if is_long:
                    trail_sl = s.trailing_peak - s.pending_trail_offset
                    if close_price <= trail_sl:
                        logger.info(
                            f"📈 [{self.symbol}] TRAIL EXIT (bar close): "
                            f"close={close_price:.2f} <= trail_sl={trail_sl:.2f} "
                            f"(peak={s.trailing_peak:.2f})"
                        )
                        s.exit_reason = "trailing"
                        self._exit_was_intra_bar = False
                        await self._close_and_handle(close_price)
                        return
                else:
                    trail_sl = s.trailing_peak + s.pending_trail_offset
                    if close_price >= trail_sl:
                        logger.info(
                            f"📉 [{self.symbol}] TRAIL EXIT (bar close): "
                            f"close={close_price:.2f} >= trail_sl={trail_sl:.2f} "
                            f"(peak={s.trailing_peak:.2f})"
                        )
                        s.exit_reason = "trailing"
                        self._exit_was_intra_bar = False
                        await self._close_and_handle(close_price)
                        return

        # Recalculate exit levels for next bar
        await self._recalculate_exit_levels(candles)

    async def _recalculate_exit_levels(self, candles: List[dict]):
        """
        PART B: Recalculate exit levels for the next bar.
        Pine: slDistance, SL, TP, trail recalculated every bar via strategy.exit().
        These become active for the next bar's broker emulator check.
        """
        s = self.state
        if not s.in_position:
            return

        close_price = candles[-1]["c"]
        entry_price = s.entry_price

        atr = compute_atr(candles, STRATEGY.ATR_LENGTH)
        if atr <= 0:
            atr = s.sl_distance / STRATEGY.SL_MULTIPLIER if s.sl_distance > 0 else 1.0
        sl_distance = STRATEGY.SL_MULTIPLIER * atr

        if s.position_side == "long":
            initial_sl = entry_price - sl_distance
            take_profit = entry_price + sl_distance * STRATEGY.RR_RATIO
        else:
            initial_sl = entry_price + sl_distance
            take_profit = entry_price - sl_distance * STRATEGY.RR_RATIO

        # ── Breakeven (conditional, reversible — exactly like Pine) ──
        BUFFER_PRICE = 0.20  # syminfo.mintick * 20 = $0.01 * 20

        if s.position_side == "long":
            be_level = s.bull_ob if s.entry_source == "OB" else s.bull_fvg_bottom
            reached_1r = close_price >= entry_price + sl_distance
            current_sl = max(initial_sl, be_level - BUFFER_PRICE) if reached_1r else initial_sl
        else:
            be_level = s.bear_ob if s.entry_source == "OB" else s.bear_fvg_top
            reached_1r = close_price <= entry_price - sl_distance
            current_sl = min(initial_sl, be_level + BUFFER_PRICE) if reached_1r else initial_sl

        # Log breakeven changes
        if reached_1r and not s.breakeven_applied:
            s.breakeven_applied = True
            logger.info(
                f"🔒 [{self.symbol}] Breakeven reached: SL={current_sl:.2f} "
                f"(beLevel={be_level:.2f}, initialSL={initial_sl:.2f})"
            )
        elif not reached_1r and s.breakeven_applied:
            s.breakeven_applied = False
            logger.info(f"🔓 [{self.symbol}] Breakeven reverted: SL back to initialSL={initial_sl:.2f}")

        # ── Trail parameters ──
        trail_activation_price = entry_price + sl_distance * STRATEGY.RR_RATIO * STRATEGY.TRAIL_ACTIVATION_RATIO \
            if s.position_side == "long" else \
            entry_price - sl_distance * STRATEGY.RR_RATIO * STRATEGY.TRAIL_ACTIVATION_RATIO
        trail_offset_ticks = sl_distance * STRATEGY.TRAIL_OFFSET_RATIO  # Pine: trailOffset = slDistance * 0.2 (ticks)
        trail_offset_price = trail_offset_ticks * 0.01  # Convert ticks to price: ticks × syminfo.mintick

        # ── Store pending levels for next bar's real-time monitoring ──
        s.pending_sl = current_sl
        s.pending_tp = take_profit
        s.pending_trail_activation = trail_activation_price
        s.pending_trail_offset = trail_offset_price
        s.current_sl = current_sl
        s.sl_distance = sl_distance
        s.initial_sl = initial_sl
        s.take_profit = take_profit

        # Log updated levels
        if s.trailing_active:
            trail_sl = (s.trailing_peak - trail_offset_price) if s.position_side == "long" \
                else (s.trailing_peak + trail_offset_price)
            logger.info(
                f"📊 [{self.symbol}] Levels updated: SL={current_sl:.2f} TP={take_profit:.2f} | "
                f"Trail ON: peak={s.trailing_peak:.2f} trail_sl={trail_sl:.2f} offset=${trail_offset_price:.4f}"
            )
        else:
            logger.info(
                f"📊 [{self.symbol}] Levels updated: SL={current_sl:.2f} TP={take_profit:.2f} | "
                f"Trail activation={trail_activation_price:.2f} offset=${trail_offset_price:.4f}"
            )

        self._save_state()

        # Update exchange SL/TP safety net
        await self._update_exchange_sltp()

    # ── exchange SL/TP safety net ────────────────────────────
    async def _update_exchange_sltp(self):
        """Place/update SL and TP on exchange as a safety net.
        
        The bot handles exits internally (on_price_update for SL/TP,
        _manage_position_on_candle for trailing). Exchange orders are
        backup in case the bot crashes or loses WS connection.
        
        Dedup: skips update if SL hasn't changed since last placement.
        """
        s = self.state
        if not s.in_position or s.pending_sl <= 0:
            return

        # Dedup: don't spam exchange if levels haven't changed
        sl_to_place = round(s.pending_sl, 2)
        tp_to_place = round(s.pending_tp, 2) if s.pending_tp > 0 else 0

        if sl_to_place == self._last_exchange_sl:
            return

        try:
            await self.trader.replace_sl_tp(
                symbol=self.symbol,
                size=s.entry_size,
                side=s.position_side,
                sl_price=sl_to_place,
                tp_price=tp_to_place,
            )
            self._last_exchange_sl = sl_to_place
            logger.info(
                f"🔄 [{self.symbol}] Exchange SL/TP updated: SL={sl_to_place:.2f} TP={tp_to_place:.2f}"
            )
        except Exception as e:
            logger.error(f"[{self.symbol}] Failed to update exchange SL/TP: {e}")

    # ── emergency SL helper ─────────────────────────────────
    def _calc_emergency_sl(self, side: str, base_sl: float, sl_distance: float) -> float:
        """Calculate wide emergency SL for exchange (safety net only).
        Set at 2x the SL distance from the base_sl price, giving room for
        candle-close evaluation to handle normal SL hits.
        """
        EMERGENCY_MULT = 1.0  # extra distance beyond base_sl
        if side == "long":
            return base_sl - sl_distance * EMERGENCY_MULT
        else:
            return base_sl + sl_distance * EMERGENCY_MULT

    # ── trade log ────────────────────────────────────────────
    def _save_trade(self, trade: dict):
        """Append trade to trades.json."""
        os.makedirs(GLOBAL.DATA_DIR, exist_ok=True)
        trades = []
        if os.path.exists(GLOBAL.TRADES_FILE):
            try:
                with open(GLOBAL.TRADES_FILE) as f:
                    trades = json.load(f)
            except Exception:
                trades = []

        trades.append(trade)
        # Keep last N trades
        if len(trades) > GLOBAL.MAX_TRADE_HISTORY:
            trades = trades[-GLOBAL.MAX_TRADE_HISTORY:]

        with open(GLOBAL.TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=2)
