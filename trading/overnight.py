"""
Overnight hold eligibility — decides which stock positions are safe to carry overnight.

Logic:
  A position is eligible to hold overnight if ALL of the following are true:
    1. Confidence at entry was >= OVERNIGHT_MIN_CONFIDENCE (tracked in DB)
    2. Current unrealized PnL >= OVERNIGHT_MIN_PNL_PCT (position is already working)
    3. Trailing stop is at breakeven or better (peak PnL >= TRAILING_ACTIVATE_PCT)
    4. No earnings within OVERNIGHT_EARNINGS_LOOKOUT_DAYS trading days
    5. SPY intraday move is < OVERNIGHT_MAX_SPY_MOVE (not a high macro vol day)

Positions that don't qualify are flatted at EOD as usual.
Overnight positions get a tighter bracket replaced at next open.
"""
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import alpaca_client as alpaca
from data.db import get_conn, get_position_peak
from config import TRAILING_ACTIVATE_PCT, FINNHUB_API_KEY

logger = logging.getLogger(__name__)

# --- Tuning ---
OVERNIGHT_MIN_CONFIDENCE    = 0.78   # Entry confidence must have been >= this
OVERNIGHT_MIN_PNL_PCT       = 0.02   # Must be up at least 2% to hold overnight
OVERNIGHT_MAX_SPY_MOVE      = 0.025  # Skip overnight holds on high-vol macro days (SPY >2.5% intraday)
OVERNIGHT_EARNINGS_DAYS     = 2      # Don't hold if earnings within this many calendar days
OVERNIGHT_MAX_POSITIONS     = 2      # Hold at most this many positions overnight


def _get_entry_confidence(symbol: str) -> float | None:
    """Pull the confidence score logged at entry from the trades table."""
    try:
        conn = get_conn()
        row = conn.execute("""
            SELECT confidence FROM trades
            WHERE symbol = ? AND action = 'BUY' AND status IN ('open', 'filled')
            ORDER BY timestamp DESC LIMIT 1
        """, (symbol,)).fetchone()
        conn.close()
        return float(row["confidence"]) if row and row["confidence"] is not None else None
    except Exception as e:
        logger.warning(f"Could not fetch entry confidence for {symbol}: {e}")
        return None


def _has_earnings_soon(symbol: str) -> bool:
    """
    Returns True if there's an earnings event within OVERNIGHT_EARNINGS_DAYS calendar days.
    Uses Finnhub earnings calendar. Returns False (safe to hold) if Finnhub is unavailable.
    """
    if not FINNHUB_API_KEY:
        logger.debug(f"No Finnhub key — skipping earnings check for {symbol}")
        return False
    try:
        import requests
        now = datetime.now(timezone.utc)
        from_date = now.strftime("%Y-%m-%d")
        to_date   = (now + timedelta(days=OVERNIGHT_EARNINGS_DAYS)).strftime("%Y-%m-%d")
        resp = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": from_date, "to": to_date, "symbol": symbol, "token": FINNHUB_API_KEY},
            timeout=5,
        )
        if resp.status_code == 200:
            earnings = resp.json().get("earningsCalendar", [])
            if earnings:
                logger.info(f"Overnight hold: {symbol} has earnings within {OVERNIGHT_EARNINGS_DAYS}d — excluded")
                return True
        return False
    except Exception as e:
        logger.warning(f"Earnings check failed for {symbol}: {e}")
        return False  # Default to safe if check fails


def _spy_too_volatile() -> bool:
    """Returns True if SPY has moved more than OVERNIGHT_MAX_SPY_MOVE intraday — too risky to hold."""
    try:
        open_p    = alpaca.get_intraday_open_price("SPY", "us_equity")
        current_p = alpaca.get_latest_price("SPY", "us_equity")
        if open_p and current_p and open_p > 0:
            move = abs((current_p - open_p) / open_p)
            if move >= OVERNIGHT_MAX_SPY_MOVE:
                logger.info(f"Overnight hold: SPY intraday move={move*100:.1f}% >= {OVERNIGHT_MAX_SPY_MOVE*100:.1f}% — no overnight holds today")
                return True
    except Exception as e:
        logger.warning(f"SPY volatility check failed for overnight eligibility: {e}")
    return False


def get_overnight_eligible(stock_positions: list, aggregated: dict | None = None) -> set:
    """
    Given a list of stock position dicts (from get_portfolio_state),
    return a set of symbols that are eligible to hold overnight.

    aggregated: optional sentiment aggregator output — used to check has_earnings_today
    flag already computed by the aggregator, avoiding a redundant Finnhub API call.

    At most OVERNIGHT_MAX_POSITIONS symbols are returned — ranked by PnL descending.
    """
    if not stock_positions:
        return set()

    # Fast-fail: if market is ripping/crashing, don't hold anything overnight
    if _spy_too_volatile():
        return set()

    candidates = []
    for pos in stock_positions:
        sym     = pos["symbol"]
        pnl_pct = pos["unrealized_plpc"]
        peak    = get_position_peak(sym) or 0.0

        # 1. Must be profitable enough to bother holding
        if pnl_pct < OVERNIGHT_MIN_PNL_PCT:
            logger.info(f"Overnight hold: {sym} excluded — PnL {pnl_pct*100:+.1f}% < {OVERNIGHT_MIN_PNL_PCT*100:.0f}% minimum")
            continue

        # 2. Must have trailing stop activated (at breakeven or better)
        if peak < TRAILING_ACTIVATE_PCT:
            logger.info(f"Overnight hold: {sym} excluded — peak PnL {peak*100:+.1f}% hasn't hit trailing floor ({TRAILING_ACTIVATE_PCT*100:.0f}%)")
            continue

        # 3. Entry confidence must have been high
        conf = _get_entry_confidence(sym)
        if conf is None or conf < OVERNIGHT_MIN_CONFIDENCE:
            logger.info(f"Overnight hold: {sym} excluded — entry confidence {conf} < {OVERNIGHT_MIN_CONFIDENCE}")
            continue

        # 4. No earnings coming up — check aggregator flag first, fall back to Finnhub API
        agg_data = (aggregated or {}).get(sym, {})
        if agg_data.get("has_earnings_today"):
            logger.info(f"Overnight hold: {sym} excluded — earnings today (aggregator flag)")
            continue
        if _has_earnings_soon(sym):
            continue

        logger.info(f"Overnight hold: {sym} ELIGIBLE — PnL={pnl_pct*100:+.1f}%, peak={peak*100:+.1f}%, conf={conf:.2f}")
        candidates.append((sym, pnl_pct))

    # Rank by PnL descending, cap at OVERNIGHT_MAX_POSITIONS
    candidates.sort(key=lambda x: x[1], reverse=True)
    eligible = {sym for sym, _ in candidates[:OVERNIGHT_MAX_POSITIONS]}

    if eligible:
        logger.info(f"Overnight hold: holding {eligible} overnight")
    else:
        logger.info("Overnight hold: no positions eligible — full EOD flatten")

    return eligible
