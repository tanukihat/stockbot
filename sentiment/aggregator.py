"""
Aggregates signals from all sentiment sources into a unified structure
for Claude to analyze.
"""
import logging
from sentiment.reddit import scrape_reddit
from sentiment.stocktwits import scrape_stocktwits
from sentiment.finnhub import scrape_finnhub
from sentiment.discovery import discover_dynamic_symbols
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
        st_data = scrape_stocktwits(ALL_STOCK_SYMBOLS)

    # Finnhub: company news + earnings calendar (stocks only)
    fh_data = {}
    if scan_stocks:
        logger.info("Scraping Finnhub news + earnings...")
        fh_data = scrape_finnhub(ALL_STOCK_SYMBOLS)

    # Merge all sources by symbol
    all_symbols = set(list(reddit_data.keys()) + list(st_data.keys()) + list(fh_data.keys()))

    for sym in all_symbols:
        r = reddit_data.get(sym, {})
        st = st_data.get(sym, {})
        fh = fh_data.get(sym, {})

        # Determine asset class
        crypto_syms = [s.split("/")[0] for s in ALL_CRYPTO_SYMBOLS]
        asset_class = "crypto" if sym in crypto_syms else "us_equity"

        # Build context snippets for Claude
        # Cap each source independently so Finnhub can't crowd out Reddit/StockTwits
        finnhub_pieces = []
        reddit_pieces = []
        st_pieces = []

        # Finnhub news
        if fh.get("has_earnings_today"):
            finnhub_pieces.append(f"[Finnhub ⚡EARNINGS TODAY] {sym} reports earnings today")
        for article in fh.get("articles", [])[:4]:  # cap at 4
            age = article["age_hours"]
            age_str = f"{age:.0f}h ago" if age >= 1 else f"{age*60:.0f}min ago"
            piece = f"[Finnhub News | {age_str} | {article['source']}] {article['headline']}"
            if article.get("summary"):
                piece += f": {article['summary']}"
            finnhub_pieces.append(piece)

        # Reddit posts
        for post in r.get("posts", [])[:4]:  # cap at 4
            age = post.get("age_hours", 0)
            age_str = f"{age:.0f}h ago" if age >= 1 else f"{age*60:.0f}min ago"
            piece = f"[Reddit r/{post['subreddit']} | {age_str} | score:{post.get('score',0)}] {post['title']}"
            if post.get("body"):
                piece += f": {post['body'][:300]}"
            reddit_pieces.append(piece)

        # StockTwits messages
        for snippet in st.get("snippets", [])[:3]:  # cap at 3
            st_pieces.append(f"[StockTwits] {snippet}")

        # Interleave sources so no single source dominates the 10-item window
        context_pieces = []
        for pieces in [finnhub_pieces, reddit_pieces, st_pieces]:
            context_pieces.extend(pieces)

        if not context_pieces:
            continue

        # Weighted sentiment average (reddit + stocktwits + finnhub boost)
        sentiments = []
        if r.get("raw_sentiment") is not None:
            sentiments.append(r["raw_sentiment"])
        if st.get("sentiment_score") is not None:
            sentiments.append(st["sentiment_score"])
        if fh.get("sentiment_boost") is not None and fh.get("article_count", 0) > 0:
            sentiments.append(fh["sentiment_boost"])

        avg_raw_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0

        mention_count = (
            r.get("mention_count", 0)
            + st.get("message_count", 0)
            + fh.get("article_count", 0)
        )

        # Finnhub-only symbols (no social chatter) still pass if they have news
        if mention_count < MIN_SENTIMENT_MENTIONS and avg_raw_sentiment == 0 and not fh.get("has_earnings_today"):
            continue

        combined[sym] = {
            "symbol": sym,
            "asset_class": asset_class,
            "mention_count": mention_count,
            "raw_sentiment": avg_raw_sentiment,
            "context": "\n".join(context_pieces),  # interleaved: up to 4 finnhub + 4 reddit + 3 st
            "reddit_mention_count": r.get("mention_count", 0),
            "stocktwits_message_count": st.get("message_count", 0),
            "top_reddit_score": r.get("top_post_score", 0),
            "finnhub_article_count": fh.get("article_count", 0),
            "has_earnings_today": fh.get("has_earnings_today", False),
        }

    # --- Dynamic discovery: surface hot tickers outside the static watchlist ---
    if scan_stocks and reddit_data:
        existing = set(combined.keys())
        dynamic = discover_dynamic_symbols(reddit_data, existing_symbols=existing)
        for sym, data in dynamic.items():
            if sym not in combined:  # don't overwrite watchlist entries
                combined[sym] = data
        if dynamic:
            logger.info(f"Dynamic discovery added {len(dynamic)} new symbol(s): {list(dynamic.keys())}")

    logger.info(f"Aggregated sentiment for {len(combined)} symbols")
    return combined
