import os
from dotenv import load_dotenv

load_dotenv()

# --- Alpaca ---
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")

# --- Anthropic ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- Portfolio allocation ---
TARGET_STOCK_PCT = 0.60
TARGET_CRYPTO_PCT = 0.25
TARGET_OPTIONS_PCT = 0.15
MAX_POSITIONS = 5

# --- Risk management ---
STOP_LOSS_PCT = 0.08       # Close position if down 8%
TAKE_PROFIT_PCT = 0.07     # Close position if up 7%
TRAILING_STOP_PCT = 0.04   # Trailing stop once up 5% (locks in gains)
TRAILING_ACTIVATE_PCT = 0.05  # Activate trailing stop after 5% gain
MAX_POSITION_PCT = 0.22    # Max 22% of portfolio in a single position

# --- Scheduling ---
SCAN_INTERVAL_MINUTES = 30     # How often to scan for new trades
CRYPTO_SCAN_INTERVAL_MINUTES = 60  # Crypto scans (24/7)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MINUTE = 0

# --- Sentiment thresholds ---
MIN_SENTIMENT_SCORE = 0.65     # Min confidence to open a position (0-1)
MIN_SENTIMENT_MENTIONS = 3     # Min number of source mentions to consider

# --- Watchlists ---
WATCHLIST = {
    "stocks_ai": [
        "NVDA", "MSFT", "GOOGL", "META", "PLTR", "SMCI",
        "AI", "BBAI", "SOUN", "IONQ", "RGTI", "QUBT", "QBTS"
    ],
    "stocks_quantum": [
        "IONQ", "RGTI", "QUBT", "QBTS", "IBM", "HON"
    ],
    "stocks_minerals": [
        "ALB", "SQM", "LAC", "LTHM", "MP", "UUUU", "PLTM"
    ],
    "crypto": [
        "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD", "DOT/USD"
    ],
}

# All stock symbols flat
ALL_STOCK_SYMBOLS = list(set(
    WATCHLIST["stocks_ai"] +
    WATCHLIST["stocks_quantum"] +
    WATCHLIST["stocks_minerals"]
))

ALL_CRYPTO_SYMBOLS = WATCHLIST["crypto"]

# Tickers that are also common English words — require $TICKER prefix to avoid false positives
DOLLAR_SIGN_ONLY_TICKERS = {"AI", "LINK", "DOT", "SOL", "LAC"}

# Sector keywords for Reddit/news scraping
SECTOR_KEYWORDS = [
    "artificial intelligence", "AI stocks", "quantum computing", "quantum stocks",
    "lithium", "cobalt", "rare earth", "strategic minerals", "EV battery",
    "NVDA", "PLTR", "IONQ", "RGTI", "ALB", "SQM", "LAC",
    "SMCI", "QUBT", "QBTS", "BBAI"
]

# Subreddits to monitor
REDDIT_SUBS = [
    "wallstreetbets", "stocks", "investing", "algotrading",
    "MachineLearning", "QuantumComputing", "CryptoCurrency",
    "Superstonk", "StockMarket"
]

# Options config
OPTIONS_MAX_DTE = 14       # Max days to expiration for options plays
OPTIONS_MIN_DTE = 2        # Min days to expiration
OPTIONS_TARGET_DELTA = 0.40  # Target delta for calls/puts (~ATM)
