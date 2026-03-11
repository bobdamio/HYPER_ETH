"""
HYPER_ETH — Settings
Exact translation of ETH_main.pine strategy parameters.
Multi-symbol: ETH, HYPE, ICP, BNB.
"""


class STRATEGY:
    """Core strategy parameters — mirrors Pine Script inputs."""

    SYMBOLS = ["ETH"]  # Only ETH — other coins unprofitable
    TIMEFRAME = "15m"           # 15-minute candles
    LEVERAGE = 10

    # Pivot-based Order Blocks
    PIVOT_LENGTH = 6            # pivotLen = 6

    # ATR & SL/TP
    ATR_LENGTH = 14             # atrLen = 14
    SL_MULTIPLIER = 1.5         # slMultiplier = 1.5 (x ATR)
    RR_RATIO = 3.2              # rrRatio = 3.2

    # FVG
    USE_FVG = True              # useFVG = true
    FVG_LOOKBACK = 10           # fvgLookback = 10 bars

    # Strong candle filter
    STRONG_CANDLE_ATR_RATIO = 0.35  # body > 0.35 * ATR

    # Breakeven & Trailing
    TRAIL_ACTIVATION_RATIO = 0.3    # 30% of TP distance
    TRAIL_OFFSET_RATIO = 0.2        # 20% of SL distance

    # Cooldown
    COOLDOWN_BARS = 0           # cooldownBars = 0 in Pine


class RISK:
    """Smooth dynamic risk — mirrors Pine Script risk management."""

    BASE_RISK_PCT = 5.0         # baseRisk = 5%
    MIN_RISK_PCT = 2.0          # minRisk = 2%
    MAX_RISK_PCT = 10.0         # maxRisk = 10%
    ADJUSTMENT_RATE = 10.0      # adjustmentRate = 10% per trade

    # Hard limits
    MIN_POSITION_USD = 11.0     # HyperLiquid minimum notional
    MAX_POSITION_USD = 600.0    # Max single position
    MAX_EQUITY_USAGE = 0.95     # maxSize = equity * 0.95


class GLOBAL:
    DATA_DIR = "data"
    LOG_DIR = "logs"
    TRADES_FILE = "data/trades.json"
    MAX_TRADE_HISTORY = 500
    LOOP_INTERVAL = 15          # seconds between checks (sub-candle polling)
    CANDLE_FETCH_LIMIT = 300    # candles to fetch (300 for stable EMA50)
