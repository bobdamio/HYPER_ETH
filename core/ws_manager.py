"""
HYPER_ETH — WebSocket Manager
Real-time price feeds, order updates, and candle data via HL WebSocket API.

Subscriptions:
  - AllMids: real-time mid prices for all coins (trailing SL, bot backup)
  - OrderUpdates: instant notification when our SL/TP triggers execute
  - Candle: real-time OHLCV updates → instant candle close detection
"""

import asyncio
import logging
import threading
import time
from typing import Dict, Callable, Optional, Set, List

from hyperliquid.info import Info
from hyperliquid.utils import constants
from config.credentials import Config
from config.settings import STRATEGY, GLOBAL

logger = logging.getLogger("WS")


class HLWebSocketManager:
    """
    Manages WebSocket subscriptions to HyperLiquid.
    Runs in a background thread, dispatches events to asyncio loop.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.symbols: Set[str] = set(STRATEGY.SYMBOLS)
        self._prices: Dict[str, float] = {}
        self._on_price_cbs: list[Callable] = []
        self._on_order_cbs: list[Callable] = []
        self._on_fill_cbs: list[Callable] = []
        self._on_candle_close_cbs: list[Callable] = []
        self._info: Optional[Info] = None
        self._connected = False
        self._last_price_time: float = 0

        # Candle buffer: per-symbol list of candles [{t, o, h, l, c, v}, ...]
        self._candle_buffers: Dict[str, List[dict]] = {}
        self._candle_max_len = GLOBAL.CANDLE_FETCH_LIMIT + 50  # keep some extra

    def start(self):
        """Create Info with WebSocket enabled and subscribe."""
        base_url = (
            constants.MAINNET_API_URL if Config.IS_MAINNET
            else constants.TESTNET_API_URL
        )
        try:
            self._info = Info(base_url, skip_ws=False)
            logger.info("WebSocket connection established")
        except Exception as e:
            logger.error(f"WebSocket init failed: {e}")
            return

        # Subscribe to all mid prices
        self._info.subscribe(
            {"type": "allMids"},
            self._on_all_mids,
        )
        logger.info("Subscribed to AllMids (real-time prices)")

        # Subscribe to order updates for our wallet
        wallet = Config.WALLET_ADDRESS
        self._info.subscribe(
            {"type": "orderUpdates", "user": wallet},
            self._on_order_update,
        )
        logger.info(f"Subscribed to OrderUpdates for {wallet[:10]}...")

        # Subscribe to user fills for real execution prices
        self._info.subscribe(
            {"type": "userFills", "user": wallet},
            self._on_user_fills,
        )
        logger.info(f"Subscribed to UserFills for {wallet[:10]}...")

        # Subscribe to candle updates for each symbol
        interval = STRATEGY.TIMEFRAME
        for sym in self.symbols:
            self._info.subscribe(
                {"type": "candle", "coin": sym, "interval": interval},
                self._on_candle_msg,
            )
            logger.info(f"Subscribed to Candle WS for {sym}/{interval}")

        self._connected = True

    def stop(self):
        """Disconnect WebSocket."""
        if self._info and self._info.ws_manager:
            try:
                self._info.disconnect_websocket()
            except Exception:
                pass
        self._connected = False
        logger.info("WebSocket disconnected")

    @property
    def connected(self) -> bool:
        return self._connected

    def get_price(self, symbol: str) -> Optional[float]:
        """Get latest mid price for a symbol (from WS cache)."""
        return self._prices.get(symbol)

    def on_price(self, callback: Callable):
        """Register callback for price updates: callback(symbol, price)"""
        self._on_price_cbs.append(callback)

    def on_order(self, callback: Callable):
        """Register callback for order updates: callback(order_data)"""
        self._on_order_cbs.append(callback)

    def on_fill(self, callback: Callable):
        """Register callback for user fills: callback(fill_data)"""
        self._on_fill_cbs.append(callback)

    def on_candle_close(self, callback: Callable):
        """Register callback for candle close: callback(symbol, candles)"""
        self._on_candle_close_cbs.append(callback)

    def seed_candles(self, symbol: str, candles: List[dict], quiet: bool = False):
        """
        Seed/resync candle buffer from REST API.
        candles: list of {t, o, h, l, c, v} sorted by time ascending.
        Thread-safe: replaces buffer atomically and updates in-place to avoid
        WS thread holding stale reference.
        """
        old_buf = self._candle_buffers.get(symbol)
        new_buf = list(candles)
        self._candle_buffers[symbol] = new_buf
        # Also update old buffer in-place so WS thread's reference stays valid
        if old_buf is not None:
            old_buf.clear()
            old_buf.extend(new_buf)
        if not quiet:
            logger.info(f"Candle buffer seeded for {symbol}: {len(candles)} candles")

    def get_candles(self, symbol: str) -> Optional[List[dict]]:
        """Get current candle buffer for a symbol."""
        buf = self._candle_buffers.get(symbol)
        if buf:
            return list(buf)  # return copy
        return None

    # ── WS callbacks (called from WS thread) ─────────────────

    def _on_all_mids(self, msg: dict):
        """
        AllMids message: {'channel': 'allMids', 'data': {'mids': {'ETH': '1950.5', ...}}}
        Called from WS thread — dispatch to asyncio loop.
        """
        try:
            data = msg.get("data", {})
            mids = data.get("mids", {})

            for sym in self.symbols:
                if sym in mids:
                    price = float(mids[sym])
                    old = self._prices.get(sym)
                    self._prices[sym] = price

                    # Only dispatch if price actually changed
                    if old is None or old != price:
                        for cb in self._on_price_cbs:
                            try:
                                result = cb(sym, price)
                                # If callback returns a coroutine, schedule it
                                if asyncio.iscoroutine(result):
                                    asyncio.run_coroutine_threadsafe(result, self.loop)
                            except Exception as e:
                                logger.error(f"Price callback error: {e}")

            self._last_price_time = time.time()
        except Exception as e:
            logger.error(f"AllMids parse error: {e}")

    def _on_order_update(self, msg: dict):
        """
        OrderUpdates message: {'channel': 'orderUpdates', 'data': [...]}
        Each item: {'order': {'coin': 'ETH', 'side': 'A', ...}, 'status': 'filled'|'canceled'|...}
        Called from WS thread.
        """
        try:
            data = msg.get("data", [])
            logger.info(
                f"📋 WS OrderUpdate received: {len(data)} item(s) — "
                + ", ".join(
                    f"{item.get('order', {}).get('coin', '?')}:{item.get('status', '?')}"
                    for item in data
                )
            )
            for order in data:
                for cb in self._on_order_cbs:
                    try:
                        result = cb(order)
                        if asyncio.iscoroutine(result):
                            asyncio.run_coroutine_threadsafe(result, self.loop)
                    except Exception as e:
                        logger.error(f"Order callback error: {e}")
        except Exception as e:
            logger.error(f"OrderUpdate parse error: {e}")

    def _on_user_fills(self, msg: dict):
        """
        UserFills message: {'channel': 'userFills', 'data': {'isSnapshot': bool, 'user': str, 'fills': [...]}}
        Each fill: {coin, px, sz, side, time, dir, closedPnl, oid, ...}
        The 'px' field is the ACTUAL execution price (unlike orderUpdates limitPx).
        Called from WS thread.
        """
        try:
            data = msg.get("data", {})
            if data.get("isSnapshot"):
                return  # Skip initial snapshot

            fills = data.get("fills", [])
            for fill in fills:
                coin = fill.get("coin", "")
                if coin not in self.symbols:
                    continue
                logger.info(
                    f"💰 WS UserFill: {coin} {fill.get('dir', '?')} "
                    f"px={fill.get('px', '?')} sz={fill.get('sz', '?')} "
                    f"oid={fill.get('oid', '?')} pnl={fill.get('closedPnl', '?')}"
                )
                for cb in self._on_fill_cbs:
                    try:
                        result = cb(fill)
                        if asyncio.iscoroutine(result):
                            asyncio.run_coroutine_threadsafe(result, self.loop)
                    except Exception as e:
                        logger.error(f"Fill callback error: {e}")
        except Exception as e:
            logger.error(f"UserFills parse error: {e}")

    def _on_candle_msg(self, msg: dict):
        """
        Candle message: {'channel': 'candle', 'data': {'t': ..., 'T': ..., 's': 'ETH', 'i': '15m', 'o': ..., 'c': ..., 'h': ..., 'l': ..., 'v': ..., 'n': ...}}
        Called from WS thread.
        Detects candle close: when new candle timestamp arrives, the previous candle is complete.
        """
        try:
            data = msg.get("data", {})
            sym = data.get("s", "")
            if sym not in self.symbols:
                return

            candle = {
                "t": int(data["t"]),
                "o": float(data["o"]),
                "h": float(data["h"]),
                "l": float(data["l"]),
                "c": float(data["c"]),
                "v": float(data["v"]),
            }

            buf = self._candle_buffers.get(sym)
            if buf is None or len(buf) == 0:
                # Buffer not seeded yet — ignore until warmup provides it
                return

            last_t = buf[-1]["t"]
            new_t = candle["t"]

            if new_t == last_t:
                # Same candle — update OHLCV in place (forming candle)
                buf[-1] = candle
            elif new_t > last_t:
                # New candle arrived → previous candle is now CLOSED
                # Append the new forming candle
                buf.append(candle)

                # Trim buffer to max length
                if len(buf) > self._candle_max_len:
                    excess = len(buf) - self._candle_max_len
                    del buf[:excess]

                # Fire candle close callbacks with full buffer
                # (buf[-1] is the new forming candle, buf[-2] is the just-closed candle)
                candles_copy = list(buf)
                for cb in self._on_candle_close_cbs:
                    try:
                        result = cb(sym, candles_copy)
                        if asyncio.iscoroutine(result):
                            asyncio.run_coroutine_threadsafe(result, self.loop)
                    except Exception as e:
                        logger.error(f"Candle close callback error: {e}")

                logger.info(
                    f"🕯️ [{sym}] Candle closed: "
                    f"O={buf[-2]['o']:.2f} H={buf[-2]['h']:.2f} "
                    f"L={buf[-2]['l']:.2f} C={buf[-2]['c']:.2f} | "
                    f"Buffer: {len(buf)} candles"
                )
            # else: stale candle (new_t < last_t) — ignore

        except Exception as e:
            logger.error(f"Candle parse error: {e}")
