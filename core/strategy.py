"""
HYPER_ETH — Strategy Engine
Exact translation of ETH_main.pine (OB + FVG + Smooth Dynamic Risk).One instance per symbol."""

import asyncio
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
    entry_time: float = 0.0

    # Smooth dynamic risk
    current_risk: float = RISK.BASE_RISK_PCT

    # Cooldown
    last_exit_bar: int = 0

    # Order Blocks (latest detected)
    bull_ob: float = 0.0
    bear_ob: float = 0.0

    # FVGs (latest detected)
    bull_fvg_top: float = 0.0
    bull_fvg_bottom: float = 0.0
    bull_fvg_bar: int = 0
    bear_fvg_top: float = 0.0
    bear_fvg_bottom: float = 0.0
    bear_fvg_bar: int = 0

    # Trade counter
    total_trades: int = 0
    wins: int = 0
    losses: int = 0

    # Bar counter (persisted for FVG validity and cooldown)
    bar_counter: int = 0


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

    # Check left bars
    for i in range(1, left + 1):
        if candles[pivot_idx - i]["h"] > pivot_high:
            return None
    # Check right bars
    for i in range(1, right + 1):
        if candles[pivot_idx + i]["h"] > pivot_high:
            return None

    return pivot_idx


def detect_pivot_low(candles: List[dict], left: int, right: int) -> Optional[int]:
    """Detect pivot low at index [-(right+1)]."""
    if len(candles) < left + right + 1:
        return None

    pivot_idx = len(candles) - 1 - right
    pivot_low = candles[pivot_idx]["l"]

    for i in range(1, left + 1):
        if candles[pivot_idx - i]["l"] < pivot_low:
            return None
    for i in range(1, right + 1):
        if candles[pivot_idx + i]["l"] < pivot_low:
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
        self._warmup_done = False  # Blocks all WS callbacks until warmup + sync complete
        self._lock = False  # Simple reentrance guard for async operations
        self._emergency_first_seen = 0.0  # Emergency backup timer
        self._last_sl_exchange_update = 0.0  # Throttle: last time we updated exchange SL
        self._sl_update_interval = 15  # Min seconds between exchange SL updates
        self._last_exchange_sl = 0.0  # Last SL value sent to exchange (dedup)

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
    def warmup(self, candles: List[dict]):
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
        for i in range(min_bars, len(candles)):
            bar_num = i  # use candle index as bar number during warmup
            window = candles[:i + 1]  # candles[0..i] — all bars up to current

            # ── Pivot-based Order Blocks ──
            # Check if there's a pivot at position [i - plen] confirmed by plen bars on each side
            if i >= plen * 2:
                candidate = i - plen
                is_pivot_high = True
                is_pivot_low = True

                for j in range(1, plen + 1):
                    if candles[candidate - j]["h"] > candles[candidate]["h"]:
                        is_pivot_high = False
                    if candles[candidate + j]["h"] > candles[candidate]["h"]:
                        is_pivot_high = False
                    if candles[candidate - j]["l"] < candles[candidate]["l"]:
                        is_pivot_low = False
                    if candles[candidate + j]["l"] < candles[candidate]["l"]:
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
                    fvg_count += 1

                if c0["h"] < c2["l"]:  # Bearish FVG
                    s.bear_fvg_top = c2["l"]
                    s.bear_fvg_bottom = c0["h"]
                    s.bear_fvg_bar = bar_num
                    fvg_count += 1

        # Set bar_counter to match candle count so FVG validity works correctly
        s.bar_counter = len(candles)
        # Store last CLOSED candle timestamp (candles[-2], since candles[-1] is forming)
        # This ensures the first WS candle close is detected as a new candle
        self._last_candle_t = candles[-2]["t"] if len(candles) >= 2 else candles[-1]["t"]

        # Fix cooldown: last_exit_bar from previous session may be > new bar_counter
        # which would block entries for hours until bar_counter catches up
        if s.last_exit_bar >= s.bar_counter:
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

        # NOW safe to allow WS callbacks (on_price_update, etc.)
        self._warmup_done = True

        logger.info(
            f"[{self.symbol}] Warmup complete: {len(candles)} bars | "
            f"OBs detected: {ob_count} | FVGs detected: {fvg_count} | "
            f"bull_ob={s.bull_ob:.2f} | bear_ob={s.bear_ob:.2f} | "
            f"bull_fvg=[{s.bull_fvg_bottom:.2f}-{s.bull_fvg_top:.2f}] (age={s.bar_counter - s.bull_fvg_bar}) | "
            f"bear_fvg=[{s.bear_fvg_bottom:.2f}-{s.bear_fvg_top:.2f}] (age={s.bar_counter - s.bear_fvg_bar})"
        )

    async def tick(self, candles: List[dict]):
        """
        Called on each new closed candle (detected by run.py or polling fallback).
        HL API returns current unclosed candle as candles[-1].
        We use candles[-2] (last CLOSED candle) for signals/indicators.

        Real-time exit management (trailing, SL/TP backup) is handled by
        on_price_update() via WebSocket — NOT here.
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

        # Use all candles up to and including last closed (exclude current unclosed)
        closed_candles = candles[:-1]

        # 1. Update indicators (on closed candles only)
        self._update_indicators(closed_candles)

        # 2. Sync position from exchange
        await self._sync_position(closed_candles)

        # 3. If in position → update exchange SL/TP on candle close
        if self.state.in_position:
            await self._manage_position_on_candle(closed_candles)
            return

        # 4. If flat → check entry signals
        await self._check_entries(closed_candles)

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

            # Bearish FVG: high < low[2]
            if c0["h"] < c2["l"]:
                s.bear_fvg_top = c2["l"]
                s.bear_fvg_bottom = c0["h"]
                s.bear_fvg_bar = s.bar_counter

    # ── position sync ────────────────────────────────────────
    async def _sync_position(self, candles: List[dict]):
        """Sync state.in_position with actual exchange position."""
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

        elif pos is not None and not self.state.in_position:
            # Position exists but state says flat — resync with computed SL/TP
            s = self.state
            szi = float(pos["szi"])
            s.in_position = True
            s.position_side = "long" if szi > 0 else "short"
            s.entry_price = float(pos["entryPx"])
            s.entry_size = abs(szi)

            # Compute SL/TP from current ATR (like a fresh entry)
            atr = compute_atr(candles, STRATEGY.ATR_LENGTH)
            if atr > 0:
                s.sl_distance = STRATEGY.SL_MULTIPLIER * atr
                if s.position_side == "long":
                    s.initial_sl = s.entry_price - s.sl_distance
                    s.current_sl = s.initial_sl
                    s.take_profit = s.entry_price + s.sl_distance * STRATEGY.RR_RATIO
                    s.zone_level = s.bull_ob if s.bull_ob > 0 else s.entry_price
                else:
                    s.initial_sl = s.entry_price + s.sl_distance
                    s.current_sl = s.initial_sl
                    s.take_profit = s.entry_price - s.sl_distance * STRATEGY.RR_RATIO
                    s.zone_level = s.bear_ob if s.bear_ob > 0 else s.entry_price

            s.entry_source = "OB"  # unknown, assume OB
            s.breakeven_applied = False
            s.trailing_active = False
            s.entry_time = time.time()
            self._save_state()

            logger.warning(
                f"[{self.symbol}] Resynced position: {s.position_side} @ {s.entry_price} | "
                f"SL={s.current_sl:.2f} | TP={s.take_profit:.2f}"
            )

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
        })

        # Notify
        await self.notifier.send(
            f"{result_emoji} *{self.symbol} Trade Closed*\n"
            f"Side: `{s.position_side.upper()}`\n"
            f"Source: `{s.entry_source}`\n"
            f"Entry: `{s.entry_price}` → Exit: `{last_price}`\n"
            f"PnL: `{pnl_pct:+.2f}%`\n"
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
        s.entry_time = 0.0
        s.last_exit_bar = s.bar_counter
        self._last_exchange_sl = 0.0  # Reset for next position

        self._save_state()

    # ── entry signals ────────────────────────────────────────
    async def _check_entries(self, candles: List[dict]):
        """Check for OB and FVG entry signals — mirrors Pine Script.
        candles[-1] here is the last CLOSED candle (current unclosed already stripped)."""
        s = self.state
        c = candles[-1]  # last CLOSED candle

        # Cooldown check
        if s.bar_counter <= s.last_exit_bar + STRATEGY.COOLDOWN_BARS:
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
            bull_fvg_valid = (
                s.bull_fvg_bottom > 0
                and (s.bar_counter - s.bull_fvg_bar) <= STRATEGY.FVG_LOOKBACK
            )
            bear_fvg_valid = (
                s.bear_fvg_top > 0
                and (s.bar_counter - s.bear_fvg_bar) <= STRATEGY.FVG_LOOKBACK
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

        # Clamp size
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

        logger.info(
            f"📊 [{self.symbol}] Signal: {side.upper()} {entry_source} | "
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

        # Execute
        result = await self.trader.open_position(self.symbol, side, size_coin, sl_price, tp_price)
        if not result:
            logger.error("Failed to open position.")
            return

        # Update state
        s.in_position = True
        s.position_side = side
        s.entry_price = result["entry_price"]
        s.entry_size = result["size"]
        s.entry_source = entry_source
        s.zone_level = zone_level
        s.sl_distance = sl_distance
        s.initial_sl = sl_price
        s.current_sl = sl_price
        s.take_profit = tp_price
        s.breakeven_applied = False
        s.trailing_active = False
        s.entry_time = time.time()
        self._last_exchange_sl = sl_price  # Exchange has initial SL from open_position
        self._last_sl_exchange_update = 0.0  # Reset throttle for new position

        self._save_state()
        logger.info(f"✅ [{self.symbol}] Position opened: {side.upper()} @ {s.entry_price}")

    # ── position management (hybrid: exchange SL/TP + WS real-time) ──

    def on_price_update(self, price: float):
        """
        Called from WebSocket on every mid-price tick (real-time).
        
        Responsibilities:
        - Compute trailing SL / breakeven (update internal state)
        - Emergency-only backup: market-close ONLY if price blows past SL by 2x
          and exchange trigger hasn't fired in 30 sec (exchange is the primary executor)
        
        We do NOT market-close on normal SL/TP hit — the exchange trigger order
        handles that. This avoids race conditions where both fire simultaneously
        and create a reverse position.
        """
        s = self.state
        if not self._warmup_done or not s.in_position or self._lock:
            return None

        # ── Emergency backup: only if price BLEW PAST SL significantly ──
        # Exchange should have closed already. If not, something is very wrong.
        emergency_margin = s.sl_distance * 0.5  # 50% past SL = emergency
        if s.position_side == "long":
            emergency_sl = s.current_sl - emergency_margin
            emergency_hit = price <= emergency_sl
        else:
            emergency_sl = s.current_sl + emergency_margin
            emergency_hit = price >= emergency_sl

        if emergency_hit:
            # Check if we've been past SL for a while (exchange should have fired)
            now = time.time()
            if self._emergency_first_seen == 0:
                self._emergency_first_seen = now
                logger.warning(
                    f"⚠️ [{self.symbol}] Price {price:.2f} blew past SL {s.current_sl:.2f} "
                    f"by {emergency_margin:.2f} — waiting for exchange trigger..."
                )
                return None

            elapsed = now - self._emergency_first_seen
            if elapsed >= 30:  # 30 seconds past emergency = exchange failed
                logger.error(
                    f"🆘 [{self.symbol}] EMERGENCY CLOSE: price={price:.2f} past SL={s.current_sl:.2f} "
                    f"for {elapsed:.0f}s — exchange trigger failed!"
                )
                self._emergency_first_seen = 0
                return self._close_and_handle(price)
            return None
        else:
            # Reset emergency timer if price came back
            self._emergency_first_seen = 0

        # ── Compute breakeven & trailing (internal state only) ──
        old_sl = s.current_sl

        if s.position_side == "long":
            if not s.breakeven_applied and price >= s.entry_price + s.sl_distance:
                buffer = s.sl_distance * 0.02
                new_sl = max(s.initial_sl, s.zone_level - buffer)
                if new_sl > s.current_sl:
                    s.current_sl = new_sl
                    s.breakeven_applied = True
                    logger.info(f"🔒 [{self.symbol}] Breakeven: SL → {s.current_sl:.2f}")

            trail_activation = s.sl_distance * STRATEGY.RR_RATIO * STRATEGY.TRAIL_ACTIVATION_RATIO
            trail_offset = s.sl_distance * STRATEGY.TRAIL_OFFSET_RATIO
            if price >= s.entry_price + trail_activation:
                trailing_sl = price - trail_offset
                if trailing_sl > s.current_sl:
                    s.current_sl = trailing_sl
                    s.trailing_active = True

        else:  # short
            if not s.breakeven_applied and price <= s.entry_price - s.sl_distance:
                buffer = s.sl_distance * 0.02
                new_sl = min(s.initial_sl, s.zone_level + buffer)
                if new_sl < s.current_sl:
                    s.current_sl = new_sl
                    s.breakeven_applied = True
                    logger.info(f"🔒 [{self.symbol}] Breakeven: SL → {s.current_sl:.2f}")

            trail_activation = s.sl_distance * STRATEGY.RR_RATIO * STRATEGY.TRAIL_ACTIVATION_RATIO
            trail_offset = s.sl_distance * STRATEGY.TRAIL_OFFSET_RATIO
            if price <= s.entry_price - trail_activation:
                trailing_sl = price + trail_offset
                if trailing_sl < s.current_sl:
                    s.current_sl = trailing_sl
                    s.trailing_active = True

        if s.current_sl != old_sl:
            self._save_state()
            # Schedule exchange SL update, throttled to avoid API spam
            now = time.time()
            if now - self._last_sl_exchange_update >= self._sl_update_interval:
                self._last_sl_exchange_update = now
                return self._update_exchange_sl()

        return None

    async def _update_exchange_sl(self):
        """Update exchange SL/TP trigger orders to match current trailing SL."""
        s = self.state
        if not s.in_position or self._lock:
            return
        # Deduplicate: skip if exchange already has this SL value
        if s.current_sl == self._last_exchange_sl:
            return
        try:
            logger.info(
                f"🔄 [{self.symbol}] WS trailing → exchange SL update: {s.current_sl:.2f}"
            )
            await self.trader.replace_sl_tp(
                symbol=self.symbol,
                size=s.entry_size,
                side=s.position_side,
                sl_price=s.current_sl,
                tp_price=s.take_profit,
            )
            # Re-check: if position was closed during the await (race condition),
            # we just placed orphan orders — clean them up immediately
            if not s.in_position:
                logger.warning(f"[{self.symbol}] Position closed during SL update — cleaning orphan orders")
                await self.trader.cancel_all_orders(self.symbol)
                return
            self._last_exchange_sl = s.current_sl
        except Exception as e:
            logger.error(f"[{self.symbol}] Failed to update exchange SL: {e}")

    async def on_order_triggered(self, order_data: dict):
        """
        Called from WebSocket when an order update comes in.
        Detects when our SL/TP triggered → handle position close.
        """
        if not self._warmup_done:
            return

        coin = order_data.get("coin", "")
        if coin != self.symbol:
            return

        status = order_data.get("status", "")
        order_type = order_data.get("orderType", "")

        # HL order statuses: 'filled', 'canceled', 'triggered', etc.
        if status == "filled" and ("Stop" in order_type or "Take Profit" in order_type):
            logger.info(
                f"⚡ [{self.symbol}] Exchange {order_type} filled! "
                f"px={order_data.get('triggerPx', '?')}"
            )
            # Position was closed by exchange trigger — clean up remaining orders + state
            if self.state.in_position and not self._lock:
                self._lock = True
                try:
                    await self.trader.cancel_all_orders(self.symbol)
                    # Safety: wait briefly then verify no ghost position
                    await asyncio.sleep(0.5)
                    await self._verify_no_reverse_position()
                    exit_price = float(order_data.get("triggerPx", 0)) or float(order_data.get("px", 0))
                    await self._on_position_closed([], exit_price=exit_price)
                finally:
                    self._lock = False

    async def _close_and_handle(self, exit_price: float):
        """Close position via market and handle state update.
        
        CRITICAL: Cancel trigger orders FIRST to prevent race condition
        where both bot market-close and exchange SL trigger execute,
        resulting in a reverse position.
        """
        if self._lock:
            return
        self._lock = True
        try:
            # 1. Cancel all trigger orders FIRST to prevent double execution
            await self.trader.cancel_all_orders(self.symbol)
            
            # 2. Close position via market
            closed = await self.trader.close_position(self.symbol)
            if closed:
                # 3. Safety: verify no accidental reverse position from race condition
                await asyncio.sleep(0.5)
                await self._verify_no_reverse_position()
                await self._on_position_closed([], exit_price=exit_price)
            else:
                logger.error(f"[{self.symbol}] Failed to close! Will retry.")
        finally:
            self._lock = False

    async def _verify_no_reverse_position(self):
        """Check for accidental reverse position from SL/TP race condition."""
        pos = self.trader.get_position(self.symbol)
        if pos is not None:
            szi = float(pos.get("szi", 0))
            if szi != 0:
                logger.warning(
                    f"⚠️ [{self.symbol}] Reverse position detected! size={szi} "
                    f"— closing immediately (SL/TP race condition)"
                )
                await self.trader.cancel_all_orders(self.symbol)
                try:
                    is_buy = szi < 0
                    await asyncio.to_thread(
                        self.trader.exchange.market_open,
                        self.symbol, is_buy, abs(szi), None, 0.01,
                    )
                    logger.info(f"✅ [{self.symbol}] Reverse position closed.")
                except Exception as e:
                    logger.error(f"❌ [{self.symbol}] Failed to close reverse: {e}")

    async def _manage_position_on_candle(self, candles: List[dict]):
        """
        Called on new candle close. Updates exchange SL/TP if trailing moved.
        Also does full position sync with exchange.
        """
        s = self.state
        if not s.in_position:
            return

        # Verify position still exists
        pos = self.trader.get_position(self.symbol)
        if pos is None:
            logger.info(f"[{self.symbol}] Position closed (detected on candle).")
            try:
                await self.trader.cancel_all_orders(self.symbol)
            except Exception as e:
                logger.error(f"[{self.symbol}] Failed to cancel orders: {e}")
            await self._on_position_closed(candles, exit_price=candles[-1]["c"])
            return

        # Update exchange SL if it changed AND wasn't already synced by WS trailing
        if s.current_sl != s.initial_sl and s.current_sl != self._last_exchange_sl:
            arrow = '📈' if s.position_side == 'long' else '📉'
            logger.info(
                f"{arrow} [{self.symbol}] Candle sync exchange SL → {s.current_sl:.2f}"
            )
            await self.trader.replace_sl_tp(
                symbol=self.symbol,
                size=s.entry_size,
                side=s.position_side,
                sl_price=s.current_sl,
                tp_price=s.take_profit,
            )
            self._last_exchange_sl = s.current_sl

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
