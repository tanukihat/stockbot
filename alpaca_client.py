"""
Alpaca API client wrapper.
Handles account info, positions, orders, and market data.
"""
import requests
import logging
from datetime import datetime, timezone
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL

logger = logging.getLogger(__name__)

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    "Content-Type": "application/json",
}

DATA_BASE_URL = "https://data.alpaca.markets/v2"
DATA_CRYPTO_URL = "https://data.alpaca.markets/v1beta3"


def _get(path, base=ALPACA_BASE_URL, params=None):
    url = f"{base}/{path.lstrip('/')}"
    r = requests.get(url, headers=HEADERS, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def _post(path, body=None, base=ALPACA_BASE_URL):
    url = f"{base}/{path.lstrip('/')}"
    r = requests.post(url, headers=HEADERS, json=body, timeout=10)
    r.raise_for_status()
    return r.json()


def _delete(path, base=ALPACA_BASE_URL):
    url = f"{base}/{path.lstrip('/')}"
    r = requests.delete(url, headers=HEADERS, timeout=10)
    if r.status_code == 204:
        return {}
    r.raise_for_status()
    return r.json()


# --- Symbol helpers ---

def to_alpaca_symbol(symbol, asset_class):
    """
    Ensure symbol is in Alpaca's expected format for orders/closes.
    Internally we store crypto as base (BTC), Alpaca wants BTC/USD.
    """
    if asset_class == "crypto" and "/" not in symbol:
        return f"{symbol}/USD"
    return symbol


# --- Account ---

def get_account():
    return _get("/account")


def get_portfolio_value():
    acct = get_account()
    return float(acct["portfolio_value"])


def get_cash():
    acct = get_account()
    return float(acct["cash"])


def get_buying_power():
    acct = get_account()
    return float(acct["buying_power"])


# --- Positions ---

def get_positions():
    return _get("/positions")


def get_position(symbol):
    try:
        return _get(f"/positions/{symbol}")
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            return None
        raise


def close_position(symbol, reason=""):
    logger.info(f"Closing position: {symbol} — {reason}")
    try:
        return _delete(f"/positions/{symbol}")
    except requests.HTTPError as e:
        status = e.response.status_code
        if status in (403, 404, 422):
            # 403/422 = order already in flight; 404 = position already gone
            # Both mean the position is being/has been closed — treat as success
            logger.info(f"Position {symbol} already closed or close in progress (HTTP {status}) — OK")
            return {}
        logger.error(f"Failed to close {symbol}: {e}")
        return None


# --- Orders ---

def get_open_orders():
    return _get("/orders", params={"status": "open", "limit": 50})


def get_filled_orders(since=None, limit=50):
    """Return closed/filled orders, optionally filtered by a UTC timestamp string."""
    params = {"status": "closed", "limit": limit}
    if since:
        params["after"] = since
    return _get("/orders", params=params)


def cancel_all_orders():
    return _delete("/orders")


def place_market_order(symbol, side, notional=None, qty=None, asset_class="us_equity"):
    """
    Place a market order.
    Use notional for dollar amount, qty for share count.
    asset_class: 'us_equity' or 'crypto'
    """
    body = {
        "symbol": symbol,
        "side": side,
        "type": "market",
        "time_in_force": "day" if asset_class == "us_equity" else "gtc",
    }
    if notional:
        body["notional"] = str(round(notional, 2))
    elif qty:
        body["qty"] = str(qty)
    else:
        raise ValueError("Must provide notional or qty")

    logger.info(f"Placing {side} market order: {symbol} notional={notional} qty={qty}")
    return _post("/orders", body)


def place_bracket_order(symbol, side, notional, take_profit_pct, stop_loss_pct, asset_class="us_equity"):
    """
    Place a bracket order (entry + take profit + stop loss in one shot).
    Only works for us_equity with qty-based orders (not notional).
    For simplicity, we'll use separate orders for crypto.
    """
    # Get current price to calculate qty
    price = get_latest_price(symbol, asset_class)
    if not price:
        logger.error(f"Can't get price for {symbol}")
        return None

    qty = round(notional / price, 6)
    if qty <= 0:
        logger.error(f"Calculated qty={qty} for {symbol}, skipping")
        return None

    if side == "buy":
        tp_price = round(price * (1 + take_profit_pct), 2)
        sl_price = round(price * (1 - stop_loss_pct), 2)
    else:
        tp_price = round(price * (1 - take_profit_pct), 2)
        sl_price = round(price * (1 + stop_loss_pct), 2)

    body = {
        "symbol": symbol,
        "qty": str(round(qty, 4)),
        "side": side,
        "type": "market",
        "time_in_force": "gtc",
        "order_class": "bracket",
        "take_profit": {"limit_price": str(tp_price)},
        "stop_loss": {"stop_price": str(sl_price)},
    }

    logger.info(
        f"Bracket order: {side} {symbol} qty={qty:.4f} @ ~${price:.2f} "
        f"TP=${tp_price:.2f} SL=${sl_price:.2f}"
    )
    return _post("/orders", body)


def place_stop_loss(symbol, qty, stop_price, side="sell"):
    body = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "stop",
        "stop_price": str(round(stop_price, 2)),
        "time_in_force": "gtc",
    }
    return _post("/orders", body)


def place_limit_order(symbol, side, qty, limit_price, asset_class="us_equity"):
    body = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "limit",
        "limit_price": str(round(limit_price, 2)),
        "time_in_force": "day" if asset_class == "us_equity" else "gtc",
    }
    return _post("/orders", body)


# --- Market Data ---

def get_latest_price(symbol, asset_class="us_equity"):
    """Get the latest trade price for a symbol."""
    try:
        if asset_class == "crypto":
            # Alpaca crypto symbol format: BTC/USD -> BTC/USD
            sym = symbol.replace("/", "%2F")
            data = _get(f"/crypto/us/latest/trades?symbols={sym}", base=DATA_CRYPTO_URL)
            trades = data.get("trades", {})
            clean = symbol.replace("/", "")
            # Try both formats
            if symbol in trades:
                return float(trades[symbol]["p"])
            elif clean in trades:
                return float(trades[clean]["p"])
            return None
        else:
            data = _get(f"/stocks/trades/latest?symbols={symbol}", base=DATA_BASE_URL)
            trades = data.get("trades", {})
            if symbol in trades:
                return float(trades[symbol]["p"])
            return None
    except Exception as e:
        logger.error(f"Error getting price for {symbol}: {e}")
        return None


def is_market_open():
    """Check if the US stock market is currently open."""
    try:
        clock = _get("/clock")
        return clock.get("is_open", False)
    except Exception as e:
        logger.error(f"Error checking market clock: {e}")
        return False


def get_asset_info(symbol):
    """Get asset info (tradable, fractionable, etc.)"""
    try:
        return _get(f"/assets/{symbol}")
    except Exception:
        return None


def get_options_contracts(underlying, expiration_date_gte, expiration_date_lte, option_type="call"):
    """Get options contracts for a given underlying."""
    try:
        params = {
            "underlying_symbols": underlying,
            "expiration_date_gte": expiration_date_gte,
            "expiration_date_lte": expiration_date_lte,
            "type": option_type,
            "limit": 20,
        }
        return _get("/options/contracts", params=params)
    except Exception as e:
        logger.error(f"Error getting options for {underlying}: {e}")
        return None
