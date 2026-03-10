"""
Claude-powered sentiment analysis and trade signal generation.
This is the brain of the bot — Claude reads the aggregated sentiment
and outputs structured trade signals.
"""
import json
import logging
import anthropic
from config import ANTHROPIC_API_KEY, MIN_SENTIMENT_SCORE, CLAUDE_MODEL

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are an autonomous AI trading analyst for a high-frequency paper trading bot.
Your job is to analyze social sentiment data and produce clear, structured trade signals.

Strategy profile:
- SCALPING focused: target 5-10% gains, quick in and out
- Medium-to-high risk tolerance
- Sectors of interest: AI/ML companies, quantum computing, strategic minerals (lithium, cobalt)
- Asset classes: US equities, crypto
- Max 5 simultaneous positions
- Stop-loss: 8% | Take-profit: 7%

You will receive aggregated social sentiment data for one or more symbols.
For each symbol, output a JSON trade signal. Be decisive — if sentiment is strong, say BUY.
If sentiment is weak or mixed, say HOLD or SKIP. Never be wishy-washy.

Output format (JSON array):
[
  {
    "symbol": "NVDA",
    "asset_class": "us_equity",
    "action": "BUY" | "SELL" | "HOLD" | "SKIP",
    "confidence": 0.0-1.0,
    "reasoning": "Brief explanation (2-3 sentences max)",
    "urgency": "HIGH" | "MEDIUM" | "LOW",
    "sector": "AI" | "QUANTUM" | "MINERALS" | "CRYPTO" | "OTHER"
  }
]

Only output valid JSON. No preamble, no explanation outside the JSON."""


def analyze_sentiment_batch(aggregated_data: dict) -> list:
    """
    Send aggregated sentiment data to Claude for analysis.
    Returns list of trade signals.
    """
    if not aggregated_data:
        logger.info("No sentiment data to analyze")
        return []

    # Build the prompt
    symbols_text = []
    for sym, data in aggregated_data.items():
        symbols_text.append(
            f"=== {sym} ({data['asset_class']}) ===\n"
            f"Mentions: {data['mention_count']} (Reddit: {data['reddit_mention_count']}, "
            f"StockTwits: {data['stocktwits_message_count']})\n"
            f"Raw sentiment score: {data['raw_sentiment']:.2f} (-1=very bearish, +1=very bullish)\n"
            f"Social context:\n{data['context']}\n"
        )

    user_prompt = (
        f"Analyze the following social sentiment data and provide trade signals.\n"
        f"Current positions are tracked separately — focus only on signal quality.\n\n"
        + "\n".join(symbols_text)
    )

    try:
        logger.info(f"Sending {len(aggregated_data)} symbols to Claude for analysis...")
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw = response.content[0].text.strip()

        # Strip markdown code blocks if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        signals = json.loads(raw)
        logger.info(f"Claude returned {len(signals)} signals")

        # Filter by confidence threshold
        strong_signals = [s for s in signals if s.get("confidence", 0) >= MIN_SENTIMENT_SCORE]
        logger.info(f"{len(strong_signals)} signals meet confidence threshold ({MIN_SENTIMENT_SCORE})")
        return strong_signals

    except json.JSONDecodeError as e:
        logger.error(f"Claude returned invalid JSON: {e}\nRaw: {raw[:500]}")
        return []
    except Exception as e:
        logger.error(f"Claude analysis failed: {e}")
        return []


def analyze_existing_position(symbol, asset_class, entry_price, current_price,
                               unrealized_pct, holding_hours):
    """
    Ask Claude whether to hold or close an existing position.
    Used for positions that haven't hit stop-loss or take-profit yet.
    """
    prompt = (
        f"I hold {symbol} ({asset_class}). Entry: ${entry_price:.2f}, "
        f"Current: ${current_price:.2f}, P&L: {unrealized_pct*100:.1f}%, "
        f"Holding for {holding_hours:.1f} hours.\n"
        f"Given our scalping strategy (target 5-10% gain, 8% stop-loss), "
        f"should I HOLD or CLOSE this position? "
        f"Output JSON: {{\"action\": \"HOLD\" or \"CLOSE\", \"reasoning\": \"...\"}}"
    )

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        logger.error(f"Position analysis failed for {symbol}: {e}")
        return {"action": "HOLD", "reasoning": "Analysis unavailable"}
