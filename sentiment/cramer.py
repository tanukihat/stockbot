"""
Cramer Scraper + Inverse Cramer Score (ICS)

Scrapes Jim Cramer's recent stock picks from publicly available sources,
sends them to Claude Haiku for sentiment analysis, then inverts the result
into an ICS (0.0–1.0, where 1.0 = Cramer hates it = strong inverse buy signal).

ICS is NOT used as a trading signal — it's attached to CLOSED position
notifications as a retrospective correlation tracker. The goal: see with
our eyeballs if ICS predicts performance.
"""
import json
import re
import logging
import time
import anthropic
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from config import ANTHROPIC_API_KEY, ANALYSIS_MODEL

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# In-memory cache: { symbol: {"ics": float, "cramer_action": str, "fetched_at": float} }
_cramer_cache: dict = {}
_CACHE_TTL_SECONDS = 3600  # refresh once per hour max


# ---------------------------------------------------------------------------
# Scrapers — try multiple sources, fall back gracefully
# ---------------------------------------------------------------------------

def _scrape_stockanalysis(symbol: str) -> list[str]:
    """
    Pull recent Cramer mentions from stockanalysis.com/cramer/
    Returns a list of plain-text blurbs mentioning the symbol.
    """
    snippets = []
    try:
        url = f"https://stockanalysis.com/cramer/{symbol.lower()}/"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; stockbot/1.0)"}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return snippets
        text = r.text
        # Pull action strings — stockanalysis renders them as text nodes
        # Patterns like "Buy", "Sell", "Positive", "Negative", "Don't Buy"
        action_re = re.compile(
            r'(Buy|Sell|Positive|Negative|Don\'t Buy|Bullish|Bearish|Hold|Own It|Lightning Round)',
            re.IGNORECASE
        )
        # Also grab surrounding date context
        date_re = re.compile(r'\d{4}-\d{2}-\d{2}|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* \d{1,2},? \d{4}')
        actions = action_re.findall(text)
        dates = date_re.findall(text)
        if actions:
            date_str = dates[0] if dates else "recent"
            snippets.append(f"[stockanalysis.com] Cramer on {symbol}: {', '.join(actions[:5])} ({date_str})")
    except Exception as e:
        logger.debug(f"stockanalysis scrape failed for {symbol}: {e}")
    return snippets


def _scrape_madmoney_tracker() -> dict[str, list[str]]:
    """
    Pull recent Cramer picks from the Mad Money Stock Screener / trackers.
    Returns { symbol: [list of snippets] }
    """
    results: dict[str, list[str]] = {}
    try:
        # cramer-tracker.com provides a simple recent picks list
        url = "https://www.cramer-tracker.com/picks"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; stockbot/1.0)"}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return results
        text = r.text
        # Tickers are usually in ALL CAPS, 1-5 chars, surrounded by sentiment words
        # Pattern: capture ticker + action within ~100 chars
        pick_re = re.compile(
            r'\b([A-Z]{1,5})\b.{0,80}?(buy|sell|bullish|bearish|positive|negative|own|avoid|don\'t buy)',
            re.IGNORECASE
        )
        for m in pick_re.finditer(text):
            sym = m.group(1)
            action = m.group(2)
            if sym not in results:
                results[sym] = []
            results[sym].append(f"[cramer-tracker.com] Cramer: {action} on {sym}")
    except Exception as e:
        logger.debug(f"cramer-tracker scrape failed: {e}")
    return results


def _scrape_generic_cramer_news(symbol: str) -> list[str]:
    """
    Last-resort: hit a simple news search for "[symbol] Cramer" via RSS/JSON.
    Uses Brave Search-style free endpoint or finnhub news fallback.
    """
    snippets = []
    try:
        url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            cramer_re = re.compile(r'cramer', re.IGNORECASE)
            if cramer_re.search(r.text):
                # Pull surrounding text around "cramer" mentions
                for m in cramer_re.finditer(r.text):
                    start = max(0, m.start() - 100)
                    end = min(len(r.text), m.end() + 200)
                    snippet = re.sub(r'<[^>]+>', '', r.text[start:end]).strip()
                    if symbol.upper() in snippet.upper():
                        snippets.append(f"[Yahoo Finance RSS] {snippet[:300]}")
                        break
    except Exception as e:
        logger.debug(f"Yahoo RSS scrape failed for {symbol}: {e}")
    return snippets


def get_cramer_snippets(symbol: str) -> list[str]:
    """
    Aggregate all scraper sources for a symbol.
    Returns combined list of raw text snippets about Cramer's calls on this ticker.
    """
    snippets = []

    # Source 1: stockanalysis.com (symbol-specific page)
    snippets.extend(_scrape_stockanalysis(symbol))

    # Source 2: cramer-tracker bulk picks (check if symbol appears)
    tracker_data = _scrape_madmoney_tracker()
    if symbol in tracker_data:
        snippets.extend(tracker_data[symbol])

    # Source 3: Yahoo RSS fallback
    if not snippets:
        snippets.extend(_scrape_generic_cramer_news(symbol))

    return snippets


# ---------------------------------------------------------------------------
# Haiku Analysis → ICS
# ---------------------------------------------------------------------------

_ICS_SYSTEM = """You are analyzing Jim Cramer's public statements about a stock ticker.
Your job is to determine his overall sentiment (bullish/bearish/neutral) and produce a raw score.

Output ONLY valid JSON, no preamble:
{
  "cramer_sentiment": "bullish" | "bearish" | "neutral" | "unknown",
  "cramer_action": "BUY" | "SELL" | "HOLD" | "UNKNOWN",
  "raw_cramer_score": 0.0 to 1.0,
  "reasoning": "one sentence"
}

Where raw_cramer_score:
  1.0 = extremely bullish (Cramer screaming BUY)
  0.5 = neutral / no clear call
  0.0 = extremely bearish (Cramer saying sell/avoid)

If there is no data or the data is ambiguous, return "unknown" and 0.5."""


def _analyze_with_haiku(symbol: str, snippets: list[str]) -> dict:
    """Send Cramer snippets to Haiku and get back structured sentiment."""
    if not snippets:
        return {
            "cramer_sentiment": "unknown",
            "cramer_action": "UNKNOWN",
            "raw_cramer_score": 0.5,
            "reasoning": "No Cramer data found for this ticker.",
        }

    user_content = (
        f"Ticker: {symbol}\n\n"
        f"Cramer data snippets:\n"
        + "\n".join(f"- {s}" for s in snippets[:8])
    )

    try:
        response = _client.messages.create(
            model="claude-haiku-4-5",  # cheap — just JSON extraction
            max_tokens=300,
            system=_ICS_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if "```" in raw:
            raw = re.sub(r'```[a-z]*\n?', '', raw).strip().rstrip('`').strip()
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"Haiku ICS analysis failed for {symbol}: {e}")
        return {
            "cramer_sentiment": "unknown",
            "cramer_action": "UNKNOWN",
            "raw_cramer_score": 0.5,
            "reasoning": f"Analysis error: {e}",
        }


def compute_ics(symbol: str) -> dict:
    """
    Main entry point. Returns ICS data for a symbol.

    ICS = 1.0 - raw_cramer_score
    Cramer bullish (1.0) → ICS 0.0 (don't invert, bad omen)
    Cramer bearish (0.0) → ICS 1.0 (strong inverse signal, buy!)
    Cramer unknown (0.5) → ICS 0.5 (neutral, no signal)

    Cached for 1 hour to avoid hammering scrapers.
    """
    now = time.time()
    cached = _cramer_cache.get(symbol)
    if cached and (now - cached["fetched_at"]) < _CACHE_TTL_SECONDS:
        logger.debug(f"ICS cache hit for {symbol}")
        return cached

    logger.info(f"Computing ICS for {symbol}...")
    snippets = get_cramer_snippets(symbol)
    analysis = _analyze_with_haiku(symbol, snippets)

    raw_score = analysis.get("raw_cramer_score", 0.5)
    ics = round(1.0 - raw_score, 2)

    result = {
        "symbol": symbol,
        "ics": ics,
        "cramer_sentiment": analysis.get("cramer_sentiment", "unknown"),
        "cramer_action": analysis.get("cramer_action", "UNKNOWN"),
        "reasoning": analysis.get("reasoning", ""),
        "had_data": bool(snippets),
        "fetched_at": now,
    }
    _cramer_cache[symbol] = result
    logger.info(f"ICS for {symbol}: {ics:.2f} (Cramer: {result['cramer_sentiment']} → inverse)")
    return result


def format_ics_for_telegram(symbol: str, pnl_pct: float) -> str:
    """
    Returns a short ICS line to append to close notifications.
    Includes whether the trade result matched Inverse Cramer theory.

    ICS ≥ 0.65 = Cramer was bearish → Inverse Cramer said BUY
    ICS ≤ 0.35 = Cramer was bullish → Inverse Cramer said AVOID
    ICS 0.36–0.64 = No strong Cramer signal (neutral zone)

    Verification:
    - If ICS ≥ 0.65 and pnl_pct > 0 → ✅ Inverse Cramer correct
    - If ICS ≥ 0.65 and pnl_pct ≤ 0 → ❌ Inverse Cramer wrong
    - If ICS ≤ 0.35 and pnl_pct ≤ 0 → ✅ Inverse Cramer correct (avoided a loser)
    - If ICS ≤ 0.35 and pnl_pct > 0 → ❌ Inverse Cramer wrong (missed a winner)
    - Neutral zone → ➖ No signal
    """
    try:
        data = compute_ics(symbol)
    except Exception as e:
        logger.warning(f"ICS fetch failed for {symbol}: {e}")
        return ""

    ics = data["ics"]
    cramer_call = data["cramer_action"]
    had_data = data["had_data"]

    if not had_data:
        return f"🎰 <b>ICS:</b> N/A (no Cramer data)"

    # Determine verdict
    if ics >= 0.65:
        # Cramer was bearish → IC theory says this should profit
        verdict = "✅" if pnl_pct > 0 else "❌"
        signal_str = f"Cramer: {cramer_call} → IC: BUY signal"
    elif ics <= 0.35:
        # Cramer was bullish → IC theory says avoid
        verdict = "✅" if pnl_pct <= 0 else "❌"
        signal_str = f"Cramer: {cramer_call} → IC: AVOID signal"
    else:
        verdict = "➖"
        signal_str = f"Cramer: {cramer_call} (neutral zone)"

    return f"🎰 <b>ICS:</b> {ics:.2f} | {signal_str} {verdict}"
