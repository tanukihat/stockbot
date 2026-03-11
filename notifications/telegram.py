"""
Telegram notification module.
All bot updates flow through here.
Setup: create a bot via @BotFather, get the token, and find your chat ID by
messaging your bot and hitting https://api.telegram.org/bot<TOKEN>/getUpdates
"""
import requests
import logging
from datetime import datetime
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send(message: str, parse_mode="HTML"):
    """Send a message to the configured Telegram chat."""
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
        f"  • {p['symbol']}: ${p['market_value']:.0f} ({p['unrealized_plpc']*100:+.1f}%)"
        for p in positions
    ) or "  (none)"

    msg = (
        f"🤖 <b>StockBot Online</b>\n"
        f"📊 Portfolio: <b>${pv:,.2f}</b>\n"
        f"💵 Cash: ${cash:,.2f}\n"
        f"📦 Current Positions:\n{pos_str}\n\n"
        f"🎯 Strategy: Scalping 5-10% | SL: -8% | TP: +7%\n"
        f"⚡ Scanning every 30 min. Let's get it."
    )
    send(msg)


def notify_trade_opened(trade: dict):
    sym = trade["symbol"]
    notional = trade.get("notional", 0)
    price = trade.get("entry_price", 0)
    tp = trade.get("take_profit", 0)
    sl = trade.get("stop_loss", 0)
    conf = trade.get("confidence", 0)
    reason = trade.get("reasoning", "")
    asset = trade.get("asset_class", "us_equity")

    asset_emoji = "₿" if asset == "crypto" else "📈"

    msg = (
        f"{asset_emoji} <b>OPENED: {sym}</b>\n"
        f"💰 Size: ${notional:,.0f} @ ${price:.2f}\n"
        f"✅ Take Profit: ${tp:.2f} (+7%)\n"
        f"🛑 Stop Loss: ${sl:.2f} (-8%)\n"
        f"🧠 Confidence: {conf*100:.0f}%\n"
        f"📝 {reason}"
    )
    send(msg)


def notify_trade_closed(symbol, action, pnl, pnl_pct, reason, exit_price=None):
    if pnl >= 0:
        emoji = "💰" if pnl_pct >= 0.05 else "✅"
    else:
        emoji = "🛑" if "stop" in reason.lower() else "❌"

    price_str = f" @ ${exit_price:.2f}" if exit_price else ""
    msg = (
        f"{emoji} <b>CLOSED: {symbol}</b>\n"
        f"P&L: <b>{'+' if pnl >= 0 else ''}{pnl:.2f} ({pnl_pct*100:+.1f}%)</b>\n"
        f"Reason: {reason}{price_str}"
    )
    send(msg)


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
    send(msg)


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
    send(msg)


def notify_error(message: str):
    send(f"⚠️ <b>Bot Error</b>\n{message}")


def notify_position_inherited(symbol, pnl_pct, action):
    """Notify about existing positions on startup."""
    emoji = "⚠️" if pnl_pct < -0.05 else "📦"
    msg = (
        f"{emoji} <b>Inherited position: {symbol}</b>\n"
        f"Current P&L: {pnl_pct*100:+.1f}%\n"
        f"Action taken: {action}"
    )
    send(msg)
