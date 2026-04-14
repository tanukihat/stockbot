"""
Dynamic ticker discovery — finds hot symbols mentioned in sector-relevant Reddit posts
that aren't on the static watchlist. Validated against Alpaca before passing to Claude.

Flow:
  1. Scan Reddit posts that already matched sector keywords
  2. Extract ALL ticker mentions (not just watchlist ones)
  3. Filter: not already on watchlist, not a common word, valid on Alpaca, price >= $1
  4. Return up to MAX_DYNAMIC_SYMBOLS with their post context for Claude

This runs as a lightweight side-pass on already-fetched Reddit data — no extra API calls
to Reddit. Only Alpaca asset validation adds latency (cached after first call).
"""
import re
import logging
from config import ALL_STOCK_SYMBOLS, ALL_CRYPTO_SYMBOLS, DOLLAR_SIGN_ONLY_TICKERS

logger = logging.getLogger(__name__)

# --- Tuning ---
MAX_DYNAMIC_SYMBOLS    = 8     # max new symbols to surface per cycle
MIN_DYNAMIC_MENTIONS   = 3     # must appear at least this many times across sector posts
MIN_DYNAMIC_PRICE      = 1.0   # filter out penny stocks
DYNAMIC_MAX_PRICE      = 5000  # filter out Berkshire-tier outliers

# Words that look like tickers but aren't — expanded common false-positive list
COMMON_WORDS = {
    "A", "I", "IN", "IS", "IT", "BE", "AS", "AT", "BY", "DO", "GO", "IF", "ME",
    "MY", "NO", "OF", "OK", "ON", "OR", "SO", "TO", "UP", "US", "WE", "AM",
    "AN", "ARE", "ALL", "AND", "ANY", "BUT", "CAN", "CEO", "CFO", "COO", "CTO",
    "DD", "DID", "DIV", "DUE", "EOD", "EST", "ETF", "FAQ", "FOR", "FYI",
    "GET", "GOT", "HAS", "HAD", "HER", "HIM", "HIS", "HOW", "IMO", "INC",
    "IPO", "IRA", "IRS", "ITS", "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC", "LET", "LLC", "LTD", "MAX",
    "MIN", "MOD", "NET", "NEW", "NOT", "NOW", "NYSE", "OTC", "OUT", "OWN",
    "PAY", "PDF", "PER", "PIN", "PLS", "POC", "POV", "PRE", "PRO", "PUT",
    "QOQ", "QTD", "QTR", "RIP", "ROE", "ROI", "RUN", "SAY", "SEC", "SEE",
    "SET", "SHE", "THE", "TBH", "TBT", "TOP", "TWO", "USA", "USE", "VIX",
    "WAS", "WHO", "WHY", "WIN", "WOW", "YOY", "YTD", "YOU", "YOY",
    "YOLO", "HODL", "FOMO", "BTFD", "APES", "BULL", "BEAR", "CALL", "PUTS",
    "LONG", "SHORT", "SELL", "BUYS", "DUMP", "MOON", "PUMP", "REKT", "NOPE",
    "FWIW", "AFAIK", "TLDR", "IMO", "IMHO", "EPS", "ATH", "ATL", "YTD",
    "GDP", "CPI", "FED", "SPY", "QQQ", "DIA", "IWM", "VIX", "DJIA",  # common indices/ETFs
}

# Cache of validated Alpaca assets: { symbol: True/False }
_alpaca_asset_cache: dict[str, bool] = {}


def _is_tradeable_on_alpaca(symbol: str) -> bool:
    """Check if a symbol is a tradeable US equity on Alpaca. Cached."""
    if symbol in _alpaca_asset_cache:
        return _alpaca_asset_cache[symbol]
    try:
        import alpaca_client as a
        asset = a._get(f"/assets/{symbol}")
        tradeable = (
            asset.get("tradable", False)
            and asset.get("status") == "active"
            and asset.get("asset_class") == "us_equity"
            and not asset.get("easy_to_borrow") is False  # skip hard-to-borrow
        )
        _alpaca_asset_cache[symbol] = tradeable
        return tradeable
    except Exception:
        _alpaca_asset_cache[symbol] = False
        return False


def _get_price(symbol: str) -> float | None:
    """Get latest price for a symbol. Returns None on failure."""
    try:
        import alpaca_client as a
        return a.get_latest_price(symbol, "us_equity")
    except Exception:
        return None


def extract_all_tickers(text: str) -> set[str]:
    """
    Extract ALL plausible ticker mentions from text — not limited to watchlist.
    Uses $ prefix as strong signal; also matches 2-5 char uppercase words.
    """
    upper = text.upper()
    # $TICKER mentions — high confidence
    dollar_tickers = set(re.findall(r'\$([A-Z]{1,5})\b', upper))
    # Bare uppercase words 2-5 chars — lower confidence, filter aggressively
    word_tickers = set(re.findall(r'\b([A-Z]{2,5})\b', upper))

    candidates = set()
    for t in dollar_tickers:
        if len(t) >= 2 and t not in COMMON_WORDS:
            candidates.add(t)
    for t in word_tickers:
        if len(t) >= 2 and t not in COMMON_WORDS and t not in DOLLAR_SIGN_ONLY_TICKERS:
            candidates.add(t)
    return candidates


def discover_dynamic_symbols(reddit_data: dict, existing_symbols: set) -> dict:
    """
    Given raw reddit_data (from scrape_reddit), find tickers that:
    - Appear >= MIN_DYNAMIC_MENTIONS times across sector-relevant posts
    - Are NOT already in existing_symbols (watchlist)
    - Are tradeable US equities on Alpaca
    - Have price between MIN_DYNAMIC_PRICE and DYNAMIC_MAX_PRICE

    Returns a dict of { symbol: discovery_context } ready to merge into aggregated data.
    Each entry mirrors the structure expected by the aggregator/claude_analyzer.
    """
    if not reddit_data:
        return {}

    # Count mentions of non-watchlist tickers across all reddit posts
    ticker_mentions: dict[str, list] = {}

    for sym, data in reddit_data.items():
        for post in data.get("posts", []):
            combined = f"{post.get('title', '')} {post.get('body', '')}"
            found = extract_all_tickers(combined)
            for ticker in found:
                if ticker in existing_symbols:
                    continue
                if ticker not in ticker_mentions:
                    ticker_mentions[ticker] = []
                ticker_mentions[ticker].append(post)

    # Filter by mention count
    hot_candidates = {
        sym: posts for sym, posts in ticker_mentions.items()
        if len(posts) >= MIN_DYNAMIC_MENTIONS
    }

    if not hot_candidates:
        logger.info("Dynamic discovery: no candidates above mention threshold")
        return {}

    logger.info(f"Dynamic discovery: {len(hot_candidates)} candidates above threshold — validating...")

    # Validate against Alpaca + price filter
    validated = {}
    checked = 0
    for sym, posts in sorted(hot_candidates.items(), key=lambda x: -len(x[1])):
        if len(validated) >= MAX_DYNAMIC_SYMBOLS:
            break
        checked += 1

        if not _is_tradeable_on_alpaca(sym):
            logger.debug(f"Dynamic discovery: {sym} not tradeable on Alpaca — skipped")
            continue

        price = _get_price(sym)
        if price is None or price < MIN_DYNAMIC_PRICE or price > DYNAMIC_MAX_PRICE:
            logger.debug(f"Dynamic discovery: {sym} price={price} out of range — skipped")
            continue

        # Build context for Claude — use the top posts that mentioned this ticker
        top_posts = sorted(posts, key=lambda p: -p.get("score", 0))[:4]
        context_pieces = []
        for post in top_posts:
            age = post.get("age_hours", 0)
            age_str = f"{age:.0f}h ago" if age >= 1 else f"{age*60:.0f}min ago"
            piece = f"[Reddit r/{post['subreddit']} | {age_str} | score:{post.get('score',0)}] {post['title']}"
            if post.get("body"):
                piece += f": {post['body'][:300]}"
            context_pieces.append(piece)

        mention_count = len(posts)
        sentiments = [p.get("sentiment", 0) for p in posts if p.get("sentiment") is not None]
        avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0
        top_score = max((p.get("score", 0) for p in posts), default=0)

        validated[sym] = {
            "symbol": sym,
            "asset_class": "us_equity",
            "mention_count": mention_count,
            "raw_sentiment": avg_sentiment,
            "context": "\n".join(context_pieces),
            "reddit_mention_count": mention_count,
            "stocktwits_message_count": 0,
            "top_reddit_score": top_score,
            "finnhub_article_count": 0,
            "has_earnings_today": False,
            "dynamic_discovery": True,  # flag so we can log/track these separately
        }
        logger.info(f"Dynamic discovery: ✨ {sym} — {mention_count} mentions, sentiment={avg_sentiment:+.2f}, price=${price:.2f}")

    logger.info(f"Dynamic discovery: {len(validated)} new symbols surfaced (checked {checked} candidates)")
    return validated
