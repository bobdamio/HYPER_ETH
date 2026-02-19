"""
HYPER_ETH — Main Runner (Full WebSocket)

Architecture:
  - WebSocket AllMids:      real-time trailing SL, breakeven, emergency backup (every tick)
  - WebSocket OrderUpdates: instant detection of exchange SL/TP trigger fills
  - WebSocket Candle:       instant candle close detection → indicators + entry signals
  - Polling fallback:       REST candle check every 60s as safety net if WS misses a close
"""

import asyncio
import logging
import os
import sys
import signal
import time

from config.settings import STRATEGY, GLOBAL
from core.trader import HLTrader
from core.notifier import Notifier
from core.strategy import StrategyEngine
from core.ws_manager import HLWebSocketManager

# ── Logging ──────────────────────────────────────────────────
os.makedirs(GLOBAL.LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(f"{GLOBAL.LOG_DIR}/bot.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("HYPER_ETH")
logger.setLevel(logging.INFO)

# Strategy logger at DEBUG to capture signal checks
logging.getLogger("Strategy").setLevel(logging.DEBUG)
# Keep other loggers at INFO to avoid noise
logging.getLogger("Trader").setLevel(logging.INFO)
logging.getLogger("WS").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ── Globals ──────────────────────────────────────────────────
RUNNING = True


def shutdown_handler(sig, frame):
    global RUNNING
    logger.info(f"Received signal {sig}, shutting down...")
    RUNNING = False


signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)


# ── WebSocket Callbacks ─────────────────────────────────────

def make_price_handler(engines: dict):
    """
    Returns a callback for AllMids WS events.
    Called from WS thread — returns coroutine if exchange SL needs updating.
    """
    def on_price(symbol: str, price: float):
        engine = engines.get(symbol)
        if engine is None:
            return None
        # on_price_update is sync, returns coroutine for SL update or emergency
        return engine.on_price_update(price)
    return on_price


def make_order_handler(engines: dict):
    """
    Returns a callback for OrderUpdates WS events.
    Called from WS thread — returns coroutine for async handling.
    """
    def on_order(order_data: dict):
        coin = order_data.get("coin", "")
        engine = engines.get(coin)
        if engine is None:
            return None
        return engine.on_order_triggered(order_data)
    return on_order


def make_candle_handler(engines: dict):
    """
    Returns a callback for Candle WS close events.
    Called from WS thread when a candle closes (new candle timestamp detected).
    Returns coroutine → dispatched to asyncio loop.
    """
    def on_candle_close(symbol: str, candles: list):
        engine = engines.get(symbol)
        if engine is None:
            return None
        logger.info(f"⚡ [{symbol}] WS candle close → tick()")
        return engine.tick(candles)
    return on_candle_close


# ── Main Loop ────────────────────────────────────────────────
async def main():
    global RUNNING

    symbols = STRATEGY.SYMBOLS

    logger.info("=" * 50)
    logger.info("🚀 HYPER_ETH Starting (Full WebSocket mode)...")
    logger.info(f"   Symbols:   {', '.join(symbols)}")
    logger.info(f"   Timeframe: {STRATEGY.TIMEFRAME}")
    logger.info(f"   Leverage:  {STRATEGY.LEVERAGE}x")
    logger.info(f"   R:R Ratio: {STRATEGY.RR_RATIO}")
    logger.info(f"   SL:        {STRATEGY.SL_MULTIPLIER}x ATR")
    logger.info(f"   FVG:       {'ON' if STRATEGY.USE_FVG else 'OFF'}")
    logger.info("=" * 50)

    trader = HLTrader()
    notifier = Notifier()

    # One engine per symbol — separate state, OB/FVG, risk per coin
    engines: dict[str, StrategyEngine] = {}
    for sym in symbols:
        engines[sym] = StrategyEngine(sym, trader, notifier)
        logger.info(f"   ✅ Engine created for {sym}")

    # ── Start WebSocket ──
    loop = asyncio.get_running_loop()
    ws = HLWebSocketManager(loop)

    ws.on_price(make_price_handler(engines))
    ws.on_order(make_order_handler(engines))
    ws.on_candle_close(make_candle_handler(engines))

    try:
        ws.start()
        logger.info("🔌 WebSocket connected — real-time price + order + candle feeds active")
    except Exception as e:
        logger.error(f"WebSocket start failed: {e} — falling back to polling-only mode")

    # Startup info
    try:
        equity = trader.get_equity()
        logger.info(f"💰 Equity: ${equity:.2f}")

        prices = {}
        positions_info = []
        for sym in symbols:
            try:
                px = trader.get_mark_price(sym)
                prices[sym] = px
                logger.info(f"💲 {sym}: ${px:.2f}")
            except Exception as e:
                logger.warning(f"Could not get price for {sym}: {e}")

            pos = trader.get_position(sym)
            if pos:
                szi = float(pos["szi"])
                side = "LONG" if szi > 0 else "SHORT"
                positions_info.append(f"{sym}: {side} {abs(szi)} @ {pos['entryPx']}")
                logger.info(f"📍 {sym}: {side} {abs(szi)} @ {pos['entryPx']}")

        if not positions_info:
            logger.info("📍 No active positions.")

        prices_str = " | ".join(f"{s}: `${p:.2f}`" for s, p in prices.items())
        await notifier.send(
            f"🚀 *HYPER\\_ETH Started (Full WS)*\n"
            f"Symbols: `{', '.join(symbols)}`\n"
            f"Equity: `${equity:.2f}`\n"
            f"{prices_str}\n"
            f"Timeframe: `{STRATEGY.TIMEFRAME}` | Leverage: `{STRATEGY.LEVERAGE}x`"
        )
    except Exception as e:
        logger.error(f"Startup check failed: {e}")
        await notifier.send(f"⚠️ *HYPER\\_ETH* startup warning: `{e}`")

    # Fetch candles via REST once for warmup + seed WS candle buffer
    logger.info("📚 Fetching candles & running warmup...")
    for sym in symbols:
        try:
            candles = trader.get_candles(sym)
            if candles and len(candles) > 30:
                engines[sym].warmup(candles)
                # Seed WS candle buffer with REST data
                ws.seed_candles(sym, candles)
                logger.info(f"[{sym}] Candle buffer seeded: {len(candles)} candles (last t={candles[-1]['t']})")
            else:
                logger.warning(f"[{sym}] Not enough candles for warmup: {len(candles) if candles else 0}")
        except Exception as e:
            logger.error(f"[{sym}] Warmup failed: {e}")
    logger.info("📚 Warmup complete — WS candle close will trigger ticks.")

    # ── Main loop (heartbeat + fallback candle check) ──
    # WS handles: real-time prices, order fills, candle close detection
    # This loop is just: heartbeat logging + WS health + fallback REST candle check
    tick_count = 0
    last_heartbeat = time.time()
    last_fallback_check = time.time()
    HEARTBEAT_INTERVAL = 120       # seconds between heartbeat logs
    FALLBACK_CHECK_INTERVAL = 60   # REST candle fallback check every 60s

    while RUNNING:
        try:
            now = time.time()

            # ── Fallback: REST candle check (safety net if WS candle missed a close)
            if now - last_fallback_check >= FALLBACK_CHECK_INTERVAL:
                last_fallback_check = now
                for sym in symbols:
                    try:
                        candles = trader.get_candles(sym)
                        if not candles or len(candles) < 30:
                            continue
                        # Update WS buffer with fresh REST data if needed
                        ws_buf = ws.get_candles(sym)
                        rest_last_t = candles[-2]["t"]  # last closed candle
                        ws_last_closed_t = ws_buf[-2]["t"] if ws_buf and len(ws_buf) >= 2 else 0

                        if rest_last_t > ws_last_closed_t:
                            # WS missed a candle close — run tick with REST data
                            logger.warning(
                                f"⚠️ [{sym}] WS missed candle close "
                                f"(REST: {rest_last_t}, WS: {ws_last_closed_t}) — fallback tick"
                            )
                            ws.seed_candles(sym, candles)  # resync buffer
                            await engines[sym].tick(candles)
                        # else: WS is up to date — no action needed

                    except Exception as e:
                        logger.error(f"[{sym}] Fallback check error: {e}", exc_info=True)

            tick_count += 1

            # Heartbeat every 2 minutes
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                last_heartbeat = now
                try:
                    equity = trader.get_equity()
                except Exception:
                    equity = 0.0

                ws_status = "WS:OK" if ws.connected else "WS:DOWN"
                ws_age = ""
                if ws._last_price_time > 0:
                    age = now - ws._last_price_time
                    ws_age = f" ({age:.0f}s ago)"

                statuses = []
                for sym in symbols:
                    eng = engines[sym]
                    s = eng.state
                    ws_px = ws.get_price(sym)
                    buf = ws.get_candles(sym)
                    buf_len = len(buf) if buf else 0
                    if s.in_position:
                        px_str = f" px={ws_px:.2f}" if ws_px else ""
                        pos_str = f"{s.position_side.upper()} @ {s.entry_price:.2f}{px_str}"
                    else:
                        pos_str = "FLAT"
                    statuses.append(f"{sym}:{pos_str} buf={buf_len}")
                logger.info(
                    f"💓 #{tick_count} | ${equity:.2f} | {ws_status}{ws_age} | "
                    + " | ".join(statuses)
                )

                # WS health check — reconnect if stale
                if ws._last_price_time > 0 and (now - ws._last_price_time) > 60:
                    logger.warning("⚠️ WebSocket stale (>60s) — reconnecting...")
                    try:
                        ws.stop()
                        await asyncio.sleep(2)
                        ws.start()
                        # Re-seed candle buffers after reconnect
                        for sym in symbols:
                            try:
                                candles = trader.get_candles(sym)
                                if candles and len(candles) > 30:
                                    ws.seed_candles(sym, candles, quiet=True)
                            except Exception:
                                pass
                        logger.info("🔌 WebSocket reconnected + candle buffers re-seeded")
                    except Exception as e:
                        logger.error(f"WS reconnect failed: {e}")

        except KeyboardInterrupt:
            RUNNING = False
            break
        except Exception as e:
            logger.error(f"Loop error: {e}", exc_info=True)
            await asyncio.sleep(5)
            continue

        await asyncio.sleep(GLOBAL.LOOP_INTERVAL)

    # Shutdown
    ws.stop()
    logger.info("🛑 HYPER_ETH Stopped.")
    await notifier.send("🛑 *HYPER\\_ETH Stopped*")


if __name__ == "__main__":
    asyncio.run(main())
