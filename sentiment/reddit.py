"""
Reddit sentiment scraper.
Uses the public Reddit JSON API — no auth required.
Scans configured subreddits for watchlist symbol mentions and sentiment.
"""
import requests
import re
import logging
import time
from config import REDDIT_SUBS, ALL_STOCK_SYMBOLS, ALL_CRYPTO_SYMBOLS, SECTOR_KEYWORDS

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "stockbot/1.0 sentiment-scanner"}

CRYPTO_SYMBOLS = [s.split("/")[0] for s in ALL_CRYPTO_SYMBOLS]  # BTC, ETH, etc.
ALL_SYMBOLS = ALL_STOCK_SYMBOLS + CRYPTO_SYMBOLS


def fetch_subreddit_posts(subreddit, limit=25, sort="hot"):
    """Fetch posts from a subreddit using the public JSON API."""
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json"
    params = {"limit": limit}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        posts = data.get("data", {}).get("children", [])
        return [p["data"] for p in posts]
    except Exception as e:
        logger.warning(f"Reddit fetch failed for r/{subreddit}: {e}")
        return []


def extract_symbol_mentions(text):
    """Extract stock/crypto ticker mentions from text."""
    # Match $TICKER or standalone TICKER (2-5 uppercase letters)
    dollar_tickers = re.findall(r'\$([A-Z]{1,5})\b', text.upper())
    word_tickers = re.findall(r'\b([A-Z]{2,5})\b', text.upper())

    # Filter to only known symbols
    found = set()
    for sym in dollar_tickers + word_tickers:
        if sym in ALL_SYMBOLS:
            found.add(sym)

    return list(found)


def basic_sentiment(text):
    """
    Very basic lexical sentiment — bullish/bearish/neutral.
    Returns a float: positive = bullish, negative = bearish.
    Claude will do the heavy lifting; this is just quick pre-filtering.
    """
    text_lower = text.lower()

    bullish_words = [
        "moon", "buy", "bull", "calls", "long", "pump", "surge", "breakout",
        "rocket", "gains", "rally", "bullish", "buying", "upside", "squeeze",
        "rip", "🚀", "💎", "hodl", "yolo", "undervalued", "strong",
    ]
    bearish_words = [
        "sell", "puts", "short", "crash", "dump", "bearish", "drop", "fall",
        "overvalued", "bubble", "collapse", "tanking", "down", "loss", "weak",
        "🩳", "💀", "rekt", "crashing",
    ]

    bull_count = sum(1 for w in bullish_words if w in text_lower)
    bear_count = sum(1 for w in bearish_words if w in text_lower)

    total = bull_count + bear_count
    if total == 0:
        return 0.0
    return (bull_count - bear_count) / total


def scrape_reddit():
    """
    Scrape all configured subreddits and return structured mentions.
    Returns dict: { symbol: { posts: [...], raw_sentiment: float, mention_count: int } }
    """
    symbol_data = {}

    for sub in REDDIT_SUBS:
        posts = fetch_subreddit_posts(sub, limit=25)
        time.sleep(0.5)  # be polite to Reddit

        for post in posts:
            title = post.get("title", "")
            body = post.get("selftext", "")
            score = post.get("score", 0)
            url = post.get("url", "")
            combined = f"{title} {body}"

            # Check for sector keyword match
            has_keyword = any(kw.lower() in combined.lower() for kw in SECTOR_KEYWORDS)
            symbols = extract_symbol_mentions(combined)

            if not symbols and not has_keyword:
                continue

            sentiment = basic_sentiment(combined)

            for sym in symbols:
                if sym not in symbol_data:
                    symbol_data[sym] = {
                        "posts": [],
                        "raw_sentiment_sum": 0.0,
                        "mention_count": 0,
                        "top_score": 0,
                    }
                symbol_data[sym]["posts"].append({
                    "subreddit": sub,
                    "title": title[:200],
                    "body": body[:500],
                    "score": score,
                    "url": url,
                    "sentiment": sentiment,
                })
                symbol_data[sym]["raw_sentiment_sum"] += sentiment
                symbol_data[sym]["mention_count"] += 1
                symbol_data[sym]["top_score"] = max(symbol_data[sym]["top_score"], score)

    # Normalize
    result = {}
    for sym, data in symbol_data.items():
        count = data["mention_count"]
        result[sym] = {
            "posts": data["posts"][:5],  # top 5 most recent
            "raw_sentiment": data["raw_sentiment_sum"] / count if count else 0,
            "mention_count": count,
            "top_post_score": data["top_score"],
        }

    logger.info(f"Reddit: found mentions for {len(result)} symbols")
    return result
