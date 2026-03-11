"""
StockBot — Autonomous AI trading bot
Main entry point and scheduler.

Runs sentiment analysis + trade execution on a schedule.
Market hours: every 30 min | Crypto (24/7): every 60 min | Daily summary: 4pm ET
"""
import logging
import sys
import time
from datetime import datetime, timezone
import schedule

import alpaca_client as alpaca
from sentiment.aggregator import aggregate_sentiment
from analysis.claude_analyzer import analyze_sentiment_batch
from trading.portfolio import get_portfolio_state, check_stop_and_take_profit
from trading.executor import execute_signal, buy_options_call
from notifications import telegram
from data.db import (
    init_db, log_trade, open_position, close_position_db,
    log_scan, get_daily_summary, get_open_position_age
)
from config import (
    MAX_POSITIONS, TARGET_OPTIONS_PCT, TARGET_CRYPTO_PCT, TARGET_STOCK_PCT,
    STOP_LOSS_PCT, MIN_SENTIMENT_SCORE, MIN_SENTIMENT_SCORE_URGENT,
    ALL_CRYPTO_SYMBOLS, SCAN_INTERVAL_MINUTES, CRYPTO_SCAN_INTERVAL_MINUTES,
    EOD_CLOSE_STOCKS, EOD_CLOSE_TIME
)

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("stockbot.log"),
    ],
)
logger = logging.getLogger("stockbot.main")

CRYPTO_SYMBOLS_BASE = [s.split("/")[0] for s in ALL_CRYPTO_SYMBOLS]

# Track the last AMD-cleanup so we don't run it every cycle
_startup_cleanup_done = False


def handle_existing_positions(state):
    """
    On startup / each cycle: enforce stop-loss and take-profit on existing positions.
    Also closes deeply underwater positions the previous session left open.
    """
    to_close = check_stop_and_take_profit(state)

    for item in to_close:
        sym = item["symbol"]
        reason = item["reason"]
        pnl_pct = item["pnl_pct"]

        # Find full position data
        all_pos = {p["symbol"]: p for p in state["all_positions"]}
        pos = all_pos.get(sym)
        if not pos:
            continue

        pnl = pos["unrealized_pl"]
        exit_price = pos["current_price"]

        result = alpaca.close_position(alpaca.to_alpaca_symbol(sym, item["asset_class"]), reason=reason)
        if result is not None:
            close_position_db(sym, exit_price, pnl, pnl_pct)
            if "TRAILING" in reason.upper():
                telegram.notify_trailing_stop(sym, pnl, pnl_pct, reason)
            elif "STOP" in reason.upper():
                telegram.notify_stop_loss(sym, pnl, pnl_pct)
            else:
                telegram.notify_take_profit(sym, pnl, pnl_pct)
            logger.info(f"Closed {sym}: {reason} | PnL: {pnl_pct*100:+.1f}%")

    # Close any positions held stale for >48h without hitting targets
    for pos in state["all_positions"]:
        sym = pos["symbol"]
        if sym in {i["symbol"] for i in to_close}:
            continue  # already handled
        age_hours = get_open_position_age(sym)
        if age_hours and age_hours > 48:
            logger.info(f"{sym} held for {age_hours:.1f}h — closing stale position")
            result = alpaca.close_position(alpaca.to_alpaca_symbol(sym, pos["asset_class"]), reason="Stale position >48h")
            if result is not None:
                pnl = pos["unrealized_pl"]
                pnl_pct = pos["unrealized_plpc"]
                close_position_db(sym, pos["current_price"], pnl, pnl_pct)
                telegram.notify_trade_closed(sym, "SELL", pnl, pnl_pct, "Stale position >48h", pos["current_price"])


def startup_position_audit():
    """
    On first run: audit inherited positions.
    AMD at -16.6% is already past stop-loss — close it.
    """
    global _startup_cleanup_done
    if _startup_cleanup_done:
        return

    state = get_portfolio_state()
    logger.info("Startup audit of inherited positions...")

    for pos in state["all_positions"]:
        sym = pos["symbol"]
        pnl_pct = pos["unrealized_plpc"]

        if pnl_pct <= -STOP_LOSS_PCT:
            # Past stop-loss threshold — close immediately
            action_taken = f"Closed immediately (inherited position {pnl_pct*100:+.1f}%, past stop-loss)"
            logger.warning(f"Inherited {sym} at {pnl_pct*100:+.1f}% — closing")
            # Register in DB first so close_position_db has a row to update
            open_position(sym, pos["asset_class"], pos["avg_entry_price"], pos["market_value"])
            alpaca.close_position(alpaca.to_alpaca_symbol(sym, pos["asset_class"]), reason="Inherited position past stop-loss")
            close_position_db(sym, pos["current_price"], pos["unrealized_pl"], pnl_pct)
            telegram.notify_position_inherited(sym, pnl_pct, action_taken)
        else:
            action_taken = f"Monitoring (P&L: {pnl_pct*100:+.1f}%)"
            telegram.notify_position_inherited(sym, pnl_pct, action_taken)
            # Register in DB so age tracking works
            open_position(sym, pos["asset_class"], pos["avg_entry_price"], pos["market_value"])

    _startup_cleanup_done = True


def run_trading_cycle(scan_stocks=True, scan_crypto=True):
    """
    Main trading cycle:
    1. Check + enforce stop-loss / take-profit on open positions
    2. Scrape sentiment
    3. Analyze with Claude
    4. Execute signals
    5. Notify
    """
    logger.info(f"=== Trading cycle start | stocks={scan_stocks} crypto={scan_crypto} ===")
    start = time.time()

    try:
        # --- Step 1: Position management ---
        state = get_portfolio_state()
        handle_existing_positions(state)

        # Refresh state after closes
        state = get_portfolio_state()

        if state["open_slots"] <= 0:
            logger.info("All position slots full — skipping sentiment scan")
            log_scan(0, 0, 0, "All slots full")
            return

        # --- Step 2: Sentiment scraping ---
        aggregated = aggregate_sentiment(scan_crypto=scan_crypto, scan_stocks=scan_stocks)

        if not aggregated:
            logger.info("No sentiment signals found this cycle")
            log_scan(0, 0, 0, "No sentiment data")
            return

        # --- Step 3: Claude analysis ---
        signals = analyze_sentiment_batch(aggregated)

        # Filter signals for symbols we don't already hold
        held = {p["symbol"] for p in state["all_positions"]}
        signals = [s for s in signals if s["symbol"] not in held]

        # Sort: HIGH urgency first, then by confidence descending
        signals.sort(key=lambda x: (
            0 if x.get("urgency") == "HIGH" else 1,
            -x.get("confidence", 0)
        ))

        # Log all actionable signals for observability
        for s in signals:
            logger.info(
                f"Signal: {s['action']} {s['symbol']} "
                f"conf={s.get('confidence', 0):.2f} urgency={s.get('urgency', '?')} "
                f"wsb={s.get('wsb_signal', False)} — {s.get('reasoning', '')[:80]}"
            )

        # --- Step 4: Execute top signals ---
        trades_executed = 0
        executed_symbols = set()

        # Refresh state before each execution to respect slot limits
        market_open = alpaca.is_market_open()

        for signal in signals:
            if signal["action"] not in ("BUY",):
                continue

            current_state = get_portfolio_state()
            if current_state["open_slots"] <= 0:
                logger.info("Position slots full mid-cycle, stopping execution")
                break

            sym = signal["symbol"]
            asset_class = signal.get("asset_class", "us_equity")

            # Don't try to place stock orders after hours — bracket orders will be rejected
            if asset_class == "us_equity" and not market_open:
                logger.info(f"Skipping {sym} — market closed, stock orders not accepted")
                continue

            # HIGH urgency signals (squeeze plays etc.) get a lower confidence bar
            urgency = signal.get("urgency", "LOW")
            threshold = MIN_SENTIMENT_SCORE_URGENT if urgency == "HIGH" else MIN_SENTIMENT_SCORE
            if signal.get("confidence", 0) < threshold:
                logger.info(f"Skipping {sym} — confidence {signal.get('confidence',0):.2f} below threshold {threshold} (urgency={urgency})")
                continue

            # Respect allocation targets
            if asset_class == "crypto" and current_state["crypto_pct"] >= TARGET_CRYPTO_PCT + 0.10:
                logger.info(f"Skipping {sym} — crypto allocation already at target")
                continue
            if asset_class == "us_equity" and current_state["stock_pct"] >= TARGET_STOCK_PCT + 0.10:
                logger.info(f"Skipping {sym} — stock allocation already at target")
                continue

            result = execute_signal(signal, current_state)

            if result and result.get("action") == "BUY":
                trades_executed += 1
                executed_symbols.add(sym)

                # Log to DB
                log_trade(result)
                open_position(sym, asset_class, result.get("entry_price", 0), result.get("notional", 0))

                # Notify Telegram
                telegram.notify_trade_opened(result)

                logger.info(f"Executed BUY: {sym} (confidence={signal.get('confidence', 0):.2f})")

            elif result and "FAILED" in result.get("action", ""):
                telegram.notify_error(f"Trade failed: {sym} — {result.get('error', '')}")

        # --- Options: if we have signal budget and options slot available ---
        current_state = get_portfolio_state()
        if (current_state["open_slots"] > 0 and
                current_state["options_pct"] < TARGET_OPTIONS_PCT - 0.05 and
                trades_executed > 0):
            # Find the highest-confidence stock signal to buy calls on
            stock_signals = [s for s in signals if s.get("asset_class") == "us_equity"
                             and s["action"] == "BUY" and s.get("confidence", 0) >= 0.75]
            if stock_signals:
                top = stock_signals[0]
                opt_result = buy_options_call(
                    underlying=top["symbol"],
                    confidence=top["confidence"],
                    portfolio_value=current_state["portfolio_value"]
                )
                if opt_result:
                    log_trade(opt_result)
                    telegram.send(
                        f"📊 <b>OPTIONS: Bought {opt_result['qty']}x {opt_result['symbol']}</b>\n"
                        f"Strike: ${opt_result['strike']} exp {opt_result['expiry']}\n"
                        f"Underlying: {opt_result['underlying']}"
                    )
                    trades_executed += 1

        # --- Step 5: Scan summary ---
        log_scan(len(aggregated), len(signals), trades_executed)
        telegram.notify_scan_complete(len(aggregated), signals[:5], trades_executed)

        elapsed = time.time() - start
        logger.info(f"=== Cycle complete in {elapsed:.1f}s | {trades_executed} trades executed ===")

    except Exception as e:
        logger.error(f"Trading cycle error: {e}", exc_info=True)
        telegram.notify_error(f"Cycle error: {str(e)[:200]}")


def run_market_cycle():
    """Run during US market hours (stocks + crypto)."""
    if alpaca.is_market_open():
        run_trading_cycle(scan_stocks=True, scan_crypto=True)
    else:
        logger.info("Market closed — skipping stock scan")


def run_crypto_cycle():
    """Run crypto-only scan (24/7)."""
    if not alpaca.is_market_open():
        run_trading_cycle(scan_stocks=False, scan_crypto=True)


def close_all_stock_positions():
    """
    EOD flatten — close all stock positions by EOD_CLOSE_TIME.
    Crypto stays open 24/7. This is day trader mode.
    """
    if not EOD_CLOSE_STOCKS:
        return
    state = get_portfolio_state()
    stock_pos = state["stock_positions"]
    if not stock_pos:
        logger.info("EOD close: no stock positions to flatten")
        return
    logger.info(f"EOD close: flattening {len(stock_pos)} stock position(s)")
    for pos in stock_pos:
        sym = pos["symbol"]
        pnl = pos["unrealized_pl"]
        pnl_pct = pos["unrealized_plpc"]
        result = alpaca.close_position(
            alpaca.to_alpaca_symbol(sym, "us_equity"), reason="EOD flatten"
        )
        if result is not None:
            close_position_db(sym, pos["current_price"], pnl, pnl_pct)
            telegram.notify_trade_closed(sym, "SELL", pnl, pnl_pct, "EOD flatten", pos["current_price"])
            logger.info(f"EOD closed {sym}: PnL {pnl_pct*100:+.1f}%")


def send_daily_summary():
    """4pm ET daily summary."""
    state = get_portfolio_state()
    summary = get_daily_summary()
    telegram.notify_daily_summary(summary, state)


def main():
    logger.info("🚀 StockBot starting up...")

    # Initialize database
    init_db()

    # Startup: audit inherited positions
    startup_position_audit()

    # Send startup notification
    state = get_portfolio_state()
    telegram.notify_startup(state)

    # --- Schedule ---
    # Market hours: configurable interval (default 15 min)
    schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(run_market_cycle)

    # Crypto: configurable (default 30 min, runs 24/7)
    schedule.every(CRYPTO_SCAN_INTERVAL_MINUTES).minutes.do(run_crypto_cycle)

    # EOD flatten: close all stock positions before close
    schedule.every().day.at(EOD_CLOSE_TIME).do(close_all_stock_positions)

    # Daily summary at 4:05 PM ET
    schedule.every().day.at("16:05").do(send_daily_summary)

    # Run immediately on start
    logger.info("Running initial cycle...")
    run_trading_cycle(scan_stocks=alpaca.is_market_open(), scan_crypto=True)

    logger.info("Scheduler running. Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
