"""
Aggregates signals from all sentiment sources into a unified structure
for Claude to analyze.
"""
import logging
from sentiment.reddit import scrape_reddit
from sentiment.stocktwits import scrape_stocktwits
from config import ALL_STOCK_SYMBOLS, ALL_CRYPTO_SYMBOLS, MIN_SENTIMENT_MENTIONS

logger = logging.getLogger(__name__)


def aggregate_sentiment(scan_crypto=True, scan_stocks=True):
    """
    Runs all scrapers and merges results.
    Returns a dict of { symbol: aggregated_data } ready for Claude.
    """
    combined = {}

    # Reddit (covers both stocks and crypto)
    logger.info("Scraping Reddit...")
    reddit_data = scrape_reddit()

    # StockTwits (stocks only, focused on watchlist)
    st_data = {}
    if scan_stocks:
        logger.info("Scraping StockTwits...")
        st_data = scrape_stocktwits(ALL_STOCK_SYMBOLS[:20])

    # Merge Reddit + StockTwits by symbol
    all_symbols = set(list(reddit_data.keys()) + list(st_data.keys()))

    for sym in all_symbols:
        r = reddit_data.get(sym, {})
        st = st_data.get(sym, {})

        # Determine asset class
        crypto_syms = [s.split("/")[0] for s in ALL_CRYPTO_SYMBOLS]
        asset_class = "crypto" if sym in crypto_syms else "us_equity"

        # Build context snippets for Claude
        context_pieces = []

        # Reddit posts
        for post in r.get("posts", []):
            piece = f"[Reddit r/{post['subreddit']}] {post['title']}"
            if post.get("body"):
                piece += f": {post['body'][:300]}"
            context_pieces.append(piece)

        # StockTwits messages
        for snippet in st.get("snippets", []):
            context_pieces.append(f"[StockTwits] {snippet}")

        if not context_pieces:
            continue

        # Weighted sentiment average (reddit + stocktwits)
        sentiments = []
        if r.get("raw_sentiment") is not None:
            sentiments.append(r["raw_sentiment"])
        if st.get("sentiment_score") is not None:
            sentiments.append(st["sentiment_score"])

        avg_raw_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0

        mention_count = r.get("mention_count", 0) + st.get("message_count", 0)

        if mention_count < MIN_SENTIMENT_MENTIONS and avg_raw_sentiment == 0:
            continue  # not enough signal

        combined[sym] = {
            "symbol": sym,
            "asset_class": asset_class,
            "mention_count": mention_count,
            "raw_sentiment": avg_raw_sentiment,
            "context": "\n".join(context_pieces[:8]),  # top 8 snippets for Claude
            "reddit_mention_count": r.get("mention_count", 0),
            "stocktwits_message_count": st.get("message_count", 0),
            "top_reddit_score": r.get("top_post_score", 0),
        }

    logger.info(f"Aggregated sentiment for {len(combined)} symbols")
    return combined
