"""
Sentiment-based exit logic — cuts positions when sentiment reverses before price hits stop-loss.

How it works:
  Each trading cycle, held stock symbols are re-scored by Claude alongside new signals.
  If a held symbol comes back SELL or HOLD with low confidence, it gets a "strike".
  Two consecutive strikes → exit the position immediately, don't wait for -5%.

  Strikes reset to 0 if sentiment recovers above the recovery threshold.

Thresholds (tunable):
  SENTIMENT_EXIT_THRESHOLD   — score below this is considered negative (default: 0.35)
  SENTIMENT_RECOVERY_THRESHOLD — score above this clears the strike count (default: 0.55)
  SENTIMENT_EXIT_STRIKES     — how many consecutive bad readings before we exit (default: 2)
  SENTIMENT_EXIT_MIN_MENTIONS — require at least this many mentions to trust a negative reading
"""
import logging
import time

logger = logging.getLogger(__name__)

# --- Tuning ---
SENTIMENT_EXIT_THRESHOLD     = 0.35   # Confidence below this = bearish reading
SENTIMENT_RECOVERY_THRESHOLD = 0.55   # Confidence above this = strike reset
SENTIMENT_EXIT_STRIKES       = 2      # Consecutive bad readings before exit
SENTIMENT_EXIT_MIN_MENTIONS  = 3      # Ignore reversals with fewer than this many mentions
SENTIMENT_REENTRY_COOLDOWN_SECS = 2 * 3600  # 2h cooldown before re-buying a sentiment-exited symbol

# In-memory strike tracker: { symbol: int }
_sentiment_strikes: dict[str, int] = {}

# In-memory re-entry cooldown tracker: { symbol: float (unix timestamp of exit) }
_sentiment_exit_times: dict[str, float] = {}


def check_sentiment_exits(held_symbols: set, signals: list, aggregated: dict) -> list:
    """
    Evaluate held positions for sentiment reversal exits.

    Args:
        held_symbols: set of symbols currently held (stock only — crypto stays 24/7)
        signals: list of analyzed signals from claude_analyzer (may include held symbols)
        aggregated: raw aggregated sentiment dict (for mention count check)

    Returns:
        list of dicts: [{"symbol": str, "reason": str, "pnl_pct": None}]
        (pnl_pct filled in by caller from portfolio state)
    """
    global _sentiment_strikes
    to_exit = []

    # Build a lookup from signals by symbol
    signal_map = {s["symbol"]: s for s in signals}

    for sym in held_symbols:
        signal = signal_map.get(sym)
        raw = aggregated.get(sym, {})
        mention_count = raw.get("mention_count", 0)

        if signal is None:
            # Symbol not in sentiment this cycle — no data, don't penalize
            logger.debug(f"Sentiment exit: {sym} not in this cycle's signals — skipping")
            continue

        action = signal.get("action", "HOLD")
        confidence = signal.get("confidence", 0.5)

        # Low mention count — not enough signal to trust a reversal
        if mention_count < SENTIMENT_EXIT_MIN_MENTIONS:
            logger.debug(f"Sentiment exit: {sym} only {mention_count} mentions — ignoring low-data reading")
            continue

        is_bearish = (action in ("SELL", "HOLD") and confidence < SENTIMENT_EXIT_THRESHOLD)
        is_recovered = confidence >= SENTIMENT_RECOVERY_THRESHOLD

        if is_bearish:
            _sentiment_strikes[sym] = _sentiment_strikes.get(sym, 0) + 1
            strikes = _sentiment_strikes[sym]
            logger.info(
                f"Sentiment exit: {sym} bearish reading "
                f"(action={action}, conf={confidence:.2f}, mentions={mention_count}) "
                f"— strike {strikes}/{SENTIMENT_EXIT_STRIKES}"
            )
            if strikes >= SENTIMENT_EXIT_STRIKES:
                reason = (
                    f"Sentiment reversal: {strikes} consecutive bearish readings "
                    f"(conf={confidence:.2f}, action={action})"
                )
                to_exit.append({"symbol": sym, "reason": reason, "pnl_pct": None})
                # Reset strikes after acting so we don't re-trigger on re-entry
                _sentiment_strikes[sym] = 0

        elif is_recovered:
            if _sentiment_strikes.get(sym, 0) > 0:
                logger.info(f"Sentiment exit: {sym} recovered (conf={confidence:.2f}) — clearing strikes")
            _sentiment_strikes[sym] = 0

        else:
            # Neutral / ambiguous — don't add strikes, don't reset
            logger.debug(f"Sentiment exit: {sym} neutral (action={action}, conf={confidence:.2f}) — no change")

    return to_exit


def reset_strikes(symbol: str, sentiment_exit: bool = False):
    """Call when a position is closed for any reason, to clean up state.
    Pass sentiment_exit=True to start the 2h re-entry cooldown for this symbol."""
    _sentiment_strikes.pop(symbol, None)
    if sentiment_exit:
        _sentiment_exit_times[symbol] = time.time()
        logger.info(f"Re-entry cooldown started for {symbol} ({SENTIMENT_REENTRY_COOLDOWN_SECS // 3600:.0f}h)")
    else:
        _sentiment_exit_times.pop(symbol, None)


def is_reentry_allowed(symbol: str) -> bool:
    """Returns True if the symbol is past its re-entry cooldown (or never had one)."""
    exit_time = _sentiment_exit_times.get(symbol)
    if exit_time is None:
        return True
    elapsed = time.time() - exit_time
    if elapsed >= SENTIMENT_REENTRY_COOLDOWN_SECS:
        _sentiment_exit_times.pop(symbol, None)
        return True
    remaining_min = (SENTIMENT_REENTRY_COOLDOWN_SECS - elapsed) / 60
    logger.info(f"Re-entry blocked for {symbol} — sentiment exit cooldown expires in {remaining_min:.0f}min")
    return False


def get_strikes(symbol: str) -> int:
    """Returns current strike count for a symbol."""
    return _sentiment_strikes.get(symbol, 0)
