"""
Portfolio management — tracks positions, allocation, and slot availability.
"""
import logging
from config import (
    MAX_POSITIONS, TARGET_STOCK_PCT, TARGET_CRYPTO_PCT, TARGET_OPTIONS_PCT,
    MAX_POSITION_PCT, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    TRAILING_STOP_PCT, TRAILING_ACTIVATE_PCT
)
import alpaca_client as alpaca

logger = logging.getLogger(__name__)


def get_portfolio_state():
    """
    Returns a comprehensive snapshot of the current portfolio.
    """
    positions = alpaca.get_positions()
    account = alpaca.get_account()
    portfolio_value = float(account["portfolio_value"])
    cash = float(account["cash"])

    stock_positions = []
    crypto_positions = []
    options_positions = []

    for p in positions:
        asset_class = p.get("asset_class", "us_equity")
        pos = {
            "symbol": p["symbol"],
            "asset_class": asset_class,
            "qty": float(p["qty"]),
            "avg_entry_price": float(p["avg_entry_price"]),
            "current_price": float(p["current_price"]),
            "market_value": float(p["market_value"]),
            "unrealized_pl": float(p["unrealized_pl"]),
            "unrealized_plpc": float(p["unrealized_plpc"]),
            "side": p["side"],
        }
        if asset_class == "crypto":
            crypto_positions.append(pos)
        elif "option" in asset_class:
            options_positions.append(pos)
        else:
            stock_positions.append(pos)

    total_positions = len(stock_positions) + len(crypto_positions) + len(options_positions)
    open_slots = MAX_POSITIONS - total_positions

    stock_value = sum(p["market_value"] for p in stock_positions)
    crypto_value = sum(p["market_value"] for p in crypto_positions)
    options_value = sum(p["market_value"] for p in options_positions)

    return {
        "portfolio_value": portfolio_value,
        "cash": cash,
        "stock_positions": stock_positions,
        "crypto_positions": crypto_positions,
        "options_positions": options_positions,
        "all_positions": stock_positions + crypto_positions + options_positions,
        "total_positions": total_positions,
        "open_slots": open_slots,
        "stock_value": stock_value,
        "crypto_value": crypto_value,
        "options_value": options_value,
        "stock_pct": stock_value / portfolio_value if portfolio_value else 0,
        "crypto_pct": crypto_value / portfolio_value if portfolio_value else 0,
        "options_pct": options_value / portfolio_value if portfolio_value else 0,
    }


def get_position_size(portfolio_value, asset_class):
    """
    Calculate appropriate position size based on portfolio value and targets.
    Stays within max position size constraint.
    """
    if asset_class == "crypto":
        target_pct = TARGET_CRYPTO_PCT / max(1, 1)  # roughly 1 crypto position
    elif asset_class == "options":
        target_pct = TARGET_OPTIONS_PCT / max(1, 1)
    else:
        target_pct = TARGET_STOCK_PCT / 3  # aim for ~3 stock positions

    # Don't let any single position exceed MAX_POSITION_PCT of portfolio
    target_pct = min(target_pct, MAX_POSITION_PCT)
    return portfolio_value * target_pct


def check_needs_rebalance(state):
    """
    Check if portfolio allocation is significantly off-target.
    Returns a list of rebalance suggestions.
    """
    suggestions = []
    pv = state["portfolio_value"]

    stock_diff = state["stock_pct"] - TARGET_STOCK_PCT
    crypto_diff = state["crypto_pct"] - TARGET_CRYPTO_PCT

    if abs(stock_diff) > 0.15:
        direction = "reduce" if stock_diff > 0 else "increase"
        suggestions.append(f"Stocks at {state['stock_pct']*100:.0f}% vs target {TARGET_STOCK_PCT*100:.0f}% — {direction}")

    if abs(crypto_diff) > 0.10:
        direction = "reduce" if crypto_diff > 0 else "increase"
        suggestions.append(f"Crypto at {state['crypto_pct']*100:.0f}% vs target {TARGET_CRYPTO_PCT*100:.0f}% — {direction}")

    return suggestions


def check_stop_and_take_profit(state):
    """
    Check all positions for stop-loss or take-profit triggers.
    Returns list of positions to close with reasons.
    """
    to_close = []

    for pos in state["all_positions"]:
        pct = pos["unrealized_plpc"]
        sym = pos["symbol"]

        if pct <= -STOP_LOSS_PCT:
            to_close.append({
                "symbol": sym,
                "asset_class": pos["asset_class"],
                "reason": f"STOP-LOSS triggered at {pct*100:.1f}%",
                "pnl_pct": pct,
            })
        elif pct >= TAKE_PROFIT_PCT:
            to_close.append({
                "symbol": sym,
                "asset_class": pos["asset_class"],
                "reason": f"TAKE-PROFIT triggered at {pct*100:.1f}%",
                "pnl_pct": pct,
            })

    return to_close


def get_symbols_held():
    """Returns set of symbols currently held."""
    positions = alpaca.get_positions()
    return {p["symbol"] for p in positions}
