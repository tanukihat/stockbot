"""
Sentiment analysis and trade signal generation.

By default uses Claude Haiku (cheap, good enough for JSON parsing).
Set ANALYSIS_MODEL=openrouter/<model> to use free-tier OpenRouter models.
Set ANALYSIS_MODEL=claude-sonnet-4-6 to go back to the big gun.
"""
import json
import time
import logging
import anthropic
import requests as _requests
from config import (
    ANTHROPIC_API_KEY, OPENROUTER_API_KEY,
    ANALYSIS_MODEL, MIN_SENTIMENT_SCORE
)

logger = logging.getLogger(__name__)

_anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Pre-filter: don't waste API tokens on clearly low-signal symbols
# Must have either enough mentions OR a non-trivial sentiment score
PRE_FILTER_MIN_MENTIONS = 10      # higher bar = fewer symbols = lower cost
PRE_FILTER_MIN_SENTIMENT = 0.30  # stronger sentiment signal required
MAX_SYMBOLS_PER_CALL = 10        # tighter batch cap — cuts token cost per call

SYSTEM_PROMPT = """You are an autonomous AI trading analyst for an intraday paper trading bot.
Your job is to analyze social sentiment data and produce trade signals for moves happening TODAY —
within the next 2-6 hours. You are NOT a long-term investor. You do not care about fundamentals,
analyst price targets, or multi-week theses.

## ⚠️ CRITICAL: INTRADAY ONLY
This bot opens and closes ALL positions within the same trading day (EOD flatten at 3:50 PM ET).
You must ONLY generate BUY signals for catalysts that are likely to move the stock TODAY.

**SKIP immediately if the sentiment is:**
- A long-term thesis ("this will 10x in 2025", "great company to hold", "buy and hold")
- Fundamentals-based without a near-term catalyst ("strong earnings", "good balance sheet")
- Post-earnings analysis without an imminent catalyst
- Old news rehashed (check post age — posts >6h old are suspect, >12h are stale)
- Vague hype with no specific near-term trigger

**ONLY generate BUY signals if the sentiment shows ONE OR MORE of:**
- **Hard catalyst today**: earnings release, FDA decision, breaking news, major partnership — these are highest confidence
- **Active WSB/meme momentum NOW**: high-upvote post (<4h old) with active comment section, coordinated pile-in, squeeze language in present tense — this IS a valid same-day catalyst, not noise
- **Short/gamma squeeze starting**: unusual options activity mentioned, float short % discussed, "loading calls" language right now
- **Sector momentum wave**: AI/crypto/quantum sector moving broadly today, multiple symbols in the same sector lighting up simultaneously
- "I just bought", "loading up now", "entering here" language — present tense action signals crowd is moving NOW

## Strategy Profile
- INTRADAY SCALPING: target 5-8% gains, EOD flat on all equities
- Hold time: minutes to hours, never overnight for stocks
- High risk tolerance — WSB-style momentum trading
- Sectors: AI/ML, quantum computing, strategic minerals, high-vol meme momentum
- Max 5 positions | Stop-loss: -5% | Take-profit: +8%
- Some symbols are **dynamically discovered** (not on a fixed watchlist) — treat them the same as any other symbol; the same signal quality rules apply

## Signal Quality Rules

**Post age matters enormously:**
- < 2h old: Fresh — high weight
- 2-6h old: Usable — moderate weight
- 6-12h old: Stale — low weight, only if still very active
- > 12h old: Discard for intraday purposes — SKIP unless extraordinary reason

**What makes a signal actionable TODAY:**
- Price already moving (momentum confirmation)
- High-upvote WSB post from last 2h with active comment section
- Options activity (calls being bought for today/this week)
- Breaking catalyst: news just dropped, squeeze just started
- Cross-platform: Reddit + StockTwits both hot RIGHT NOW

**Signal hierarchy (what to weight most):**
1. Hard news catalyst today (Finnhub articles, earnings flag) — highest weight, can stand alone
2. Active WSB momentum with fresh high-engagement posts — valid standalone signal for meme/momentum names
3. Cross-confirmation: news + social both firing = maximum confidence
4. Social only with no catalyst and old posts = skip

**Urgency calibration:**
- HIGH: Active squeeze, breaking news, earnings today, post < 2h with >1k upvotes, sector-wide wave
- MEDIUM: Fresh WSB momentum (< 6h), moderate engagement, AI/crypto/quantum sector play
- LOW: Older posts, single source — skip unless hard catalyst accompanies it

**Confidence calibration:**
- 0.85+: Strong fresh multi-source consensus, active price movement, clear same-day catalyst
- 0.70-0.84: Good intraday signal, recent posts, moderate confirmation
- 0.65-0.69: Borderline — only output if the catalyst is genuinely same-day
- <0.65: SKIP

## 🌍 Macro & Geopolitical Reasoning (CRITICAL)
When news involves geopolitics, sanctions, wars, tariffs, or central bank events, you MUST reason through second-order effects before generating a signal. Social media crowds frequently misread these.

**Ask yourself: is this genuine adoption/demand, or a crisis signal in disguise?**

**Red flags — sentiment may be bullish but underlying event is RISK-OFF:**
- Country adopting crypto/assets due to **sanctions or war** → they're being cut off from the financial system, not endorsing the asset. This is geopolitical instability, not adoption.
- Military conflict near **strategic chokepoints** (Strait of Hormuz, Suez Canal, Taiwan Strait) → oil supply shock risk, broad market risk-off. Do NOT generate equity BUYs.
- Tariff announcements, trade war escalation → risk-off for equities broadly, especially tech/China-exposed names.
- Central bank surprise decisions (Fed, ECB) → volatility spike, avoid new entries.
- Sanctions on major economies → currency/commodity disruption, not a retail trading opportunity.

**Examples of misread signals (DO NOT follow the crowd here):**
- "Iran accepting Bitcoin" during a war → Iran is under sanctions. This is a sanctions workaround, NOT crypto adoption. The actual news (Hormuz, oil, military escalation) is bearish for risk assets.
- "Country bans crypto" → bearish for crypto regardless of how Reddit spins it.
- "Fed holds rates" on a surprise → market may initially cheer, but uncertainty spike means avoid new longs.
- "Tariffs paused" → genuine relief rally catalyst only if confirmed and market hasn't already priced it.

**Genuine bullish crypto/asset signals (these ARE actionable):**
- ETF approval or major institutional adoption in a stable geopolitical context
- Major payment processor or sovereign wealth fund buy announcement
- Regulatory clarity from a large economy (US, EU, Japan)
- Tech breakthrough with clear commercial application

**Rule: If the bullish framing requires ignoring a war, sanctions, or chokepoint closure — SKIP or HOLD. The crowd is wrong.**

## WSB Lexicon
- "YOLO", "all in", "calls", "moon", "🚀", "tendies", "gamma squeeze" → bullish (check if fresh)
- "puts", "short", "rekt", "bag holding", "drill", "🌈🐻" → bearish
- "loading up", "just bought", "entering now" → high recency signal
- "DD" posts with price targets → only useful if catalyst is TODAY
- High upvote + high comment velocity = crowd is active NOW

## Output Format (JSON array only — no preamble, no explanation outside JSON)
[
  {
    "symbol": "NVDA",
    "asset_class": "us_equity",
    "action": "BUY" | "SELL" | "HOLD" | "SKIP",
    "confidence": 0.0-1.0,
    "reasoning": "Brief explanation — must mention WHY this is actionable TODAY (2-3 sentences max)",
    "urgency": "HIGH" | "MEDIUM" | "LOW",
    "sector": "AI" | "QUANTUM" | "MINERALS" | "CRYPTO" | "WSB_MEME" | "OTHER",
    "wsb_signal": true | false
  }
]

Be ruthless about skipping long-thesis noise. If you can't articulate a reason this moves TODAY, SKIP it.
Only output valid JSON."""


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
    """
    Robustly extract trade signals from model output.
    Handles: markdown fences, preamble text, trailing commentary, truncated responses.
    Extracts each complete JSON object individually so a truncated response doesn't
    nuke the signals that did come through cleanly.
    """
    import re
    text = raw.strip()

    # Strip markdown fences
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("[") or part.startswith("{"):
                text = part
                break

    # First try: parse the whole thing (fast path for clean responses)
    bracket = text.find("[")
    if bracket >= 0:
        candidate = text[bracket:]
        last = candidate.rfind("]")
        if last != -1:
            try:
                return json.loads(candidate[:last + 1].strip())
            except json.JSONDecodeError:
                pass  # fall through to object-by-object extraction

    # Fallback: grab every complete JSON object — survives truncation
    signals = []
    for m in re.finditer(r'\{', text):
        start = m.start()
        depth, i = 0, start
        while i < len(text):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if 'symbol' in obj and 'action' in obj:
                            signals.append(obj)
                    except json.JSONDecodeError:
                        pass
                    break
            i += 1
    if signals:
        return signals

    raise json.JSONDecodeError("No valid signal objects found", text, 0)


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

    # --- Build prompt items ---
    def _build_prompt_for(batch: dict) -> str:
        from datetime import datetime
        import alpaca_client as _alpaca
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M ET")
        items = []
        for sym, data in batch.items():
            top_score = data.get("top_reddit_score", 0)
            score_str = f" | Top Reddit post score: {top_score:,}" if top_score > 0 else ""
            earnings_flag = " ⚡EARNINGS TODAY" if data.get("has_earnings_today") else ""
            discovery_flag = " 🔍 DYNAMICALLY DISCOVERED" if data.get("dynamic_discovery") else ""
            news_str = f" | Finnhub articles: {data.get('finnhub_article_count', 0)}" if data.get('finnhub_article_count') else ""
            # Intraday price context — helps Claude filter 'positive sentiment on a tanking stock'
            price_str = ""
            try:
                asset_class = data.get("asset_class", "us_equity")
                alpaca_sym = sym if asset_class != "crypto" else (f"{sym}/USD" if "/" not in sym else sym)
                open_p = _alpaca.get_intraday_open_price(sym, asset_class)
                current_p = _alpaca.get_latest_price(alpaca_sym, asset_class)
                if open_p and current_p and open_p > 0:
                    move_pct = (current_p - open_p) / open_p * 100
                    direction = "⬆️" if move_pct >= 0 else "⬇️"
                    price_str = f" | Intraday price move: {direction}{move_pct:+.2f}% (open ${open_p:.2f} → now ${current_p:.2f})"
            except Exception:
                pass
            items.append(
                f"=== {sym} ({data['asset_class']}){earnings_flag}{discovery_flag} ===\n"
                f"Mentions: {data['mention_count']} (Reddit: {data['reddit_mention_count']}, "
                f"StockTwits: {data['stocktwits_message_count']}{news_str}){score_str}{price_str}\n"
                f"Raw sentiment score: {data['raw_sentiment']:.2f} (-1=very bearish, +1=very bullish)\n"
                f"Social context (post timestamps included — age is critical for intraday):\n{data['context']}\n"
            )
        return (
            f"Current time: {now_str}. This bot trades INTRADAY ONLY — all positions close by 3:50 PM ET today.\n"
            "Only generate BUY signals for catalysts that will move the stock within the next 2-6 hours.\n"
            "SKIP anything that is a long-term thesis, old news, or lacks a same-day catalyst.\n\n"
            + "\n".join(items)
        )

    # Split into batches if needed
    filtered_items = list(filtered.items())
    batches = [
        dict(filtered_items[i:i + MAX_SYMBOLS_PER_CALL])
        for i in range(0, len(filtered_items), MAX_SYMBOLS_PER_CALL)
    ]

    try:
        all_signals = []
        for batch_num, batch in enumerate(batches):
            user_prompt = _build_prompt_for(batch)
            max_tok = min(4096, max(2000, len(batch) * 130))
            logger.info(f"Sending batch {batch_num + 1}/{len(batches)} ({len(batch)} symbols) to {ANALYSIS_MODEL}...")
            raw = _call_model(SYSTEM_PROMPT, user_prompt, max_tokens=max_tok)
            batch_signals = _parse_signals(raw)
            logger.info(f"Batch {batch_num + 1}: {len(batch_signals)} signals")
            all_signals.extend(batch_signals)

        signals = all_signals
        logger.info(f"Model returned {len(signals)} signals total")

        strong = [s for s in signals if s.get("confidence", 0) >= MIN_SENTIMENT_SCORE]
        logger.info(f"{len(strong)} signals meet confidence threshold ({MIN_SENTIMENT_SCORE})")
        # Stamp each signal with generation time for TTL enforcement downstream
        generated_at = time.time()
        for s in strong:
            s["generated_at"] = generated_at
        return strong

    except json.JSONDecodeError as e:
        logger.error(f"Model returned invalid JSON: {e}\nRaw: {raw[:500]}")
        return []
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        return []


# analyze_existing_position was removed — position management is fully rule-based
# (stop-loss / take-profit / trailing floor in trading/portfolio.py).
