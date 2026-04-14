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

# Model used for bulk sentiment analysis (runs every 30 min — keep it cheap).
# Options:
#   claude-3-5-haiku-20241022       ~$0.23/day  (default, recommended)
#   claude-sonnet-4-6               ~$0.86/day  (overkill for JSON parsing)
#   openrouter/<model>              free tier available — requires OPENROUTER_API_KEY
#     e.g. meta-llama/llama-3.3-70b-instruct:free
#          google/gemini-2.0-flash-exp:free
ANALYSIS_MODEL = os.getenv("ANALYSIS_MODEL", "claude-3-haiku-20240307")

# --- OpenRouter (optional, enables free-tier models) ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# --- Finnhub ---
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- Portfolio allocation ---
TARGET_STOCK_PCT = 0.60
TARGET_CRYPTO_PCT = 0.25
TARGET_OPTIONS_PCT = 0.15
MAX_POSITIONS = 5
MIN_POSITIONS = 3          # Bot will actively seek trades if below this threshold

# --- Risk management ---
STOP_LOSS_PCT = 0.05          # Scalper exits fast at -5%
TAKE_PROFIT_PCT = 0.08        # Hard ceiling — sell at +8% no matter what
TRAILING_STOP_PCT = 0.02      # (unused in floor mode, kept for reference)
TRAILING_ACTIVATE_PCT = 0.05  # Once +5% is hit, that becomes the profit floor
MAX_POSITION_PCT = 0.22       # Max 22% of portfolio in a single position
TARGET_DEPLOYED_PCT = 0.80    # Target ~80% of portfolio deployed (not sitting in cash)
EOD_CLOSE_STOCKS = True       # Close all stock positions by 3:50 PM (day trader mode)
EOD_CLOSE_TIME = "15:50"      # Time to flatten stock positions (ET)

# --- Scheduling ---
SCAN_INTERVAL_MINUTES = 15        # Market hours scan frequency (was 30, ignored — now used)
CRYPTO_SCAN_INTERVAL_MINUTES = 30 # Crypto scans 24/7
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 16
MARKET_CLOSE_MINUTE = 0

# --- Quiet hours (ET) ---
# Non-urgent Telegram notifications are suppressed during this window.
# Urgent alerts (stop-loss, errors) always go through.
QUIET_HOURS_START = 23  # 11 PM ET
QUIET_HOURS_END   = 8   # 8 AM ET

# --- Sentiment thresholds ---
MIN_SENTIMENT_SCORE = 0.65          # Min confidence to open a position (0-1)
MIN_SENTIMENT_SCORE_URGENT = 0.55   # Lower bar for HIGH urgency signals (squeeze plays)
MIN_SENTIMENT_MENTIONS = 3          # Min number of source mentions to consider

# --- Signal quality filters (literature-backed) ---
SIGNAL_TTL_EQUITY_SECS = 30 * 60   # Equity signals expire after 30 min (Chen et al. 2014)
SIGNAL_TTL_CRYPTO_SECS = 15 * 60   # Crypto signals expire faster — 15 min
ANTI_PUMP_MAX_MOVE_PCT = 0.03       # Skip BUY if ticker already up >3% intraday (likely in distribution phase)

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
    # WSB/degen favorites — high vol, meme momentum, always discussed on WSB
    "stocks_wsb": [
        "TSLA", "MSTR", "COIN", "HOOD", "RKLB", "JOBY", "LUNR",
        "GME", "AMC", "BBAI", "CLOV", "SPCE",
    ],
    "crypto": [
        "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD", "DOT/USD"
    ],
}

# All stock symbols flat
ALL_STOCK_SYMBOLS = list(set(
    WATCHLIST["stocks_ai"] +
    WATCHLIST["stocks_quantum"] +
    WATCHLIST["stocks_minerals"] +
    WATCHLIST["stocks_wsb"]
))

ALL_CRYPTO_SYMBOLS = WATCHLIST["crypto"]

# Tickers that are also common English words — require $TICKER prefix to avoid false positives
DOLLAR_SIGN_ONLY_TICKERS = {"AI", "LINK", "DOT", "SOL", "LAC", "HOOD", "CLOV", "COIN", "JOBY"}

# Sector keywords for Reddit/news scraping
SECTOR_KEYWORDS = [
    "artificial intelligence", "AI stocks", "quantum computing", "quantum stocks",
    "lithium", "cobalt", "rare earth", "strategic minerals", "EV battery",
    "NVDA", "PLTR", "IONQ", "RGTI", "ALB", "SQM", "LAC",
    "SMCI", "QUBT", "QBTS", "BBAI",
    # WSB flavor
    "TSLA", "MSTR", "GME", "RKLB", "LUNR", "tendies", "yolo", "calls", "puts",
    "squeeze", "gamma squeeze", "short squeeze", "apes", "moon",
]

# Subreddit config: (name, post_limit, sorts_to_fetch)
# WSB gets the royal treatment — more posts, multiple sort feeds
REDDIT_SUB_CONFIG = [
    ("wallstreetbets",   100, ["hot", "new", "rising"]),
    ("wallstreetbetsnew", 50, ["hot", "new"]),
    ("thetagang",         30, ["hot"]),
    ("options",           30, ["hot"]),
    ("stocks",            25, ["hot"]),
    ("investing",         20, ["hot"]),
    ("Superstonk",        30, ["hot"]),
    ("CryptoCurrency",    25, ["hot"]),
    ("StockMarket",       20, ["hot"]),
]

# Keep a flat list for compatibility
REDDIT_SUBS = [sub for sub, _, _ in REDDIT_SUB_CONFIG]

# Momentum gate — skip buying if price has already moved too much from today's open
MAX_INTRADAY_MOVE_PCT = 0.08   # Skip buy if stock already up/down >8% on the day

# Options config
OPTIONS_MAX_DTE = 14       # Max days to expiration for options plays
OPTIONS_MIN_DTE = 2        # Min days to expiration
OPTIONS_TARGET_DELTA = 0.40  # Target delta for calls/puts (~ATM)
