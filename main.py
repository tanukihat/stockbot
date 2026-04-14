"""
StockBot — Autonomous AI trading bot
Main entry point and scheduler.

Runs sentiment analysis + trade execution on a schedule.
Market hours: every 30 min | Crypto (24/7): every 60 min | Daily summary: 4pm ET
"""
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import schedule

import alpaca_client as alpaca
from sentiment.aggregator import aggregate_sentiment
from analysis.claude_analyzer import analyze_sentiment_batch
from trading.portfolio import get_portfolio_state, check_stop_and_take_profit
from trading.executor import execute_signal, buy_options_call
from trading.overnight import get_overnight_eligible
from trading.sentiment_exit import check_sentiment_exits, reset_strikes, is_reentry_allowed
from notifications import telegram
from data.db import (
    init_db, log_trade, open_position, close_position_db,
    log_scan, get_open_position_age, update_trade_filled, get_conn,
    get_position_peak, update_position_peak
)
from config import (
    MIN_POSITIONS, TARGET_OPTIONS_PCT, TARGET_CRYPTO_PCT, TARGET_STOCK_PCT,
    STOP_LOSS_PCT, MIN_SENTIMENT_SCORE, MIN_SENTIMENT_SCORE_URGENT,
    ALL_CRYPTO_SYMBOLS, SCAN_INTERVAL_MINUTES, CRYPTO_SCAN_INTERVAL_MINUTES,
    EOD_CLOSE_STOCKS, EOD_CLOSE_TIME, TARGET_DEPLOYED_PCT,
    SIGNAL_TTL_EQUITY_SECS, SIGNAL_TTL_CRYPTO_SECS, ANTI_PUMP_MAX_MOVE_PCT,
    QUIET_HOURS_START
)
from events_writer import check_milestones, check_trade_win, check_sentiment_events, log_close_event

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        # NOTE: FileHandler removed — systemd captures stdout via
        # StandardOutput=append:stockbot.log, so writing here too caused every
        # line to appear twice. stdout → file is handled by the service unit.
    ],
    force=True,
)
logger = logging.getLogger("stockbot.main")

CRYPTO_SYMBOLS_BASE = [s.split("/")[0] for s in ALL_CRYPTO_SYMBOLS]

# Guard: run startup position audit only once per process
_startup_cleanup_done = False

# Order fill tracking — notify when pending orders execute
_seen_order_ids: set = set()
_order_fill_initialized = False
_last_market_cycle_time: float = 0.0   # unix timestamp of last market cycle start
_last_crypto_cycle_time: float = 0.0   # unix timestamp of last crypto cycle start
MIN_CYCLE_GAP_SECS = 60               # minimum seconds between same-type cycles (catch-up guard)
_last_aggregated: dict = {}            # last sentiment aggregation result — used by EOD overnight check


def handle_existing_positions(state):
    """
    On startup / each cycle: enforce stop-loss and take-profit on existing positions.
    Also closes deeply underwater positions the previous session left open.
    """
    to_close = check_stop_and_take_profit(state)

    # Build set of symbols already being closed (pending sell orders) so we
    # don't spam notifications on every cycle while waiting for market open.
    pending_sells = set()
    pending_sell_orders = {}  # symbol -> order dict
    try:
        open_orders = alpaca.get_open_orders()
        for o in open_orders:
            if o.get("side") == "sell":
                raw = o["symbol"]
                normalized = raw.replace("/USD", "").rstrip("USD") if len(raw) > 3 else raw
                pending_sells.add(raw)
                pending_sells.add(normalized)
                pending_sell_orders[normalized] = o
                pending_sell_orders[raw] = o
    except Exception as e:
        logger.warning(f"Couldn't fetch open orders for dedup check: {e}")

    # --- Cancel-if-recovering: if a pending sell order exists but the position
    # has recovered back above the trailing floor, cancel the order and let it ride.
    from config import TRAILING_ACTIVATE_PCT
    all_pos_map = {p["symbol"]: p for p in state.get("all_positions", [])}
    for sym, order in list(pending_sell_orders.items()):
        if sym not in all_pos_map:
            continue
        pos = all_pos_map[sym]
        current_pct = pos.get("unrealized_plpc", 0)
        peak = get_position_peak(sym)
        # Only cancel if it was a profit-floor order (peak was hit) and it's recovered
        if peak is not None and peak >= TRAILING_ACTIVATE_PCT and current_pct > TRAILING_ACTIVATE_PCT:
            order_id = order.get("id")
            logger.info(f"{sym} recovered to {current_pct*100:+.1f}% — cancelling pending sell order {order_id}")
            try:
                alpaca.cancel_orders_for_symbol(order["symbol"])
                telegram.send(
                    f"↩️ <b>SELL CANCELLED: {sym}</b>\n"
                    f"Position recovered to {current_pct*100:+.1f}% (above floor) — letting it ride.",
                    urgent=True
                )
                pending_sells.discard(sym)
                pending_sells.discard(order["symbol"])
            except Exception as e:
                logger.warning(f"Failed to cancel recovery order for {sym}: {e}")

    for item in to_close:
        sym = item["symbol"]
        reason = item["reason"]
        pnl_pct = item["pnl_pct"]

        # Already has a pending close order — skip entirely, don't re-notify
        alpaca_sym = alpaca.to_alpaca_symbol(sym, item["asset_class"])
        if sym in pending_sells or alpaca_sym in pending_sells:
            logger.info(f"{sym} already has a pending sell order — skipping close/notify")
            continue

        # Find full position data
        all_pos = {p["symbol"]: p for p in state["all_positions"]}
        pos = all_pos.get(sym)
        if not pos:
            continue

        pnl = pos["unrealized_pl"]
        exit_price = pos["current_price"]

        result = alpaca.close_position(alpaca_sym, reason=reason)
        # Only notify on a fresh close (result has order details), not on
        # "already in progress" (result == {} from 403/404/422)
        if result and result.get("id"):
            close_position_db(sym, exit_price, pnl, pnl_pct)
            # Mark this order as seen so check_order_fills() doesn't double-notify
            _seen_order_ids.add(result["id"])
            log_close_event(sym, pnl_pct, pnl, exit_price, reason)
            reset_strikes(sym)  # Clean up sentiment strike state
            is_trailing = "TRAILING" in reason.upper() or "PROFIT-FLOOR" in reason.upper()
            if not alpaca.is_market_open():
                # Order queued for next market open — don't say CLOSED yet
                telegram.send(
                    f"⏳ <b>SELL QUEUED: {sym}</b>\n"
                    f"P&L (unrealized): <b>{'+' if pnl >= 0 else ''}{pnl:.2f} ({pnl_pct*100:+.1f}%)</b>\n"
                    f"Reason: {reason}\n"
                    f"📋 Day order placed — executes at market open.",
                    urgent=True,
                    dedup_key=f"sell_queued:{sym}"
                )
            elif is_trailing:
                telegram.notify_trailing_stop(sym, pnl, pnl_pct, reason)
            elif "STOP" in reason.upper():
                telegram.notify_stop_loss(sym, pnl, pnl_pct)
            else:
                telegram.notify_take_profit(sym, pnl, pnl_pct)
            logger.info(f"Closed {sym}: {reason} | PnL: {pnl_pct*100:+.1f}%")
        elif result is not None:
            logger.info(f"{sym} close already in progress (no order id returned)")

    # Stale position killer removed — sentiment exits + stop/take-profit handle this.
    # The stale killer was ejecting crypto positions that still had good thesis
    # and causing immediate re-buys. Trust the exit logic instead.


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

        # Only register in DB if not already tracked (prevents duplicate rows on restart)
        already_tracked = get_open_position_age(sym) is not None

        if pnl_pct <= -STOP_LOSS_PCT:
            # Past stop-loss threshold — close immediately
            action_taken = f"Closed immediately (inherited position {pnl_pct*100:+.1f}%, past stop-loss)"
            logger.warning(f"Inherited {sym} at {pnl_pct*100:+.1f}% — closing")
            if not already_tracked:
                open_position(sym, pos["asset_class"], pos["avg_entry_price"], pos["market_value"])
            alpaca.close_position(alpaca.to_alpaca_symbol(sym, pos["asset_class"]), reason="Inherited position past stop-loss")
            close_position_db(sym, pos["current_price"], pos["unrealized_pl"], pnl_pct)
            telegram.notify_position_inherited(sym, pnl_pct, action_taken)
        else:
            action_taken = f"Monitoring (P&L: {pnl_pct*100:+.1f}%)"
            telegram.notify_position_inherited(sym, pnl_pct, action_taken)
            # Register in DB so age tracking works — but only if not already there
            if not already_tracked:
                open_position(sym, pos["asset_class"], pos["avg_entry_price"], pos["market_value"])

    _startup_cleanup_done = True


def _run_sentiment_scan(scan_stocks, scan_crypto):
    """Scrape and analyze sentiment. Returns (aggregated, signals) or (None, []) on failure."""
    global _last_aggregated
    aggregated = aggregate_sentiment(scan_crypto=scan_crypto, scan_stocks=scan_stocks)
    if not aggregated:
        logger.info("No sentiment signals found this cycle")
        return None, []
    _last_aggregated = aggregated
    check_sentiment_events(aggregated)
    signals = analyze_sentiment_batch(aggregated)
    return aggregated, signals


def _run_sentiment_exits(held_stocks, signals, aggregated, state):
    """Check held positions for sentiment-based exits. Returns number of positions closed."""
    sentiment_exits = check_sentiment_exits(held_stocks, signals, aggregated)
    pos_map = {p["symbol"]: p for p in state["stock_positions"]}
    for exit_item in sentiment_exits:
        sym, reason = exit_item["symbol"], exit_item["reason"]
        pos = pos_map.get(sym)
        if not pos:
            continue
        pnl, pnl_pct = pos["unrealized_pl"], pos["unrealized_plpc"]
        logger.info(f"Sentiment exit triggered: {sym} | {reason} | PnL {pnl_pct*100:+.1f}%")
        result = alpaca.close_position(alpaca.to_alpaca_symbol(sym, "us_equity"), reason=reason)
        if result is not None:
            close_position_db(sym, pos["current_price"], pnl, pnl_pct)
            if result.get("id"):
                _seen_order_ids.add(result["id"])
            log_close_event(sym, pnl_pct, pnl, pos["current_price"], reason)
            telegram.notify_trade_closed(sym, "SELL", pnl, pnl_pct, reason, pos["current_price"])
            reset_strikes(sym, sentiment_exit=True)
    return len(sentiment_exits)


def _is_signal_skippable(signal, state, market_open, under_min, under_deployed):
    """
    Returns a skip reason string if this signal should be skipped, else None.
    Checks: market hours, EOD window, TTL, anti-pump, confidence, allocation.
    """
    sym = signal["symbol"]
    asset_class = signal.get("asset_class", "us_equity")

    if asset_class == "us_equity" and not is_reentry_allowed(sym):
        return f"{sym} in sentiment-exit cooldown"

    if asset_class == "us_equity" and not market_open:
        return f"{sym} — market closed"

    if asset_class == "us_equity" and EOD_CLOSE_STOCKS:
        now_et = datetime.now(ZoneInfo("America/New_York"))
        eod_h, eod_m = map(int, EOD_CLOSE_TIME.split(":"))
        minutes_to_eod = (eod_h * 60 + eod_m) - (now_et.hour * 60 + now_et.minute)
        if minutes_to_eod < 45:
            return f"{sym} — only {minutes_to_eod}min to EOD flatten"

    ttl = SIGNAL_TTL_CRYPTO_SECS if asset_class == "crypto" else SIGNAL_TTL_EQUITY_SECS
    signal_age = time.time() - signal.get("generated_at", time.time())
    if signal_age > ttl:
        return f"{sym} — signal expired ({signal_age/60:.0f}min old, TTL={ttl//60}min)"

    try:
        open_price = alpaca.get_intraday_open_price(sym, asset_class)
        alpaca_sym = sym if asset_class != "crypto" else (f"{sym}/USD" if "/" not in sym else sym)
        current_price = alpaca.get_latest_price(alpaca_sym, asset_class)
        if open_price and current_price:
            intraday_move = (current_price - open_price) / open_price
            if intraday_move > ANTI_PUMP_MAX_MOVE_PCT:
                return f"{sym} — already up {intraday_move*100:.1f}% intraday (anti-pump)"
    except Exception as e:
        logger.warning(f"Anti-pump check failed for {sym}: {e}")

    urgency = signal.get("urgency", "LOW")
    threshold = MIN_SENTIMENT_SCORE_URGENT if (under_min or under_deployed or urgency == "HIGH") else MIN_SENTIMENT_SCORE
    if signal.get("confidence", 0) < threshold:
        return f"{sym} — confidence {signal.get('confidence', 0):.2f} below threshold {threshold}"

    if asset_class == "crypto" and state["crypto_pct"] >= TARGET_CRYPTO_PCT + 0.10:
        return f"{sym} — crypto allocation at target"
    if asset_class == "us_equity" and state["stock_pct"] >= TARGET_STOCK_PCT + 0.10:
        return f"{sym} — stock allocation at target"

    return None


def _execute_signals(signals, held_all, state, under_min, under_deployed):
    """Execute BUY signals in priority order. Returns number of trades executed."""
    # Filter, deduplicate, and sort
    signals = [s for s in signals if s["symbol"] not in held_all]
    seen_syms = {}
    for s in signals:
        sym = s["symbol"]
        if sym not in seen_syms or s.get("confidence", 0) > seen_syms[sym].get("confidence", 0):
            seen_syms[sym] = s
    signals = sorted(seen_syms.values(), key=lambda x: (
        0 if x.get("urgency") == "HIGH" else 1,
        -x.get("confidence", 0)
    ))

    for s in signals:
        logger.info(
            f"Signal: {s['action']} {s['symbol']} "
            f"conf={s.get('confidence', 0):.2f} urgency={s.get('urgency', '?')} "
            f"wsb={s.get('wsb_signal', False)} — {s.get('reasoning', '')[:80]}"
        )

    trades_executed = 0
    slots_used_this_cycle = 0
    market_open = alpaca.is_market_open()

    for signal in signals:
        if signal["action"] != "BUY":
            continue

        current_state = get_portfolio_state()
        if current_state["open_slots"] - slots_used_this_cycle <= 0:
            logger.info("Position slots full mid-cycle, stopping execution")
            break

        skip_reason = _is_signal_skippable(signal, current_state, market_open, under_min, under_deployed)
        if skip_reason:
            logger.info(f"Skipping: {skip_reason}")
            continue

        result = execute_signal(signal, current_state)
        sym = signal["symbol"]
        asset_class = signal.get("asset_class", "us_equity")

        if result and result.get("action") == "BUY":
            trades_executed += 1
            slots_used_this_cycle += 1
            log_trade(result)
            open_position(sym, asset_class, result.get("entry_price", 0), result.get("notional", 0))
            telegram.notify_trade_opened(result)
            logger.info(f"Executed BUY: {sym} (confidence={signal.get('confidence', 0):.2f})")
        elif result and "FAILED" in result.get("action", ""):
            telegram.notify_error(f"Trade failed: {sym} — {result.get('error', '')}")

    return trades_executed, signals


def _try_options(signals, trades_executed):
    """If options slot available and we traded this cycle, try buying calls on top signal."""
    current_state = get_portfolio_state()
    if not (current_state["open_slots"] > 0
            and current_state["options_pct"] < TARGET_OPTIONS_PCT - 0.05
            and trades_executed > 0):
        return 0
    stock_signals = [
        s for s in signals
        if s.get("asset_class") == "us_equity" and s["action"] == "BUY" and s.get("confidence", 0) >= 0.75
    ]
    if not stock_signals:
        return 0
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
        return 1
    return 0


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
        check_order_fills()

        # Position management
        state = get_portfolio_state()
        handle_existing_positions(state)
        state = get_portfolio_state()
        check_milestones(state["portfolio_value"])

        n_positions = state["total_positions"]
        deployed_pct = 1 - (state["cash"] / state["portfolio_value"])
        under_min = n_positions < MIN_POSITIONS
        under_deployed = deployed_pct < TARGET_DEPLOYED_PCT - 0.10
        held_stocks = {p["symbol"] for p in state["stock_positions"]}
        held_all = {p["symbol"] for p in state["all_positions"]}
        seeking_new_trades = (state["open_slots"] > 0) and (under_min or under_deployed)

        if state["open_slots"] <= 0 and not held_stocks:
            logger.info("All position slots full, no held stocks — skipping sentiment scan")
            log_scan(0, 0, 0, "All slots full")
            return

        logger.info(
            f"Positions: {n_positions}{f' (min={MIN_POSITIONS})' if under_min else ''} | "
            f"Deployed: {deployed_pct:.0%} — {'seeking trades' if seeking_new_trades else 'adequately capitalized'}"
        )

        # Sentiment scan
        aggregated, signals = _run_sentiment_scan(scan_stocks, scan_crypto)
        if not aggregated:
            log_scan(0, 0, 0, "No sentiment data")
            return

        # Sentiment exits for held positions
        exits = _run_sentiment_exits(held_stocks, signals, aggregated, state)
        if exits:
            state = get_portfolio_state()

        if not seeking_new_trades:
            log_scan(len(aggregated), 0, 0, "Adequately deployed — sentiment exits only")
            return

        # Execute new signals
        trades_executed, signals = _execute_signals(signals, held_all, state, under_min, under_deployed)
        trades_executed += _try_options(signals, trades_executed)

        log_scan(len(aggregated), len(signals), trades_executed)
        telegram.notify_scan_complete(len(aggregated), signals[:5], trades_executed)
        logger.info(f"=== Cycle complete in {time.time() - start:.1f}s | {trades_executed} trades executed ===")

    except Exception as e:
        logger.error(f"Trading cycle error: {e}", exc_info=True)
        telegram.notify_error(f"Cycle error: {str(e)[:200]}")


def run_market_cycle():
    """Run during US market hours (stocks + crypto)."""
    global _last_market_cycle_time
    now = time.time()
    if now - _last_market_cycle_time < MIN_CYCLE_GAP_SECS:
        logger.info(f"Market cycle skipped — last ran {now - _last_market_cycle_time:.0f}s ago (catch-up guard)")
        return
    _last_market_cycle_time = now
    if alpaca.is_market_open():
        run_trading_cycle(scan_stocks=True, scan_crypto=True)
    else:
        logger.info("Market closed — skipping stock scan")


def run_crypto_cycle():
    """Run crypto-only scan (24/7, but throttled overnight and on weekends)."""
    global _last_crypto_cycle_time
    now = time.time()
    if now - _last_crypto_cycle_time < MIN_CYCLE_GAP_SECS:
        logger.info(f"Crypto cycle skipped — last ran {now - _last_crypto_cycle_time:.0f}s ago (catch-up guard)")
        return
    _last_crypto_cycle_time = now
    now_et = datetime.now(ZoneInfo("America/New_York"))
    is_weekend = now_et.weekday() >= 5  # Saturday=5, Sunday=6

    if is_weekend:
        # Weekends: only run every 4 hours
        if now_et.hour % 4 != 0:
            logger.debug("Weekend crypto throttle: skipping non-4h slot")
            return
    else:
        # Weekday quiet hours (11pm–7am ET): only run every 2 hours
        if QUIET_HOURS_START <= now_et.hour or now_et.hour < 7:
            if now_et.hour % 2 != 0:
                logger.debug("Overnight weekday crypto throttle: skipping non-2h slot")
                return

    if not alpaca.is_market_open():
        run_trading_cycle(scan_stocks=False, scan_crypto=True)


def close_all_stock_positions():
    """
    EOD flatten — close all stock positions by EOD_CLOSE_TIME.
    Crypto stays open 24/7. This is day trader mode.
    """
    if not EOD_CLOSE_STOCKS:
        return
    # Never flatten on weekends — market is closed
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:  # 5=Saturday, 6=Sunday
        logger.info(f"EOD close: skipping — it's the weekend ({now_et.strftime('%A')})")
        return
    # Guard against duplicate runs when multiple processes briefly overlap during restarts
    today_str = now_et.strftime("%Y%m%d")
    lock_path = os.path.join(tempfile.gettempdir(), f"stockbot_eod_close_{today_str}.lock")
    if os.path.exists(lock_path):
        logger.warning("EOD close already ran today (lock file exists) — skipping duplicate")
        return
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        logger.warning("EOD close lock race lost — another process won, skipping")
        return
    # Cancel all open/held orders before flattening — bracket legs hold shares and cause 403s.
    # We wait after cancellation to give Alpaca time to release the held shares.
    try:
        alpaca.cancel_all_orders()
        logger.info("EOD close: cancelled all open orders")
        time.sleep(4.0)  # Wait for Alpaca to release shares locked by bracket legs
    except Exception as e:
        logger.warning(f"EOD close: failed to cancel orders: {e}")

    state = get_portfolio_state()
    stock_pos = state["stock_positions"]
    if not stock_pos:
        logger.info("EOD close: no stock positions to flatten")
        return
    # Determine which positions qualify to hold overnight
    # Pass last known aggregated data so earnings flags are respected
    overnight_holds = get_overnight_eligible(stock_pos, aggregated=_last_aggregated)

    positions_to_close = [p for p in stock_pos if p["symbol"] not in overnight_holds]
    logger.info(f"EOD close: flattening {len(positions_to_close)} position(s), holding {len(overnight_holds)} overnight")

    if overnight_holds:
        held_summary = ", ".join(
            f"{p['symbol']} ({p['unrealized_plpc']*100:+.1f}%)"
            for p in stock_pos if p["symbol"] in overnight_holds
        )
        telegram.send(f"🌙 Overnight holds: {held_summary}", urgent=True)

    for pos in positions_to_close:
        sym = pos["symbol"]
        pnl = pos["unrealized_pl"]
        pnl_pct = pos["unrealized_plpc"]
        result = alpaca.close_position(
            alpaca.to_alpaca_symbol(sym, "us_equity"), reason="EOD flatten"
        )
        if result is not None:
            close_position_db(sym, pos["current_price"], pnl, pnl_pct)
            if result.get("id"):
                _seen_order_ids.add(result["id"])
            log_close_event(sym, pnl_pct, pnl, pos["current_price"], "EOD flatten")
            telegram.notify_trade_closed(sym, "SELL", pnl, pnl_pct, "EOD flatten", pos["current_price"])
            logger.info(f"EOD closed {sym}: PnL {pnl_pct*100:+.1f}%")


def check_order_fills():
    """
    Detect when pending orders fill and send a notification.
    Runs each cycle. On first call, seeds the seen-set without notifying.
    """
    global _seen_order_ids, _order_fill_initialized
    try:
        # Fetch orders closed in the last 24h
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        filled = alpaca.get_filled_orders(since=since)
        if not _order_fill_initialized:
            # First run — seed the set so we don't spam history on startup
            _seen_order_ids = {o["id"] for o in filled}
            _order_fill_initialized = True
            logger.info(f"Order fill tracker seeded with {len(_seen_order_ids)} existing fills")
            return

        for order in filled:
            oid = order["id"]
            if oid in _seen_order_ids:
                continue
            _seen_order_ids.add(oid)

            sym = order["symbol"].replace("/USD", "")
            side = order["side"]
            qty = float(order.get("filled_qty") or order.get("qty") or 0)
            avg_price = float(order.get("filled_avg_price") or 0)

            # Skip ghost orders (cancelled bracket legs, unfilled, etc.)
            if qty == 0 or avg_price == 0:
                logger.debug(f"Skipping zero-qty/price order {oid} ({sym})")
                continue

            # Skip bracket child legs (TP/SL) — they're not user-facing fills.
            # The parent bracket BUY is the actionable notification; legs are internal.
            # order_class is '' or 'simple' for standalone orders; 'bracket'/'oto'/'oco' for parents.
            # Child legs have no order_class but have a non-null parent_order_id.
            if order.get("legs") is None and order.get("order_class") == "" and order.get("type") in ("limit", "stop"):
                # Likely a bracket child leg — skip notification but keep in seen set
                logger.debug(f"Skipping bracket child leg {oid} ({sym} {side} {order.get('type')})")
                continue

            # Try to get P&L from DB for sells
            pnl, pnl_pct = None, None
            if side == "sell":
                conn = get_conn()
                row = conn.execute(
                    "SELECT entry_price, notional FROM position_log WHERE symbol=? ORDER BY open_time DESC LIMIT 1",
                    (sym,)
                ).fetchone()
                conn.close()
                if row and row["entry_price"] and avg_price:
                    pnl_pct = (avg_price / row["entry_price"]) - 1
                    pnl = pnl_pct * float(row["notional"] or 0)
                # Mark closed in DB
                close_position_db(sym, avg_price, pnl or 0, pnl_pct or 0)

            logger.info(f"Order filled: {side.upper()} {sym} qty={qty} @ ${avg_price:.2f}")
            # Update trades table: mark BUY as filled, or close it on sell
            if side == "buy":
                update_trade_filled(oid, avg_price)
            telegram.notify_order_filled(sym, side, qty, avg_price, pnl, pnl_pct)

            # Log close reason + big wins to the dashboard
            if side == "sell" and pnl_pct is not None and pnl is not None:
                # Infer reason from P&L since bracket orders don't carry explicit reason
                if pnl_pct <= -0.045:
                    inferred_reason = "Stop-loss triggered"
                elif pnl_pct >= 0.075:
                    inferred_reason = "Take-profit triggered"
                else:
                    inferred_reason = "Bracket order filled"
                log_close_event(sym, pnl_pct, pnl, avg_price, inferred_reason)
                check_trade_win(sym, pnl_pct, pnl, avg_price)

    except Exception as e:
        logger.warning(f"Order fill check failed: {e}")


def send_morning_digest():
    """
    9:00 AM ET — scrape sentiment and send a pre-market briefing with today's plan.
    On weekends, skip the Claude sentiment call and just send portfolio state.
    """
    logger.info("Generating morning digest...")
    try:
        state = get_portfolio_state()
        pv = state["portfolio_value"]
        cash = state["cash"]
        positions = state["all_positions"]

        # On weekends skip the expensive sentiment scrape — market is closed anyway
        now_et = datetime.now(ZoneInfo("America/New_York"))
        is_weekend = now_et.weekday() >= 5
        if is_weekend:
            pos_str = "\n".join(
                f"  • {p['symbol']}: ${p['market_value']:,.0f} ({p['unrealized_plpc']*100:+.1f}%)"
                for p in positions
            ) or "  (none)"
            msg = (
                f"☀️ <b>Weekend Update — {now_et.strftime('%b %d')}</b>\n"
                f"Market closed (reopens Mon 9:30 AM)\n\n"
                f"💼 Portfolio: <b>${pv:,.2f}</b>  |  💵 Cash: ${cash:,.2f}\n"
                f"📦 Positions:\n{pos_str}"
            )
            telegram.send(msg)
            logger.info("Weekend digest sent (no sentiment scan)")
            return

        # Scrape fresh sentiment (weekdays only)
        aggregated = aggregate_sentiment(scan_crypto=True, scan_stocks=True)
        signals = analyze_sentiment_batch(aggregated) if aggregated else []

        pos_str = "\n".join(
            f"  • {p['symbol']}: ${p['market_value']:,.0f} ({p['unrealized_plpc']*100:+.1f}%)"
            for p in positions
        ) or "  (none)"

        buy_signals = [s for s in signals if s["action"] == "BUY"]
        watch_str = ""
        for s in buy_signals[:5]:
            watch_str += (
                f"  🎯 <b>{s['symbol']}</b> — {s.get('reasoning', '')}\n"
                f"     Confidence: {s.get('confidence', 0)*100:.0f}% | Urgency: {s.get('urgency','?')}\n"
            )
        if not watch_str:
            watch_str = "  Nothing high-conviction yet — waiting for open\n"

        open_slots = state["open_slots"]
        msg = (
            f"☀️ <b>Morning Digest — {datetime.now().strftime('%b %d')}</b>\n"
            f"Market opens in ~30 min\n\n"
            f"💼 Portfolio: <b>${pv:,.2f}</b>  |  💵 Cash: ${cash:,.2f}\n"
            f"📦 Positions ({len(positions)}/{5 - open_slots + len(positions)}):\n{pos_str}\n\n"
            f"🔭 <b>Watchlist for today:</b>\n{watch_str}\n"
            f"🎰 Open slots: {open_slots}"
        )
        telegram.send(msg, urgent=True)
        logger.info("Morning digest sent")
    except Exception as e:
        logger.error(f"Morning digest failed: {e}")
        telegram.notify_error(f"Morning digest failed: {e}")


HOPE_QUOTES = [
    "\"Hope is being able to see that there is light despite all of the darkness.\" — Desmond Tutu",
    "\"Once you choose hope, anything's possible.\" — Christopher Reeve",
    "\"Hope is the thing with feathers that perches in the soul.\" — Emily Dickinson",
    "\"In the middle of winter, I at last discovered that there was in me an invincible summer.\" — Albert Camus",
    "\"Hope is a waking dream.\" — Aristotle",
    "\"Everything that is done in this world is done by hope.\" — Martin Luther",
    "\"Hope itself is a species of happiness, and, perhaps, the chief happiness which this world affords.\" — Samuel Johnson",
    "\"We must accept finite disappointment, but never lose infinite hope.\" — Martin Luther King Jr.",
    "\"Hope is the power of being cheerful in circumstances that we know to be desperate.\" — G.K. Chesterton",
    "\"Where there is no hope, it is incumbent on us to invent it.\" — Albert Camus",
    "\"The very least you can do in your life is figure out what you hope for.\" — Barbara Kingsolver",
    "\"Hope is not a dream but a way of making dreams become reality.\" — L.J. Suenens",
    "\"Optimism is the faith that leads to achievement. Nothing can be done without hope and confidence.\" — Helen Keller",
    "\"Keep your face always toward the sunshine, and shadows will fall behind you.\" — Walt Whitman",
    "\"There is some good in this world, and it's worth fighting for.\" — J.R.R. Tolkien",
]

def send_daily_summary():
    """4:05 PM ET — EOD summary with full day recap.
    Guard against duplicate sends when multiple processes are briefly alive during restarts.
    """
    today_str = datetime.now().strftime("%Y%m%d")
    lock_path = os.path.join(tempfile.gettempdir(), f"stockbot_eod_summary_{today_str}.lock")
    if os.path.exists(lock_path):
        logger.warning("EOD summary already sent today (lock file exists) — skipping duplicate")
        return
    try:
        # Atomic create — only one process wins the race
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        logger.warning("EOD summary lock race lost — another process won, skipping")
        return
    today_seed = int(today_str)
    quote = HOPE_QUOTES[today_seed % len(HOPE_QUOTES)]
    state = get_portfolio_state()
    account = alpaca.get_account()

    pv = state["portfolio_value"]
    last_equity = float(account.get("last_equity") or pv)
    day_pnl = pv - last_equity
    day_pnl_pct = (day_pnl / last_equity * 100) if last_equity else 0

    positions = state["all_positions"]
    pos_str = "\n".join(
        f"  • {p['symbol']}: ${p['market_value']:,.0f} ({p['unrealized_plpc']*100:+.1f}%)"
        for p in positions
    ) or "  (none — fully flat)"

    pnl_emoji = "📈" if day_pnl >= 0 else "📉"
    msg = (
        f"🌙 <b>End of Day — {datetime.now().strftime('%b %d')}</b>\n\n"
        f"💼 Portfolio: <b>${pv:,.2f}</b>\n"
        f"{pnl_emoji} Day P&L: <b>{'+' if day_pnl >= 0 else ''}${day_pnl:.2f} ({day_pnl_pct:+.2f}%)</b>\n\n"
        f"📦 Overnight holds:\n{pos_str}\n\n"
        f"✨ {quote}"
    )
    telegram.send(msg, urgent=True)
    logger.info("EOD summary sent")


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

    # Morning digest at 9:00 AM ET (30 min before open) — weekdays only
    schedule.every().monday.at("09:00").do(send_morning_digest)
    schedule.every().tuesday.at("09:00").do(send_morning_digest)
    schedule.every().wednesday.at("09:00").do(send_morning_digest)
    schedule.every().thursday.at("09:00").do(send_morning_digest)
    schedule.every().friday.at("09:00").do(send_morning_digest)

    # Sunday night pre-market prep scan at 8:00 PM ET
    schedule.every().sunday.at("20:00").do(send_morning_digest)

    # EOD flatten: close all stock positions before close
    schedule.every().day.at(EOD_CLOSE_TIME).do(close_all_stock_positions)

    # EOD summary at 4:05 PM ET — weekdays only
    schedule.every().monday.at("16:05").do(send_daily_summary)
    schedule.every().tuesday.at("16:05").do(send_daily_summary)
    schedule.every().wednesday.at("16:05").do(send_daily_summary)
    schedule.every().thursday.at("16:05").do(send_daily_summary)
    schedule.every().friday.at("16:05").do(send_daily_summary)

    # Run immediately on start
    logger.info("Running initial cycle...")
    run_trading_cycle(scan_stocks=alpaca.is_market_open(), scan_crypto=True)

    logger.info("Scheduler running. Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
