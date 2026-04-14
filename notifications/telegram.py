"""
Telegram notification module.
All bot updates flow through here.
Setup: create a bot via @BotFather, get the token, and find your chat ID by
messaging your bot and hitting https://api.telegram.org/bot<TOKEN>/getUpdates
"""
import requests
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, QUIET_HOURS_START, QUIET_HOURS_END, STOP_LOSS_PCT, TAKE_PROFIT_PCT, SCAN_INTERVAL_MINUTES, CRYPTO_SCAN_INTERVAL_MINUTES

logger = logging.getLogger(__name__)

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
_ET = ZoneInfo("America/New_York")

# Dedup cache: { key: last_sent_datetime }
# Prevents the same alert firing on every scan cycle for a stuck position.
_dedup_cache: dict = {}
_DEDUP_TTL_HOURS = 4


def _is_quiet_hours() -> bool:
    """Returns True if current ET time is within the configured quiet window."""
    hour = datetime.now(_ET).hour
    if QUIET_HOURS_START > QUIET_HOURS_END:
        # Wraps midnight: e.g. 23 → 8
        return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END
    return QUIET_HOURS_START <= hour < QUIET_HOURS_END


def _is_duplicate(key: str) -> bool:
    """Returns True if this key was sent within the dedup TTL window."""
    last = _dedup_cache.get(key)
    if last and (datetime.now() - last) < timedelta(hours=_DEDUP_TTL_HOURS):
        return True
    _dedup_cache[key] = datetime.now()
    return False


def send(message: str, parse_mode="HTML", urgent=False, dedup_key: str = None):
    """
    Send a message to the configured Telegram chat.
    - During quiet hours, all notifications are silently dropped.
    - If dedup_key is set, identical alerts won't repeat within _DEDUP_TTL_HOURS.
    - urgent=True bypasses quiet hours (bot errors only — use sparingly).
    """
    if not urgent and _is_quiet_hours():
        logger.debug("Quiet hours — suppressing notification")
        return

    if dedup_key and _is_duplicate(dedup_key):
        logger.debug(f"Dedup suppressed: {dedup_key}")
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — printing notification instead")
        print(f"\n📱 NOTIFICATION:\n{message}\n")
        return

    try:
        r = requests.post(
            f"{BASE_URL}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": parse_mode,
            },
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        print(f"\n📱 NOTIFICATION (unsent):\n{message}\n")


def notify_startup(portfolio_state):
    pv = portfolio_state["portfolio_value"]
    cash = portfolio_state["cash"]
    positions = portfolio_state["all_positions"]
    pos_str = "\n".join(
        f"  • {p['symbol']}: {p['qty']:.4g} shares @ ${p['current_price']:.2f} (avg ${p['avg_entry_price']:.2f}) — ${p['market_value']:.0f} ({p['unrealized_plpc']*100:+.1f}%)"
        for p in positions
    ) or "  (none)"

    msg = (
        f"🤖 <b>StockBot Online</b>\n"
        f"📊 Portfolio: <b>${pv:,.2f}</b>\n"
        f"💵 Cash: ${cash:,.2f}\n"
        f"📦 Current Positions:\n{pos_str}"
    )
    send(msg)


def notify_order_filled(symbol, side, qty, avg_price, pnl=None, pnl_pct=None):
    """Notify when a pending order fills (e.g. a pre-queued stop/sell executes)."""
    if side == "buy":
        return  # OPENED notification already covers buy info
    if side == "sell":
        pnl_str = ""
        if pnl is not None and pnl_pct is not None:
            emoji = "💰" if pnl >= 0 else "🛑"
            pnl_str = f"\nP&L: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} ({pnl_pct*100:+.1f}%)</b>"
        else:
            emoji = "✅"
        msg = (
            f"{emoji} <b>SOLD: {symbol}</b>\n"
            f"Qty: {qty} @ ${avg_price:.2f}{pnl_str}"
        )
    else:
        msg = (
            f"📈 <b>BOUGHT: {symbol}</b>\n"
            f"Qty: {qty} @ ${avg_price:.2f}"
        )
    send(msg, urgent=True)


def notify_trade_opened(trade: dict):
    sym = trade["symbol"]
    notional = trade.get("notional", 0)
    price = trade.get("entry_price", 0)
    tp = trade.get("take_profit", 0)
    sl = trade.get("stop_loss", 0)
    conf = trade.get("confidence", 0)
    reason = trade.get("reasoning", "")
    asset = trade.get("asset_class", "us_equity")
    qty = round(notional / price, 6) if price else 0
    qty_str = f"{qty:.4g} shares"

    asset_emoji = "₿" if asset == "crypto" else "📈"

    msg = (
        f"{asset_emoji} <b>OPENED: {sym}</b>\n"
        f"💰 {qty_str} @ ${price:.2f} (${notional:,.0f})\n"
        f"✅ Take Profit: ${tp:.2f} (+{TAKE_PROFIT_PCT*100:.0f}%)\n"
        f"🛑 Stop Loss: ${sl:.2f} (-{STOP_LOSS_PCT*100:.0f}%)\n"
        f"🧠 Confidence: {conf*100:.0f}%\n"
        f"📝 {reason}"
    )
    send(msg, urgent=True)


def notify_trade_closed(symbol, action, pnl, pnl_pct, reason, exit_price=None):
    if pnl >= 0:
        emoji = "💰" if pnl_pct >= 0.05 else "✅"
    else:
        emoji = "🛑" if "stop" in reason.lower() else "❌"

    price_str = f" @ ${exit_price:.2f}" if exit_price else ""

    # Inverse Cramer Score — fetch async-style (best effort, don't block the close)
    ics_line = ""
    try:
        from sentiment.cramer import format_ics_for_telegram
        ics_line = format_ics_for_telegram(symbol, pnl_pct)
    except Exception as _e:
        logger.debug(f"ICS lookup skipped for {symbol}: {_e}")

    msg = (
        f"{emoji} <b>CLOSED: {symbol}</b>\n"
        f"P&L: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} ({pnl_pct*100:+.1f}%)</b>\n"
        f"Reason: {reason}{price_str}"
        + (f"\n{ics_line}" if ics_line else "")
    )
    send(msg, urgent=True, dedup_key=f"closed:{symbol}:{reason[:20]}")


def notify_stop_loss(symbol, pnl, pnl_pct):
    notify_trade_closed(symbol, "SELL", pnl, pnl_pct, "🛑 Stop-loss triggered")


def notify_take_profit(symbol, pnl, pnl_pct):
    notify_trade_closed(symbol, "SELL", pnl, pnl_pct, "✅ Take-profit hit")


def notify_trailing_stop(symbol, pnl, pnl_pct, reason):
    msg = (
        f"📉 <b>TRAILING STOP: {symbol}</b>\n"
        f"P&L: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} ({pnl_pct*100:+.1f}%)</b>\n"
        f"📝 {reason}"
    )
    send(msg, urgent=True, dedup_key=f"trailing:{symbol}")


def notify_scan_complete(symbols_checked, signals, trades_opened):
    # Only notify if trades actually happened — signals with no trades is just noise
    if trades_opened == 0:
        logger.info(f"Scan complete: {symbols_checked} checked, {len(signals)} signals, no trades")
        return

    sig_str = ""
    for s in signals[:5]:
        emoji = "🟢" if s["action"] == "BUY" else "🔴" if s["action"] == "SELL" else "⚪"
        sig_str += f"  {emoji} {s['symbol']}: {s['action']} ({s.get('confidence', 0)*100:.0f}%)\n"

    msg = (
        f"🔍 <b>Scan Complete</b>\n"
        f"Checked {symbols_checked} symbols → {len(signals)} signals → {trades_opened} trades\n"
        + (sig_str if sig_str else "")
    )
    send(msg)


def notify_daily_summary(summary: dict, portfolio_state: dict):
    pv = portfolio_state["portfolio_value"]
    pnl = summary.get("total_pnl", 0) or 0
    trades = summary.get("total_trades", 0) or 0
    buys = summary.get("buys", 0) or 0
    sells = summary.get("sells", 0) or 0
    avg_pct = (summary.get("avg_pnl_pct", 0) or 0) * 100

    msg = (
        f"📊 <b>Daily Summary</b>\n"
        f"💼 Portfolio: ${pv:,.2f}\n"
        f"📈 Today's P&L: {'+' if pnl >= 0 else ''}${pnl:.2f}\n"
        f"🔄 Trades: {trades} ({buys} buys, {sells} sells)\n"
        f"📉 Avg P&L per closed trade: {avg_pct:+.1f}%\n"
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')} ET"
    )
    send(msg, urgent=True)


def notify_error(message: str):
    send(f"⚠️ <b>Bot Error</b>\n{message}", urgent=True)


def notify_position_inherited(symbol, pnl_pct, action):
    """Notify about existing positions on startup."""
    emoji = "⚠️" if pnl_pct < -0.05 else "📦"
    msg = (
        f"{emoji} <b>Inherited position: {symbol}</b>\n"
        f"Current P&L: {pnl_pct*100:+.1f}%\n"
        f"Action taken: {action}"
    )
    send(msg)
