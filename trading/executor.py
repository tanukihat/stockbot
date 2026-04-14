"""
Trade execution logic.
Translates Claude's signals into actual Alpaca orders.
"""
import logging
from datetime import datetime, date, timedelta
from config import (
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, OPTIONS_MAX_DTE, OPTIONS_MIN_DTE,
    ALL_CRYPTO_SYMBOLS, MAX_INTRADAY_MOVE_PCT
)
import alpaca_client as alpaca
from trading.portfolio import get_position_size, get_symbols_held

logger = logging.getLogger(__name__)

CRYPTO_SYMBOLS_BASE = [s.split("/")[0] for s in ALL_CRYPTO_SYMBOLS]


def execute_signal(signal, portfolio_state):
    """
    Execute a trade based on a Claude signal.
    Returns dict with result info for notification.
    """
    sym = signal["symbol"]
    action = signal["action"]
    asset_class = signal.get("asset_class", "us_equity")
    confidence = signal.get("confidence", 0)
    reasoning = signal.get("reasoning", "")

    if action not in ("BUY", "SELL"):
        return None

    # Don't double up on existing positions
    held = get_symbols_held()
    if action == "BUY" and sym in held:
        logger.info(f"Already holding {sym}, skipping BUY signal")
        return None

    # Check position count
    state = portfolio_state
    if action == "BUY" and state["open_slots"] <= 0:
        logger.info(f"No open position slots, skipping {sym}")
        return None

    pv = state["portfolio_value"]
    # Count open positions of this type (+1 for the one we're about to open)
    n_open = len([p for p in state["all_positions"] if p["asset_class"] == asset_class]) + 1
    position_size = get_position_size(pv, asset_class, n_open=n_open)

    # Get current price
    price = alpaca.get_latest_price(
        sym if asset_class != "crypto" else f"{sym}/USD" if "/" not in sym else sym,
        asset_class
    )

    if not price:
        logger.error(f"Could not get price for {sym}")
        return None

    if action == "BUY":
        return _execute_buy(sym, asset_class, position_size, price, confidence, reasoning)
    elif action == "SELL":
        return _execute_sell(sym, asset_class, reasoning)


def _execute_buy(symbol, asset_class, notional, price, confidence, reasoning):
    """Execute a buy order with bracket (take-profit + stop-loss)."""
    logger.info(f"BUY {symbol} | ${notional:.0f} | price ~${price:.2f} | confidence={confidence:.2f}")

    # Alpaca requires "BTC/USD" format for crypto orders, not bare "BTC"
    alpaca_symbol = f"{symbol}/USD" if asset_class == "crypto" and "/" not in symbol else symbol

    # Momentum gate: skip if price has already moved too much from today's open
    try:
        open_price = alpaca.get_intraday_open_price(symbol, asset_class)
        if open_price and open_price > 0:
            intraday_move = abs(price - open_price) / open_price
            if intraday_move > MAX_INTRADAY_MOVE_PCT:
                direction = "up" if price > open_price else "down"
                logger.info(f"Momentum gate: {symbol} already moved {direction} {intraday_move*100:.1f}% from open (${open_price:.2f} → ${price:.2f}) — skipping buy")
                return {"action": "BUY_SKIPPED", "symbol": symbol, "reason": f"intraday move {intraday_move*100:.1f}% exceeds {MAX_INTRADAY_MOVE_PCT*100:.0f}% gate"}
    except Exception as e:
        logger.warning(f"Momentum gate check failed for {symbol}: {e} — proceeding anyway")

    try:
        # Use bracket orders for stocks (cleaner, one API call)
        if asset_class == "us_equity":
            result = alpaca.place_bracket_order(
                symbol=alpaca_symbol,
                side="buy",
                notional=notional,
                take_profit_pct=TAKE_PROFIT_PCT,
                stop_loss_pct=STOP_LOSS_PCT,
                asset_class=asset_class,
            )
        else:
            # Crypto: market order + manual stop tracking (Alpaca crypto doesn't support bracket)
            result = alpaca.place_market_order(
                symbol=alpaca_symbol,
                side="buy",
                notional=notional,
                asset_class="crypto",
            )

        if result:
            tp_price = round(price * (1 + TAKE_PROFIT_PCT), 2)
            sl_price = round(price * (1 - STOP_LOSS_PCT), 2)
            return {
                "action": "BUY",
                "symbol": symbol,
                "asset_class": asset_class,
                "notional": notional,
                "entry_price": price,
                "take_profit": tp_price,
                "stop_loss": sl_price,
                "confidence": confidence,
                "reasoning": reasoning,
                "order_id": result.get("id", ""),
                "status": "filled" if result.get("status") in ("filled", "new", "accepted") else result.get("status"),
            }

    except Exception as e:
        logger.error(f"Buy execution failed for {symbol}: {e}")
        return {"action": "BUY_FAILED", "symbol": symbol, "error": str(e)}


def _execute_sell(symbol, asset_class, reasoning):
    """Close an existing position."""
    logger.info(f"SELL {symbol} — {reasoning}")
    # Alpaca close_position endpoint also needs "BTC/USD" format for crypto
    alpaca_symbol = f"{symbol}/USD" if asset_class == "crypto" and "/" not in symbol else symbol
    try:
        result = alpaca.close_position(alpaca_symbol, reason=reasoning)
        if result is not None:
            return {
                "action": "SELL",
                "symbol": symbol,
                "asset_class": asset_class,
                "reasoning": reasoning,
                "status": "closed",
            }
    except Exception as e:
        logger.error(f"Sell execution failed for {symbol}: {e}")
        return {"action": "SELL_FAILED", "symbol": symbol, "error": str(e)}



def buy_options_call(underlying, confidence, portfolio_value):
    """
    Buy a short-dated call option on a high-momentum underlying.
    Target: ~2 weeks out, near-the-money.
    """
    today = date.today()
    min_exp = (today + timedelta(days=OPTIONS_MIN_DTE)).isoformat()
    max_exp = (today + timedelta(days=OPTIONS_MAX_DTE)).isoformat()

    contracts_data = alpaca.get_options_contracts(
        underlying=underlying,
        expiration_date_gte=min_exp,
        expiration_date_lte=max_exp,
        option_type="call",
    )

    if not contracts_data:
        logger.warning(f"No options contracts found for {underlying}")
        return None

    contracts = contracts_data.get("option_contracts", [])
    if not contracts:
        return None

    # Get current price to find ATM contracts
    price = alpaca.get_latest_price(underlying, "us_equity")
    if not price:
        return None

    # Find nearest-to-money call
    atm_contract = min(
        contracts,
        key=lambda c: abs(float(c.get("strike_price", 0)) - price)
    )

    contract_sym = atm_contract.get("symbol")
    strike = atm_contract.get("strike_price")
    expiry = atm_contract.get("expiration_date")

    logger.info(f"Options: buying {contract_sym} (strike={strike}, exp={expiry})")

    # Position size for options = 15% target / options allocated
    notional = get_position_size(portfolio_value, "options")
    option_price = alpaca.get_latest_price(contract_sym, "us_equity")

    if not option_price:
        logger.warning(f"Can't get options price for {contract_sym}")
        return None

    # Each contract = 100 shares
    contracts_qty = max(1, int(notional / (option_price * 100)))

    try:
        result = alpaca.place_market_order(
            symbol=contract_sym,
            side="buy",
            qty=contracts_qty,
            asset_class="us_equity",
        )
        if not result:
            logger.error(f"Options order returned no result for {contract_sym}")
            return None
        return {
            "action": "BUY_OPTION",
            "symbol": contract_sym,
            "underlying": underlying,
            "strike": strike,
            "expiry": expiry,
            "qty": contracts_qty,
            "option_price": option_price,
            "confidence": confidence,
        }
    except Exception as e:
        logger.error(f"Options buy failed for {contract_sym}: {e}")
        return None
