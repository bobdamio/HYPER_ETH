"""
HYPER_ETH — HyperLiquid Trader
Handles order execution, position management, candle fetching.
Symbol-agnostic: all methods accept a `symbol` parameter.
"""

import asyncio
import logging
import os
import secrets
import time
from typing import Optional, Dict, List

from hyperliquid.utils.types import Cloid
from config.settings import STRATEGY, RISK, GLOBAL
from core.signer import HLSigner
from core.notifier import Notifier

logger = logging.getLogger("Trader")


class HLTrader:
    """Multi-symbol trader for HyperLiquid."""

    def __init__(self):
        self.signer = HLSigner()
        self.notifier = Notifier()
        self.exchange = self.signer.exchange
        self.info = self.signer.get_info()
        self._user_state_cache = None
        self._user_state_ts = 0.0
        self._user_state_ttl = 5.0  # cache TTL in seconds
        self._meta_cache = None
        self._meta_ts = 0.0
        self._meta_ttl = 300.0  # meta rarely changes
        self._setup_logger()

    # ── logging ──────────────────────────────────────────────
    def _setup_logger(self):
        os.makedirs(GLOBAL.LOG_DIR, exist_ok=True)
        if not logger.handlers:
            fh = logging.FileHandler(f"{GLOBAL.LOG_DIR}/trader.log")
            fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
            logger.addHandler(logging.StreamHandler())
            logger.setLevel(logging.INFO)

    # ── market data ──────────────────────────────────────────
    def get_candles(self, symbol: str, interval: str = None, limit: int = None) -> List[dict]:
        """
        Fetch recent candles from HL for given symbol.
        Returns list of dicts with keys: t, o, h, l, c, v (floats).
        """
        interval = interval or STRATEGY.TIMEFRAME
        limit = limit or GLOBAL.CANDLE_FETCH_LIMIT

        import time
        end_ms = int(time.time() * 1000)
        interval_ms = self._interval_to_ms(interval)
        start_ms = end_ms - interval_ms * limit

        raw = self.info.candles_snapshot(symbol, interval, start_ms, end_ms)

        candles = []
        for c in raw:
            candles.append({
                "t": int(c["t"]),
                "o": float(c["o"]),
                "h": float(c["h"]),
                "l": float(c["l"]),
                "c": float(c["c"]),
                "v": float(c["v"]),
            })
        return candles

    def get_mark_price(self, symbol: str) -> float:
        mids = self.info.all_mids()
        px = mids.get(symbol)
        if not px or float(px) == 0:
            raise ValueError(f"No price for {symbol}")
        return float(px)

    # ── cached helpers ────────────────────────────────────────────
    def _get_user_state_cached(self) -> dict:
        """Cached user_state — avoids repeated slow REST calls within a tick."""
        now = time.time()
        if self._user_state_cache is None or (now - self._user_state_ts) > self._user_state_ttl:
            self._user_state_cache = self.signer.get_user_state()
            self._user_state_ts = now
        return self._user_state_cache

    def _get_meta_cached(self) -> dict:
        """Cached meta — szDecimals etc rarely change."""
        now = time.time()
        if self._meta_cache is None or (now - self._meta_ts) > self._meta_ttl:
            self._meta_cache = self.info.meta()
            self._meta_ts = now
        return self._meta_cache

    def invalidate_cache(self):
        """Force refresh on next call (after order execution, etc)."""
        self._user_state_cache = None
        self._user_state_ts = 0.0

    # ── account ──────────────────────────────────────────────
    def get_equity(self) -> float:
        """Returns total equity (spot USDC preferred)."""
        state = self._get_user_state_cached()
        return float(state["marginSummary"]["accountValue"])

    def get_position(self, symbol: str) -> Optional[Dict]:
        """
        Returns current position dict for given symbol, or None.
        Keys: coin, szi, entryPx, positionValue, unrealizedPnl, ...
        """
        state = self._get_user_state_cached()
        for ap in state.get("assetPositions", []):
            p = ap["position"]
            if p["coin"] == symbol and float(p["szi"]) != 0:
                return p
        return None

    def get_sz_decimals(self, symbol: str) -> int:
        meta = self._get_meta_cached()
        for asset in meta["universe"]:
            if asset["name"] == symbol:
                return asset["szDecimals"]
        return 4

    def get_px_decimals(self, symbol: str) -> int:
        try:
            price = self.get_mark_price(symbol)
            if price < 1:
                return 4
            if price < 10:
                return 3
            if price < 100:
                return 2
            if price < 10000:
                return 1
            return 0
        except Exception:
            return 1

    # ── order execution ──────────────────────────────────────
    async def open_position(
        self, symbol: str, side: str, size: float, sl_price: float, tp_price: float
    ) -> Optional[Dict]:
        """
        Open position with SL + TP for given symbol.
        side: 'long' or 'short'
        size: position size in asset units
        Returns dict with entry info or None on failure.
        """
        is_buy = side.lower() == "long"
        sz_dec = self.get_sz_decimals(symbol)
        px_dec = self.get_px_decimals(symbol)
        size = round(size, sz_dec)

        if size <= 0:
            logger.error("Position size <= 0, aborting.")
            return None

        # Check no existing position
        existing = self.get_position(symbol)
        if existing:
            ex_side = "long" if float(existing["szi"]) > 0 else "short"
            if ex_side != side.lower():
                logger.error(f"Opposite position exists ({ex_side}). Cannot open {side}.")
                return None
            logger.warning(f"Same-side position already open ({ex_side}). Skipping entry.")
            return None

        # Set leverage
        try:
            self.exchange.update_leverage(STRATEGY.LEVERAGE, symbol)
        except Exception as e:
            logger.warning(f"Leverage update failed: {e}")

        # IOC limit order with slippage
        price = self.get_mark_price(symbol)
        slippage = 0.002  # 0.2%
        limit_px = price * (1 + slippage) if is_buy else price * (1 - slippage)
        limit_px = round(limit_px, px_dec)

        logger.info(
            f"⚔️ Opening {side.upper()} {symbol} | Size={size} | "
            f"Px≈{limit_px} | SL={sl_price} | TP={tp_price}"
        )

        try:
            res = await asyncio.to_thread(
                self.exchange.order,
                symbol, is_buy, size, limit_px,
                {"limit": {"tif": "Ioc"}},
            )
        except Exception as e:
            logger.error(f"Order execution error: {e}")
            return None

        if res.get("status") != "ok":
            logger.error(f"Order failed: {res}")
            return None

        statuses = res["response"]["data"]["statuses"]
        if not any("filled" in s for s in statuses):
            logger.error(f"Order not filled (IOC): {statuses}")
            return None

        # Extract fill price
        entry_price = self._extract_avg_price(statuses) or price
        filled_size = self._extract_filled_size(statuses) or size
        logger.info(f"✅ Filled {side.upper()} {symbol} @ {entry_price} | Size={filled_size}")

        # Invalidate cache — position state changed
        self.invalidate_cache()

        # Wait briefly for exchange state to settle
        await asyncio.sleep(0.5)

        # Place SL & TP trigger orders on exchange for protection
        await self._place_trigger(
            symbol=symbol,
            is_buy=not is_buy,
            size=filled_size,
            trigger_px=round(sl_price, px_dec),
            tpsl="sl",
        )
        await self._place_trigger(
            symbol=symbol,
            is_buy=not is_buy,
            size=filled_size,
            trigger_px=round(tp_price, px_dec),
            tpsl="tp",
        )

        return {
            "side": side.lower(),
            "entry_price": entry_price,
            "size": filled_size,
            "sl": sl_price,
            "tp": tp_price,
        }

    async def _place_trigger(self, symbol: str, is_buy: bool, size: float, trigger_px: float, tpsl: str):
        """Place a trigger (SL or TP) reduce-only order."""
        cloid = Cloid("0x" + secrets.token_hex(16))
        order_type = {
            "trigger": {
                "triggerPx": trigger_px,
                "isMarket": True,
                "tpsl": tpsl,
            },
            "reduceOnly": True,
        }
        try:
            result = await asyncio.to_thread(
                self.exchange.order,
                symbol, is_buy, size, trigger_px, order_type, cloid=cloid,
            )
            label = "🛡 SL" if tpsl == "sl" else "🎯 TP"

            # Check response status
            if result and result.get("status") == "ok":
                statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                if statuses and "error" in str(statuses):
                    logger.error(f"{label} placement REJECTED for {symbol} @ {trigger_px}: {statuses}")
                else:
                    logger.info(f"{label} placed @ {trigger_px} for {symbol}")
            elif result and result.get("status") == "err":
                logger.error(f"{label} ERROR for {symbol} @ {trigger_px}: {result.get('response', '?')}")
            else:
                logger.warning(f"{label} unclear response for {symbol} @ {trigger_px}: {result}")
        except Exception as e:
            logger.error(f"Failed to place {tpsl.upper()} @ {trigger_px} for {symbol}: {e}")

    async def update_sl(self, symbol: str, new_sl_price: float, size: float, side: str):
        """
        Cancel existing SL orders, then place a new one.
        side: 'long' or 'short' (the POSITION side, not the SL order side)
        """
        await self.cancel_all_orders(symbol)

        is_buy = side.lower() == "short"
        px_dec = self.get_px_decimals(symbol)

        await self._place_trigger(
            symbol=symbol,
            is_buy=is_buy,
            size=size,
            trigger_px=round(new_sl_price, px_dec),
            tpsl="sl",
        )

        # Also re-read existing TP and re-place it — we can't selectively cancel
        # So we need to pass TP externally. For now, just SL update is enough;
        # strategy.py will handle full re-placement.

    async def cancel_all_orders(self, symbol: str, max_retries: int = 3) -> bool:
        """Cancel all open orders (including trigger/SL/TP) for given symbol."""
        for attempt in range(max_retries):
            try:
                # Use frontend_open_orders which includes trigger orders (SL/TP)
                all_orders = self.info.frontend_open_orders(self.signer.address)
                sym_orders = [o for o in all_orders if o.get("coin") == symbol]

                if not sym_orders:
                    return True

                logger.info(f"Cancelling {len(sym_orders)} orders for {symbol}")
                for order in sym_orders:
                    try:
                        await asyncio.to_thread(
                            self.exchange.cancel, symbol, order["oid"]
                        )
                    except Exception as e:
                        logger.warning(f"Cancel failed for {order['oid']}: {e}")

                await asyncio.sleep(0.5)

                remaining = [
                    o for o in self.info.frontend_open_orders(self.signer.address)
                    if o.get("coin") == symbol
                ]
                if not remaining:
                    return True

            except Exception as e:
                logger.error(f"Cancel attempt {attempt+1} error: {e}")

        logger.error(f"Failed to cancel all {symbol} orders after retries")
        return False

    async def close_position(self, symbol: str) -> bool:
        """Market-close current position for given symbol."""
        self.invalidate_cache()  # ensure fresh read
        pos = self.get_position(symbol)
        if not pos:
            logger.info(f"No position to close for {symbol}.")
            await self.cancel_all_orders(symbol)
            return True

        szi = float(pos["szi"])
        is_buy = szi < 0
        size = abs(szi)

        await self.cancel_all_orders(symbol)

        try:
            res = await asyncio.to_thread(
                self.exchange.market_open,
                symbol, is_buy, size, px=None, slippage=0.01,
            )
            if res.get("status") == "ok":
                logger.info(f"✅ Position closed for {symbol} (size={size})")
                return True
        except Exception as e:
            logger.error(f"Market close failed for {symbol}: {e}")

        # Fallback: limit order
        try:
            price = self.get_mark_price(symbol)
            limit_px = round(price * (1.005 if is_buy else 0.995), self.get_px_decimals(symbol))
            res = await asyncio.to_thread(
                self.exchange.order,
                symbol, is_buy, size, limit_px,
                {"limit": {"tif": "Gtc"}},
            )
            return res.get("status") == "ok"
        except Exception as e:
            logger.error(f"Fallback close failed for {symbol}: {e}")
            return False

    async def replace_sl_tp(self, symbol: str, size: float, side: str, sl_price: float, tp_price: float):
        """
        Cancel all orders and re-place both SL and TP.
        side: position side ('long' or 'short').
        """
        await self.cancel_all_orders(symbol)

        is_close_buy = side.lower() == "short"
        px_dec = self.get_px_decimals(symbol)

        await self._place_trigger(
            symbol=symbol,
            is_buy=is_close_buy,
            size=size,
            trigger_px=round(sl_price, px_dec),
            tpsl="sl",
        )
        await self._place_trigger(
            symbol=symbol,
            is_buy=is_close_buy,
            size=size,
            trigger_px=round(tp_price, px_dec),
            tpsl="tp",
        )

    # ── helpers ──────────────────────────────────────────────
    @staticmethod
    def _extract_avg_price(statuses) -> Optional[float]:
        total_sz = total_val = 0
        for s in statuses:
            if "filled" in s:
                sz = float(s["filled"]["totalSz"])
                px = float(s["filled"]["avgPx"])
                total_sz += sz
                total_val += sz * px
        return round(total_val / total_sz, 2) if total_sz else None

    @staticmethod
    def _extract_filled_size(statuses) -> Optional[float]:
        total = 0
        for s in statuses:
            if "filled" in s:
                total += float(s["filled"]["totalSz"])
        return total if total > 0 else None

    @staticmethod
    def _interval_to_ms(interval: str) -> int:
        units = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
        return units.get(interval, 900_000)
