"""
StockTwits sentiment scraper.
Public API — no auth required for basic symbol streams.
"""
import html
import requests
import logging
import time
from config import ALL_STOCK_SYMBOLS, ALL_CRYPTO_SYMBOLS

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "stockbot/1.0"}
BASE_URL = "https://api.stocktwits.com/api/2"

# StockTwits uses different format for crypto: BTCUSD (not BTC/USD)
CRYPTO_MAP = {s: s.replace("/", "") for s in ALL_CRYPTO_SYMBOLS}


def fetch_symbol_stream(symbol, limit=30, retries=3):
    """Fetch the latest messages for a symbol from StockTwits. Retries on 429."""
    url = f"{BASE_URL}/streams/symbol/{symbol}.json"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params={"limit": limit}, timeout=10)
            if r.status_code == 429:
                wait = 2 ** attempt
                logger.warning(f"StockTwits rate limited on {symbol}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json().get("messages", [])
        except Exception as e:
            logger.warning(f"StockTwits fetch failed for {symbol}: {e}")
            return []
    return []


def parse_sentiment(messages):
    """
    Extract sentiment from StockTwits messages.
    StockTwits users can tag messages as Bullish/Bearish.
    """
    bull = 0
    bear = 0
    neutral = 0
    snippets = []

    for msg in messages:
        entities = msg.get("entities", {})
        sentiment = entities.get("sentiment", {})
        if sentiment:
            basic = sentiment.get("basic", "").lower()
            if basic == "bullish":
                bull += 1
            elif basic == "bearish":
                bear += 1
            else:
                neutral += 1
        else:
            neutral += 1

        body = msg.get("body", "")
        if body:
            snippets.append(html.unescape(body[:200]))

    total = bull + bear + neutral
    if total == 0:
        return 0.0, []

    # Weighted: bullish = +1, bearish = -1, neutral = 0
    score = (bull - bear) / total
    return score, snippets[:5]


def scrape_stocktwits(symbols=None):
    """
    Scrape StockTwits for a list of symbols.
    Returns dict: { symbol: { sentiment_score: float, message_count: int, snippets: [...] } }
    """
    if symbols is None:
        symbols = ALL_STOCK_SYMBOLS[:15]  # limit to top watchlist to avoid rate limits

    result = {}
    for sym in symbols:
        messages = fetch_symbol_stream(sym)
        if messages:
            score, snippets = parse_sentiment(messages)
            result[sym] = {
                "sentiment_score": score,
                "message_count": len(messages),
                "snippets": snippets,
            }
        time.sleep(0.3)  # rate limit courtesy

    logger.info(f"StockTwits: got data for {len(result)} symbols")
    return result
