"""
Reddit sentiment scraper.
Uses the public Reddit JSON API — no auth required.
WSB gets preferential treatment: higher post limits + multiple sort feeds.
"""
import requests
import re
import logging
import time
from config import (
    REDDIT_SUB_CONFIG, ALL_STOCK_SYMBOLS, ALL_CRYPTO_SYMBOLS,
    SECTOR_KEYWORDS, DOLLAR_SIGN_ONLY_TICKERS
)

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "stockbot/1.0 sentiment-scanner"}

CRYPTO_SYMBOLS = [s.split("/")[0] for s in ALL_CRYPTO_SYMBOLS]  # BTC, ETH, etc.
ALL_SYMBOLS = set(ALL_STOCK_SYMBOLS + CRYPTO_SYMBOLS)


def fetch_subreddit_posts(subreddit, limit=25, sort="hot", retries=3):
    """Fetch posts from a subreddit using the public JSON API. Retries on 429/5xx."""
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json"
    params = {"limit": limit}
    backoff = 2
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=10)
            if r.status_code == 429:
                wait = backoff ** attempt
                logger.warning(f"Reddit rate limited on r/{subreddit}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            posts = data.get("data", {}).get("children", [])
            return [p["data"] for p in posts]
        except requests.HTTPError as e:
            if e.response.status_code >= 500:
                wait = backoff ** attempt
                logger.warning(f"Reddit 5xx on r/{subreddit} (attempt {attempt+1}), retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.warning(f"Reddit fetch failed for r/{subreddit}: {e}")
                return []
        except Exception as e:
            logger.warning(f"Reddit fetch failed for r/{subreddit}: {e}")
            return []
    logger.warning(f"Reddit: gave up on r/{subreddit} after {retries} attempts")
    return []


def fetch_post_comments(post_id, subreddit, limit=50, retries=2):
    """
    Fetch top-level comments for a post.
    Used on high-score WSB posts to extract deeper signal from the comment section.
    """
    url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json"
    params = {"limit": limit, "depth": 1, "sort": "top"}
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=10)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            data = r.json()
            # data[1] is the comments listing
            if len(data) < 2:
                return []
            comments = data[1].get("data", {}).get("children", [])
            return [c["data"] for c in comments if c.get("kind") == "t1"]
        except Exception as e:
            logger.warning(f"Comment fetch failed for {post_id}: {e}")
            return []
    return []


def extract_symbol_mentions(text):
    """
    Extract stock/crypto ticker mentions from text.
    Tickers in DOLLAR_SIGN_ONLY_TICKERS require an explicit $ prefix
    to avoid false positives from common English words.
    """
    upper = text.upper()
    dollar_tickers = set(re.findall(r'\$([A-Z]{1,5})\b', upper))
    word_tickers = set(re.findall(r'\b([A-Z]{2,5})\b', upper))

    found = set()
    for sym in dollar_tickers:
        if sym in ALL_SYMBOLS:
            found.add(sym)
    for sym in word_tickers:
        if sym in ALL_SYMBOLS and sym not in DOLLAR_SIGN_ONLY_TICKERS:
            found.add(sym)

    return list(found)


def basic_sentiment(text):
    """
    WSB-aware lexical sentiment — bullish/bearish/neutral.
    Returns a float: positive = bullish, negative = bearish.
    Claude does the real work; this is pre-filtering signal.
    """
    text_lower = text.lower()

    bullish_words = [
        # Standard
        "buy", "bull", "calls", "long", "surge", "breakout", "rocket",
        "gains", "rally", "bullish", "buying", "upside", "squeeze", "rip",
        "undervalued", "strong", "growth", "beat", "earnings beat",
        # WSB degen
        "moon", "tendies", "yolo", "hodl", "diamond hands", "to the moon",
        "🚀", "💎", "🙌", "apes together", "gamma squeeze", "short squeeze",
        "all in", "buying the dip", "dip", "load up", "printing", "send it",
        "calls printing", "making money", "big gains", "pump",
    ]
    bearish_words = [
        # Standard
        "sell", "puts", "short", "crash", "dump", "bearish", "drop", "fall",
        "overvalued", "bubble", "collapse", "tanking", "down", "loss", "weak",
        # WSB degen
        "rekt", "bagholding", "bag holder", "paper hands", "rug pull",
        "going to zero", "worthless", "bleeding", "red", "down bad",
        "🩳", "💀", "🌈🐻", "inverse", "puts printing", "drill", "capitulate",
        "margin call", "liquidated",
    ]

    bull_count = sum(1 for w in bullish_words if w in text_lower)
    bear_count = sum(1 for w in bearish_words if w in text_lower)

    total = bull_count + bear_count
    if total == 0:
        return 0.0
    return (bull_count - bear_count) / total


# Score threshold to bother fetching comments (saves API calls)
COMMENT_FETCH_MIN_SCORE = 500


def scrape_reddit():
    """
    Scrape all configured subreddits and return structured mentions.
    WSB gets higher limits and multiple sort feeds per REDDIT_SUB_CONFIG.
    Returns dict: { symbol: { posts: [...], raw_sentiment: float, mention_count: int } }
    """
    symbol_data = {}
    seen_post_ids = set()  # dedupe across sort feeds

    for sub, limit, sorts in REDDIT_SUB_CONFIG:
        for sort in sorts:
            posts = fetch_subreddit_posts(sub, limit=limit, sort=sort)
            time.sleep(0.6)  # be polite

            for post in posts:
                post_id = post.get("id", "")
                if post_id in seen_post_ids:
                    continue
                seen_post_ids.add(post_id)

                title = post.get("title", "")
                body = post.get("selftext", "")
                score = post.get("score", 0)
                url = post.get("url", "")
                combined = f"{title} {body}"

                has_keyword = any(kw.lower() in combined.lower() for kw in SECTOR_KEYWORDS)
                symbols = extract_symbol_mentions(combined)

                if not symbols and not has_keyword:
                    continue

                sentiment = basic_sentiment(combined)

                # For high-score WSB posts, also scrape comments
                comment_text = ""
                if sub in ("wallstreetbets", "wallstreetbetsnew") and score >= COMMENT_FETCH_MIN_SCORE:
                    logger.debug(f"Fetching comments for high-score WSB post: '{title[:60]}' (score={score})")
                    comments = fetch_post_comments(post_id, sub)
                    time.sleep(0.4)
                    if comments:
                        comment_text = " ".join(c.get("body", "")[:300] for c in comments[:20])
                        # Any additional symbols mentioned in comments
                        comment_syms = extract_symbol_mentions(comment_text)
                        symbols = list(set(symbols + comment_syms))
                        # Blend comment sentiment
                        comment_sentiment = basic_sentiment(comment_text)
                        sentiment = (sentiment + comment_sentiment) / 2

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
            "posts": sorted(data["posts"], key=lambda p: -p["score"])[:5],  # top 5 by upvotes
            "raw_sentiment": data["raw_sentiment_sum"] / count if count else 0,
            "mention_count": count,
            "top_post_score": data["top_score"],
        }

    logger.info(f"Reddit: found mentions for {len(result)} symbols across {len(seen_post_ids)} unique posts")
    return result
