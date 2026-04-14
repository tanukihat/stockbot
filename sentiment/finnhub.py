"""
Finnhub sentiment source.
Pulls two intraday-relevant signals:
  1. Company news (last 24h) for all watchlist symbols
  2. Today's earnings calendar — knowing a stock reports today is high-value context

Free tier: 60 req/min — we stay well under with one req per symbol + one earnings call.
"""
import requests
import logging
import time
from datetime import datetime, timezone, timedelta
from config import FINNHUB_API_KEY, ALL_STOCK_SYMBOLS

logger = logging.getLogger(__name__)

BASE_URL = "https://finnhub.io/api/v1"
HEADERS = {"X-Finnhub-Token": FINNHUB_API_KEY}

# How old a news article can be and still be intraday-relevant
NEWS_MAX_AGE_HOURS = 24
# Only send Claude the freshest articles — cap per symbol to keep prompt tight
MAX_ARTICLES_PER_SYMBOL = 3


def _get(endpoint, params=None, retries=2):
    url = f"{BASE_URL}/{endpoint}"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params or {}, timeout=10)
            if r.status_code == 429:
                logger.warning(f"Finnhub rate limited, waiting 5s...")
                time.sleep(5)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning(f"Finnhub {endpoint} failed (attempt {attempt+1}): {e}")
            time.sleep(1)
    return None


def fetch_company_news(symbol: str, hours_back: int = NEWS_MAX_AGE_HOURS) -> list:
    """
    Returns list of recent news articles for a symbol.
    Each article: { headline, summary, source, age_hours, url }
    """
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(hours=hours_back)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")

    data = _get("company-news", params={
        "symbol": symbol,
        "from": date_from,
        "to": date_to,
    })

    if not data or not isinstance(data, list):
        return []

    articles = []
    for item in data:
        published = item.get("datetime", 0)
        age_hours = (now.timestamp() - published) / 3600 if published else 999
        if age_hours > NEWS_MAX_AGE_HOURS:
            continue
        articles.append({
            "headline": item.get("headline", "")[:200],
            "summary": item.get("summary", "")[:300],
            "source": item.get("source", ""),
            "age_hours": round(age_hours, 1),
            "url": item.get("url", ""),
        })

    # Sort freshest first, cap
    articles.sort(key=lambda a: a["age_hours"])
    return articles[:MAX_ARTICLES_PER_SYMBOL]


def fetch_earnings_today() -> set:
    """
    Returns set of symbols reporting earnings TODAY.
    Knowing a stock reports today is critical intraday context.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    data = _get("calendar/earnings", params={"from": today, "to": today})
    if not data:
        return set()

    earnings = data.get("earningsCalendar", [])
    symbols = {e["symbol"] for e in earnings if e.get("symbol")}
    if symbols:
        logger.info(f"Finnhub: earnings today for {len(symbols)} symbols: {', '.join(sorted(symbols))}")
    return symbols


def scrape_finnhub(symbols: list = None) -> dict:
    """
    Main entry point. Scrapes news for all watchlist symbols and today's earnings.
    Returns dict: { symbol: { articles: [...], has_earnings_today: bool, sentiment_boost: float } }

    sentiment_boost: small additive signal based on news tone keywords.
    """
    if not FINNHUB_API_KEY:
        logger.warning("FINNHUB_API_KEY not set — skipping Finnhub scrape")
        return {}

    symbols = symbols or ALL_STOCK_SYMBOLS
    earnings_today = fetch_earnings_today()

    result = {}
    for sym in symbols:
        articles = fetch_company_news(sym)
        time.sleep(0.15)  # ~6 req/sec, well under 60/min limit

        if not articles and sym not in earnings_today:
            continue

        # Simple keyword sentiment on headlines
        boost = 0.0
        for a in articles:
            text = (a["headline"] + " " + a["summary"]).lower()
            bullish = sum(1 for w in [
                "beat", "surge", "jump", "rally", "upgrade", "buy rating",
                "record", "strong", "outperform", "raises guidance", "deal",
                "contract", "partnership", "fda approval", "breakthrough",
            ] if w in text)
            bearish = sum(1 for w in [
                "miss", "drop", "fall", "downgrade", "sell rating", "weak",
                "cut guidance", "layoff", "recall", "investigation", "lawsuit",
                "loss", "below expectations",
            ] if w in text)
            total = bullish + bearish
            if total:
                boost += (bullish - bearish) / total

        if articles or sym in earnings_today:
            result[sym] = {
                "articles": articles,
                "has_earnings_today": sym in earnings_today,
                "sentiment_boost": round(boost / max(len(articles), 1), 2),
                "article_count": len(articles),
            }

    logger.info(f"Finnhub: got news/earnings data for {len(result)} symbols")
    return result
