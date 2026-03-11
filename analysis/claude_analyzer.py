"""
Sentiment analysis and trade signal generation.

By default uses Claude Haiku (cheap, good enough for JSON parsing).
Set ANALYSIS_MODEL=openrouter/<model> to use free-tier OpenRouter models.
Set ANALYSIS_MODEL=claude-sonnet-4-6 to go back to the big gun.
"""
import json
import logging
import anthropic
import requests as _requests
from config import (
    ANTHROPIC_API_KEY, OPENROUTER_API_KEY,
    CLAUDE_MODEL, ANALYSIS_MODEL, MIN_SENTIMENT_SCORE
)

logger = logging.getLogger(__name__)

_anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Pre-filter: don't waste API tokens on clearly low-signal symbols
# Must have either enough mentions OR a non-trivial sentiment score
PRE_FILTER_MIN_MENTIONS = 4
PRE_FILTER_MIN_SENTIMENT = 0.15  # |raw_sentiment| threshold when mentions are low

SYSTEM_PROMPT = """You are an autonomous AI trading analyst for a high-frequency paper trading bot.
Your job is to analyze social sentiment data — primarily from Reddit's r/wallstreetbets and similar
degen communities — and produce clear, structured trade signals.

Strategy profile:
- SCALPING focused: target 5-10% gains, quick in and out
- High risk tolerance — this is WSB-style momentum trading
- Primary signal source: retail degen sentiment (WSB, options traders, crypto degens)
- Sectors of interest: AI/ML, quantum computing, strategic minerals, high-vol meme momentum
- Asset classes: US equities, crypto
- Max 5 simultaneous positions
- Stop-loss: 8% | Take-profit: 7%

WSB sentiment interpretation guide:
- "YOLO", "all in", "calls", "moon", "🚀", "tendies" → strong bullish signal
- "puts", "short", "rekt", "bag holding", "drill", "🌈🐻" → strong bearish signal
- High upvote scores on WSB posts = high retail conviction (weight heavily)
- Comment sections with strong consensus amplify the signal
- Distinguish between genuine DD posts and pure meme hype — DD gets higher confidence
- "gamma squeeze" / "short squeeze" mentions = potentially explosive upside, flag HIGH urgency

You will receive aggregated social sentiment data for one or more symbols.
For each symbol, output a JSON trade signal. Be decisive.
Strong bullish WSB consensus = BUY. Weak/mixed = SKIP. Don't hedge.

Output format (JSON array):
[
  {
    "symbol": "NVDA",
    "asset_class": "us_equity",
    "action": "BUY" | "SELL" | "HOLD" | "SKIP",
    "confidence": 0.0-1.0,
    "reasoning": "Brief explanation (2-3 sentences max)",
    "urgency": "HIGH" | "MEDIUM" | "LOW",
    "sector": "AI" | "QUANTUM" | "MINERALS" | "CRYPTO" | "WSB_MEME" | "OTHER",
    "wsb_signal": true | false
  }
]

Only output valid JSON. No preamble, no explanation outside the JSON."""


def _call_openrouter(model, system, user_content, max_tokens=2000):
    """Call OpenRouter API using the OpenAI-compatible endpoint."""
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY not set")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/stockbot",
        "X-Title": "StockBot",
    }
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    }
    r = _requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers, json=body, timeout=60
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]


def _call_anthropic(model, system, user_content, max_tokens=2000):
    """Call Anthropic API directly."""
    response = _anthropic_client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return response.content[0].text


def _call_model(system, user_content, max_tokens=2000, model=None):
    """
    Route to the right provider based on model string.
    openrouter/<model> → OpenRouter API
    everything else → Anthropic API
    """
    m = model or ANALYSIS_MODEL
    if m.startswith("openrouter/"):
        actual_model = m[len("openrouter/"):]
        logger.info(f"Using OpenRouter model: {actual_model}")
        return _call_openrouter(actual_model, system, user_content, max_tokens)
    else:
        logger.info(f"Using Anthropic model: {m}")
        return _call_anthropic(m, system, user_content, max_tokens)


def _parse_signals(raw):
    """Strip markdown fences and parse JSON signal array."""
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def analyze_sentiment_batch(aggregated_data: dict) -> list:
    """
    Send aggregated sentiment data to the analysis model for signal generation.
    Pre-filters noise before the API call to reduce cost.
    Returns list of trade signals above the confidence threshold.
    """
    if not aggregated_data:
        return []

    # --- Pre-filter: drop low-signal symbols before hitting the API ---
    filtered = {
        sym: data for sym, data in aggregated_data.items()
        if (data["mention_count"] >= PRE_FILTER_MIN_MENTIONS
            or abs(data["raw_sentiment"]) >= PRE_FILTER_MIN_SENTIMENT)
    }
    dropped = len(aggregated_data) - len(filtered)
    if dropped:
        logger.info(f"Pre-filter: dropped {dropped} low-signal symbols, sending {len(filtered)} to model")
    if not filtered:
        logger.info("Pre-filter: nothing passed, skipping model call")
        return []

    # --- Build prompt ---
    symbols_text = []
    for sym, data in filtered.items():
        top_score = data.get("top_reddit_score", 0)
        score_str = f" | Top Reddit post score: {top_score:,}" if top_score > 0 else ""
        symbols_text.append(
            f"=== {sym} ({data['asset_class']}) ===\n"
            f"Mentions: {data['mention_count']} (Reddit: {data['reddit_mention_count']}, "
            f"StockTwits: {data['stocktwits_message_count']}){score_str}\n"
            f"Raw sentiment score: {data['raw_sentiment']:.2f} (-1=very bearish, +1=very bullish)\n"
            f"Social context:\n{data['context']}\n"
        )

    user_prompt = (
        "Analyze the following social sentiment data and provide trade signals.\n"
        "Current positions are tracked separately — focus only on signal quality.\n\n"
        + "\n".join(symbols_text)
    )

    try:
        logger.info(f"Sending {len(filtered)} symbols to {ANALYSIS_MODEL} for analysis...")
        raw = _call_model(SYSTEM_PROMPT, user_prompt)
        signals = _parse_signals(raw)
        logger.info(f"Model returned {len(signals)} signals")

        strong = [s for s in signals if s.get("confidence", 0) >= MIN_SENTIMENT_SCORE]
        logger.info(f"{len(strong)} signals meet confidence threshold ({MIN_SENTIMENT_SCORE})")
        return strong

    except json.JSONDecodeError as e:
        logger.error(f"Model returned invalid JSON: {e}\nRaw: {raw[:500]}")
        return []
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        return []


def analyze_existing_position(symbol, asset_class, entry_price, current_price,
                               unrealized_pct, holding_hours):
    """
    Ask whether to hold or close an existing position.
    Uses the cheaper analysis model — this is called per-position per-cycle.
    """
    prompt = (
        f"I hold {symbol} ({asset_class}). Entry: ${entry_price:.2f}, "
        f"Current: ${current_price:.2f}, P&L: {unrealized_pct*100:.1f}%, "
        f"Holding for {holding_hours:.1f} hours.\n"
        f"Given our scalping strategy (target 5-10% gain, 8% stop-loss), "
        f"should I HOLD or CLOSE this position? "
        f'Output JSON: {{"action": "HOLD" or "CLOSE", "reasoning": "..."}}'
    )
    try:
        raw = _call_model("You are a concise trading assistant. Output only valid JSON.", prompt,
                          max_tokens=200)
        return _parse_signals(raw) if isinstance(_parse_signals(raw), dict) else json.loads(raw.strip())
    except Exception as e:
        logger.error(f"Position analysis failed for {symbol}: {e}")
        return {"action": "HOLD", "reasoning": "Analysis unavailable"}
