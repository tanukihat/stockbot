"""
events_writer.py — Log notable events to the stockbot status dashboard.

Events are written to /var/www/stockbot-status/events.json and picked up
live by the status server on each API call. Deduplicates by label+date so
the same event won't be logged twice in one day.
"""
import json
import os
import re
import logging
from datetime import date as _date

import urllib.request

logger = logging.getLogger(__name__)

EVENTS_URL    = os.getenv("STATUS_PAGE_URL", "")  # e.g. https://yourdomain.com/stockbot/api/event
EVENT_SECRET  = os.getenv("STATUS_PAGE_SECRET", "")
MILESTONE_STATE_FILE = os.path.join(os.path.dirname(__file__), "data", "milestone_state.json")

# Percentage milestones to celebrate
PORTFOLIO_MILESTONES = [5, 10, 15, 20, 25, 30, 40, 50, 75, 100]

# Macro/earnings keyword patterns
_EARNINGS_RE = re.compile(
    r'\b(earnings call|earnings report|quarterly results|EPS beat|EPS miss|'
    r'beats estimates|misses estimates|revenue beat|revenue miss|Q[1-4] earnings)\b',
    re.IGNORECASE
)

_MACRO_RE = re.compile(
    r'\b(FOMC meeting|rate decision|rate cut|rate hike|Fed decision|'
    r'CPI report|inflation data|jobs report|nonfarm payroll|GDP report|'
    r'Fed minutes|Jackson Hole|Powell speech)\b',
    re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Core writer
# ---------------------------------------------------------------------------

def log_event(label, detail="", emoji="📌", color="#94a3b8", date=None):
    """
    POST an event to the status server API. Deduplication handled server-side.
    Returns True if written, False if skipped (duplicate) or failed.
    """
    d = str(date or _date.today())
    payload = json.dumps({
        "label": label, "detail": detail,
        "emoji": emoji, "color": color, "date": d,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            EVENTS_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Event-Secret": EVENT_SECRET,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("skipped"):
                return False
            logger.info(f"Dashboard event logged: [{emoji}] {label} ({d})")
            return True
    except Exception as e:
        logger.warning(f"Failed to log dashboard event '{label}': {e}")
        return False


# ---------------------------------------------------------------------------
# Milestone tracker
# ---------------------------------------------------------------------------

def _load_milestone_state():
    try:
        with open(MILESTONE_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"base_value": None, "peak_portfolio": None, "milestones_hit": []}


def _save_milestone_state(state):
    os.makedirs(os.path.dirname(MILESTONE_STATE_FILE), exist_ok=True)
    with open(MILESTONE_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def check_milestones(portfolio_value, base_value=None):
    """
    Call after each trading cycle with the current portfolio value.
    Logs events for:
      - First time hitting +5%, +10%, +15% ... milestones
      - New all-time high (when meaningfully above previous peak)
    """
    state = _load_milestone_state()

    # Seed base_value on first call
    if state["base_value"] is None:
        state["base_value"] = base_value or portfolio_value
    if state["peak_portfolio"] is None:
        state["peak_portfolio"] = portfolio_value

    base = base_value or state["base_value"]
    pct_gain = (portfolio_value / base - 1) * 100

    # Percentage milestones
    for threshold in PORTFOLIO_MILESTONES:
        label = f"Portfolio +{threshold}%"
        if pct_gain >= threshold and label not in state["milestones_hit"]:
            state["milestones_hit"].append(label)
            emoji = "💎" if threshold >= 50 else ("🏆" if threshold >= 20 else "📈")
            log_event(
                label=label,
                detail=f"Portfolio hit +{threshold}% total return. Value: ${portfolio_value:,.2f}",
                emoji=emoji,
                color="#22c55e",
            )

    # All-time high — only log if at least 1% above previous peak to avoid noise
    peak = state["peak_portfolio"]
    if portfolio_value > peak * 1.01 and portfolio_value > base * 1.02:
        state["peak_portfolio"] = portfolio_value
        log_event(
            label="New portfolio ATH",
            detail=f"Portfolio hit a new all-time high: ${portfolio_value:,.2f} "
                   f"(+{pct_gain:.1f}% from start)",
            emoji="🔥",
            color="#f59e0b",
        )
    elif portfolio_value > peak:
        # Update peak silently (below the 1% bar)
        state["peak_portfolio"] = portfolio_value

    _save_milestone_state(state)


# ---------------------------------------------------------------------------
# Big win detector (call on a filled SELL order)
# ---------------------------------------------------------------------------

def log_close_event(symbol, pnl_pct, pnl_abs, exit_price, reason):
    """
    Log every position close to the dashboard with its reason.
    Called on stop-loss, take-profit, trailing stop, EOD flatten, stale close.
    """
    if pnl_pct is None:
        return
    pct_str = f"{pnl_pct * 100:+.1f}%"
    # Pick emoji based on reason type
    reason_upper = reason.upper()
    if "STOP" in reason_upper:
        emoji, color = "🛑", "#ef4444"
    elif "TAKE-PROFIT" in reason_upper or "TAKE_PROFIT" in reason_upper:
        emoji, color = "✅", "#22c55e"
    elif "FLOOR" in reason_upper or "TRAILING" in reason_upper:
        emoji, color = "📉", "#f59e0b"
    elif "EOD" in reason_upper or "FLATTEN" in reason_upper:
        emoji, color = "🔔", "#64748b"
    elif "STALE" in reason_upper:
        emoji, color = "⏰", "#64748b"
    else:
        emoji, color = "💱", "#94a3b8"

    label = f"{symbol} closed {pct_str} — {reason}"
    detail = f"Closed {symbol} at ${exit_price:.2f} for {pct_str} (${pnl_abs:+,.2f}). Reason: {reason}"
    log_event(label=label, detail=detail, emoji=emoji, color=color)


def check_trade_win(symbol, pnl_pct, pnl_abs, exit_price):
    """
    Log an event when a position closes with 5%+ profit.
    pnl_pct should be a decimal (0.05 = 5%).
    """
    if pnl_pct < 0.05:
        return
    pct_str = f"+{pnl_pct * 100:.1f}%"
    emoji = "💰" if pnl_pct < 0.15 else ("🤑" if pnl_pct < 0.30 else "🚀")
    log_event(
        label=f"{symbol} {pct_str} win",
        detail=f"Closed {symbol} at ${exit_price:.2f} for {pct_str} gain (${pnl_abs:+,.2f})",
        emoji=emoji,
        color="#22c55e",
    )


# ---------------------------------------------------------------------------
# Earnings / macro detector (call after aggregate_sentiment)
# ---------------------------------------------------------------------------

def check_sentiment_events(aggregated):
    """
    Scan the aggregated sentiment context for earnings or major macro events.
    Only logs if the signal is specific enough (e.g. "earnings call", "rate decision")
    rather than generic mentions — avoids flooding on normal market chatter.

    aggregated: dict of {symbol: {context: str, mention_count: int, ...}}
    """
    earnings_seen = set()
    macro_seen = set()

    for sym, data in aggregated.items():
        ctx = data.get("context", "")
        mentions = data.get("mention_count", 0)

        # Earnings: only flag symbols with real traction (not a one-off mention)
        if mentions >= 10 and _EARNINGS_RE.search(ctx):
            match = _EARNINGS_RE.search(ctx)
            earnings_seen.add((sym, match.group(0)))

        # Macro: specific high-impact phrases only
        for m in _MACRO_RE.finditer(ctx):
            macro_seen.add(m.group(0))

    # Log up to 3 earnings events (most-mentioned symbols first)
    for sym, phrase in list(earnings_seen)[:3]:
        log_event(
            label=f"{sym} earnings",
            detail=f"Earnings activity for {sym} detected in social feeds: \"{phrase}\"",
            emoji="📊",
            color="#a78bfa",
        )

    # Log macro events — dedup by normalized phrase, cap at 2 per cycle
    logged_macro = 0
    seen_normalized = set()
    for phrase in macro_seen:
        key = phrase.lower().strip()
        if key in seen_normalized:
            continue
        seen_normalized.add(key)
        log_event(
            label=f"Macro: {phrase}",
            detail=f"\"{phrase}\" trending across social sentiment feeds.",
            emoji="🏛️",
            color="#64748b",
        )
        logged_macro += 1
        if logged_macro >= 2:
            break
