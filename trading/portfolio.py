"""
Portfolio management — tracks positions, allocation, and slot availability.
"""
import logging
from config import (
    MAX_POSITIONS, TARGET_STOCK_PCT, TARGET_CRYPTO_PCT, TARGET_OPTIONS_PCT,
    MAX_POSITION_PCT, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    TRAILING_ACTIVATE_PCT
)
import alpaca_client as alpaca
from data.db import get_position_peak, update_position_peak

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
        # Normalize crypto symbols to base form (Alpaca may return "BTC/USD" or "BTCUSD")
        raw_sym = p["symbol"]
        if asset_class == "crypto":
            raw_sym = raw_sym.replace("/USD", "")
            if raw_sym.endswith("USD") and len(raw_sym) > 3:
                raw_sym = raw_sym[:-3]
        pos = {
            "symbol": raw_sym,
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


def get_market_volatility_scalar() -> float:
    """
    Returns a position-size scalar (0.5–1.0) based on intraday SPY volatility.
    If SPY has moved >2% intraday, we're in a high-vol macro environment—
    shrink positions to reduce exposure. This is the macro blindspot mitigation.
    """
    try:
        open_p = alpaca.get_intraday_open_price("SPY", "us_equity")
        current_p = alpaca.get_latest_price("SPY", "us_equity")
        if open_p and current_p and open_p > 0:
            move = abs((current_p - open_p) / open_p)
            if move >= 0.03:      # SPY moved 3%+ intraday — extreme vol, half size
                logger.info(f"Macro vol scalar: SPY intraday move={move*100:.1f}% → 0.50x position size")
                return 0.50
            elif move >= 0.02:   # SPY moved 2%+ — elevated vol, reduced size
                logger.info(f"Macro vol scalar: SPY intraday move={move*100:.1f}% → 0.75x position size")
                return 0.75
    except Exception as e:
        logger.warning(f"Volatility scalar check failed: {e}")
    return 1.0  # Normal conditions — full size


def get_position_size(portfolio_value, asset_class, n_open=1):
    """
    Calculate appropriate position size based on portfolio value and targets.
    n_open: how many positions of this type will be open after this one (including it).
    Applies a macro volatility scalar to shrink size during high-vol days.
    Stays within max position size constraint.
    """
    n = max(1, n_open)
    if asset_class == "crypto":
        target_pct = TARGET_CRYPTO_PCT / n
    elif asset_class == "options":
        target_pct = TARGET_OPTIONS_PCT / n
    else:
        # Target ~3 stock slots; if we already have more, divide accordingly
        target_pct = TARGET_STOCK_PCT / max(3, n)

    # Don't let any single position exceed MAX_POSITION_PCT of portfolio
    target_pct = min(target_pct, MAX_POSITION_PCT)

    # Scale down during high macro volatility (e.g. tariff days, FOMC)
    vol_scalar = get_market_volatility_scalar()
    return portfolio_value * target_pct * vol_scalar



def check_stop_and_take_profit(state):
    """
    Check all positions for stop-loss, take-profit, or trailing stop triggers.
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
            continue

        if pct >= TAKE_PROFIT_PCT:
            to_close.append({
                "symbol": sym,
                "asset_class": pos["asset_class"],
                "reason": f"TAKE-PROFIT triggered at {pct*100:.1f}%",
                "pnl_pct": pct,
            })
            continue

        # --- Profit floor (trailing) ---
        # Once a position hits +TRAILING_ACTIVATE_PCT, that level becomes the floor.
        # If it rises further and then drops back to the floor, we sell.
        # Hard ceiling is handled above by TAKE_PROFIT_PCT.
        peak = get_position_peak(sym)
        if peak is None or pct > peak:
            update_position_peak(sym, pct)
            peak = pct

        if (peak is not None
                and peak >= TRAILING_ACTIVATE_PCT
                and pct <= TRAILING_ACTIVATE_PCT):
            to_close.append({
                "symbol": sym,
                "asset_class": pos["asset_class"],
                "reason": f"PROFIT-FLOOR: peaked at {peak*100:+.1f}%, dropped back to floor ({TRAILING_ACTIVATE_PCT*100:.0f}%)",
                "pnl_pct": pct,
            })

    return to_close


def get_symbols_held():
    """Returns set of symbols currently held, with crypto normalized to base form."""
    positions = alpaca.get_positions()
    held = set()
    for p in positions:
        sym = p["symbol"]
        if p.get("asset_class") == "crypto":
            sym = sym.replace("/USD", "")
            if sym.endswith("USD") and len(sym) > 3:
                sym = sym[:-3]
        held.add(sym)
    return held
