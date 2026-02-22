# HYPER_ETH

Automated trading bot for **ETH perpetual futures** on [HyperLiquid](https://hyperliquid.xyz/).  
Exact translation of the ETH Trading strategy into a production system with real-time WebSocket execution.

## Strategy

**SMC (Smart Money Concepts)** — Order Blocks + Fair Value Gaps with smooth dynamic risk management.

| Parameter | Value | Description |
|-----------|-------|-------------|
| Timeframe | 15m | 15-minute candles |
| Leverage | 5x | Cross margin |
| SL | 1.5× ATR(14) | Stop-loss distance |
| R:R | 3.2 | Risk-to-reward ratio |
| Trail activation | 30% of TP | When trailing SL kicks in |
| Trail offset | 20% of SL distance | Trailing SL distance from peak |
| Base risk | 5% | Starting risk per trade |
| Risk range | 2%–10% | Smooth adjustment ±10% per trade |

### Entry Logic

1. **Order Block (OB)** — Pivot high/low detection (length=6). Long when price dips below bullish OB and closes above it; short when price pokes above bearish OB and closes below.
2. **Fair Value Gap (FVG)** — Gap between candle bodies (lookback=10 bars). Long when price enters bullish FVG zone with bullish close above EMA50; short mirror.
3. **Strong candle filter** — Candle body must exceed 35% of ATR to confirm momentum.

### Exit Logic

- **SL/TP** placed as trigger orders on HyperLiquid exchange (not software-managed)
- **Breakeven** — SL moves to zone level (OB/FVG) once price moves 1× SL distance in favor
- **Trailing SL** — Activates at 30% of TP distance, trails at 20% of SL distance from peak price
- **Emergency backup** — If price stays past exchange SL for 30s (trigger order failed), force market close

### Risk Management

Smooth dynamic risk — mirrors Pine Script exactly:
- Win → `risk × 1.10` (capped at 10%)
- Loss → `risk × 0.90` (floored at 2%)

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    run.py (Main Loop)                │
├─────────────────────────────────────────────────────┤
│                                                     │
│  ┌─────────────┐    ┌──────────────┐                │
│  │ WS AllMids  │───▶│ on_price_    │  Real-time     │
│  │ (every tick)│    │ update()     │  trailing SL   │
│  └─────────────┘    └──────────────┘  + emergency   │
│                                                     │
│  ┌─────────────┐    ┌──────────────┐                │
│  │ WS Order    │───▶│ on_order_    │  SL/TP fill    │
│  │ Updates     │    │ triggered()  │  detection     │
│  └─────────────┘    └──────────────┘                │
│                                                     │
│  ┌─────────────┐    ┌──────────────┐                │
│  │ WS User     │───▶│ on_user_     │  Actual fill   │
│  │ Fills       │    │ fill()       │  price cache   │
│  └─────────────┘    └──────────────┘                │
│                                                     │
│  ┌─────────────┐    ┌──────────────┐                │
│  │ WS Candle   │───▶│ tick()       │  Indicators    │
│  │ (15m close) │    │              │  + signals     │
│  └─────────────┘    └──────────────┘                │
│                                                     │
│  ┌─────────────┐    ┌──────────────┐                │
│  │ REST poll   │───▶│ tick()       │  Safety net    │
│  │ (60s)       │    │              │  if WS misses  │
│  └─────────────┘    └──────────────┘                │
│                                                     │
└─────────────────────────────────────────────────────┘
```

### WebSocket Feeds

| Channel | Purpose | Callback |
|---------|---------|----------|
| `allMids` | Real-time mid prices | Trailing SL, breakeven, emergency close |
| `orderUpdates` | SL/TP trigger fills | Position close detection |
| `userFills` | Actual execution prices | Fill price cache (avoids limitPx bug) |
| `candle` (15m) | Candle close detection | OB/FVG indicators, entry signals |

### Key Design Decisions

- **Exchange-side SL/TP** — Trigger orders placed on HyperLiquid, not software stops. Survives bot crashes.
- **WS-first, REST-fallback** — All real-time data from WebSocket. REST polling every 60s as safety net.
- **Fill price from `userFills` WS** — OrderUpdates `limitPx` for trigger orders is the far limit (10% away), not the actual fill. `userFills` provides the real `px`.
- **Emergency close with grace** — 30s timeout, 5s grace after exchange SL update, compares against actual exchange SL (not internal trailing).

## Project Structure

```
HYPER_ETH/
├── run.py                  # Main loop, WS callbacks, RED BUTTON
├── core/
│   ├── strategy.py         # SMC strategy engine (OB, FVG, trailing, risk)
│   ├── trader.py           # Order execution, position management
│   ├── ws_manager.py       # WebSocket subscriptions & dispatch
│   ├── signer.py           # HyperLiquid wallet/signing
│   └── notifier.py         # Telegram notifications
├── config/
│   ├── settings.py         # Strategy parameters (mirrors Pine)
│   └── credentials.py      # Environment variable loader
├── data/                   # Persistent state (volume-mounted)
│   ├── state_ETH.json      # Position state, risk, win/loss
│   └── trades.json         # Trade history log
├── logs/                   # Log files (volume-mounted)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Setup

### Prerequisites

- Python 3.11+
- Docker (recommended)
- HyperLiquid account with API wallet
- Telegram bot (optional, for notifications)

### Environment Variables

Create `.env` in the project root:

```env
HL_WALLET_ADDRESS=0xYourWalletAddress
HL_PRIVATE_KEY=0xYourPrivateKey
HL_IS_MAINNET=True

# Optional: Telegram notifications
TELEGRAM_BOT_TOKEN=123456:ABC-...
TELEGRAM_CHAT_ID=-100...
```

### Docker (Recommended)

```bash
# Build and run
docker compose up -d

# Or manually:
docker build -t hyper_eth .
docker run -d \
  --name hyper_eth \
  --restart unless-stopped \
  --env-file .env \
  -v ./data:/app/data \
  -v ./logs:/app/logs \
  hyper_eth
```

### Local

```bash
pip install -r requirements.txt
python run.py
```

## Operations

### Monitoring

```bash
# Live logs
docker logs -f hyper_eth

# Recent trades
docker logs hyper_eth 2>&1 | grep "Trade Closed"

# Signal checks
docker logs hyper_eth 2>&1 | grep "Signal:"

# Emergency events
docker logs hyper_eth 2>&1 | grep "EMERGENCY"
```

### RED BUTTON (Emergency Stop)

The bot has a built-in RED BUTTON that triggers when equity drops significantly. It:
1. Probes exchange health first (skips during HL outages — 502/504)
2. Closes all positions via `market_close`
3. Cancels all open orders
4. Verifies position is actually closed before clearing state

### Redeployment

```bash
cd HYPER_ETH
docker build -t hyper_eth .
docker stop hyper_eth && docker rm hyper_eth
docker run -d --name hyper_eth --restart unless-stopped \
  --env-file .env -v ./data:/app/data -v ./logs:/app/logs hyper_eth
```

State persists across restarts via volume-mounted `data/` directory. The bot restores position state, risk level, and win/loss counters on startup.

## Notifications

Telegram messages for:
- **Signal detected** — Side, source (OB/FVG), entry price, SL, TP, size, risk %
- **Trade closed** — Side, entry→exit, PnL %, updated risk, W/L record
- **Startup** — Symbols, equity, active positions
- **Emergency events** — RED BUTTON, exchange outages

## Dependencies

| Package | Purpose |
|---------|---------|
| `hyperliquid-python-sdk` | HL REST + WebSocket API |
| `eth-account` | Wallet signing |
| `aiohttp` | Async HTTP (Telegram) |
| `python-dotenv` | Environment variables |
| `requests` | REST fallback |

## License

Private repository. All rights reserved.
