"""
SQLite database for trade logging and position tracking.
"""
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "stockbot.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_conn()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            action TEXT NOT NULL,
            notional REAL,
            entry_price REAL,
            exit_price REAL,
            take_profit REAL,
            stop_loss REAL,
            pnl REAL,
            pnl_pct REAL,
            confidence REAL,
            reasoning TEXT,
            order_id TEXT,
            status TEXT
        );

        CREATE TABLE IF NOT EXISTS position_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            open_time TEXT NOT NULL,
            close_time TEXT,
            entry_price REAL,
            exit_price REAL,
            notional REAL,
            pnl REAL,
            pnl_pct REAL,
            peak_pnl_pct REAL,
            status TEXT DEFAULT 'open'
        );

        CREATE TABLE IF NOT EXISTS scan_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbols_scanned INTEGER,
            signals_generated INTEGER,
            trades_executed INTEGER,
            notes TEXT
        );
    """)
    # Migrate existing DBs — add columns if they don't exist yet
    for col, typedef in [("peak_pnl_pct", "REAL"), ("ics", "REAL"), ("cramer_action", "TEXT"), ("cramer_sentiment", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE position_log ADD COLUMN {col} {typedef}")
            conn.commit()
            logger.info(f"Migrated position_log: added {col} column")
        except Exception:
            pass  # Column already exists, fine

    conn.commit()
    conn.close()
    logger.info(f"Database initialized at {DB_PATH}")


def log_trade(trade: dict):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades (
            timestamp, symbol, asset_class, action, notional, entry_price,
            exit_price, take_profit, stop_loss, pnl, pnl_pct,
            confidence, reasoning, order_id, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        trade.get("symbol"), trade.get("asset_class", "us_equity"),
        trade.get("action"), trade.get("notional"), trade.get("entry_price"),
        trade.get("exit_price"), trade.get("take_profit"), trade.get("stop_loss"),
        trade.get("pnl"), trade.get("pnl_pct"),
        trade.get("confidence"), trade.get("reasoning"),
        trade.get("order_id"), trade.get("status"),
    ))
    conn.commit()
    conn.close()


def open_position(symbol, asset_class, entry_price, notional):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO position_log (symbol, asset_class, open_time, entry_price, notional, status)
        VALUES (?, ?, ?, ?, ?, 'open')
    """, (symbol, asset_class, datetime.now(timezone.utc).isoformat(), entry_price, notional))
    conn.commit()
    conn.close()


def store_ics(symbol, ics_data: dict):
    """Store ICS result against the most recent open position for this symbol."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        UPDATE position_log
        SET ics = ?, cramer_action = ?, cramer_sentiment = ?
        WHERE id = (
            SELECT id FROM position_log
            WHERE symbol = ? AND status = 'open'
            ORDER BY open_time DESC LIMIT 1
        )
    """, (
        ics_data.get("ics"),
        ics_data.get("cramer_action"),
        ics_data.get("cramer_sentiment"),
        symbol,
    ))
    conn.commit()
    conn.close()


def get_ics_history(limit=100):
    """Return closed positions that have ICS data, for correlation charting."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT symbol, close_time, pnl_pct, ics, cramer_action, cramer_sentiment
        FROM position_log
        WHERE status = 'closed' AND ics IS NOT NULL
        ORDER BY close_time DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def close_position_db(symbol, exit_price, pnl, pnl_pct):
    conn = get_conn()
    c = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    # Target only the most recent open position row to avoid nuking re-entries
    c.execute("""
        UPDATE position_log
        SET close_time = ?, exit_price = ?, pnl = ?, pnl_pct = ?, status = 'closed'
        WHERE id = (
            SELECT id FROM position_log
            WHERE symbol = ? AND status = 'open'
            ORDER BY open_time DESC LIMIT 1
        )
    """, (now, exit_price, pnl, pnl_pct, symbol))
    # Also close out the matching BUY row in trades (most recent only)
    c.execute("""
        UPDATE trades
        SET exit_price = ?, pnl = ?, pnl_pct = ?, status = 'closed'
        WHERE id = (
            SELECT id FROM trades
            WHERE symbol = ? AND action = 'BUY' AND status IN ('pending_new', 'filled')
            ORDER BY timestamp DESC LIMIT 1
        )
    """, (exit_price, pnl, pnl_pct, symbol))
    conn.commit()
    conn.close()


def update_trade_filled(order_id: str, filled_price: float):
    """Mark a BUY trade as filled once Alpaca confirms execution."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        UPDATE trades
        SET status = 'filled', entry_price = ?
        WHERE order_id = ? AND action = 'BUY'
    """, (filled_price, order_id))
    conn.commit()
    conn.close()


def get_position_peak(symbol):
    """Returns the recorded peak P&L fraction for an open position, or None."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT peak_pnl_pct FROM position_log
        WHERE symbol = ? AND status = 'open'
        ORDER BY open_time DESC LIMIT 1
    """, (symbol,))
    row = c.fetchone()
    conn.close()
    return row["peak_pnl_pct"] if row else None


def update_position_peak(symbol, pnl_pct):
    """Update peak P&L for an open position if the new value is higher."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        UPDATE position_log
        SET peak_pnl_pct = MAX(COALESCE(peak_pnl_pct, -999), ?)
        WHERE symbol = ? AND status = 'open'
    """, (pnl_pct, symbol))
    conn.commit()
    conn.close()


def get_open_position_age(symbol):
    """Returns hours since position was opened, or None."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT open_time FROM position_log
        WHERE symbol = ? AND status = 'open'
        ORDER BY open_time DESC LIMIT 1
    """, (symbol,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    open_time = datetime.fromisoformat(row["open_time"])
    now = datetime.now(timezone.utc)
    if open_time.tzinfo is None:
        open_time = open_time.replace(tzinfo=timezone.utc)
    return (now - open_time).total_seconds() / 3600


def log_scan(symbols_scanned, signals_generated, trades_executed, notes=""):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO scan_log (timestamp, symbols_scanned, signals_generated, trades_executed, notes)
        VALUES (?, ?, ?, ?, ?)
    """, (datetime.now(timezone.utc).isoformat(), symbols_scanned, signals_generated, trades_executed, notes))
    conn.commit()
    conn.close()


def get_daily_summary():
    """Get today's trade summary."""
    conn = get_conn()
    c = conn.cursor()
    today = datetime.now(timezone.utc).date().isoformat()
    c.execute("""
        SELECT
            COUNT(*) as total_trades,
            SUM(CASE WHEN action = 'BUY' THEN 1 ELSE 0 END) as buys,
            SUM(CASE WHEN action = 'SELL' THEN 1 ELSE 0 END) as sells,
            SUM(COALESCE(pnl, 0)) as total_pnl,
            AVG(CASE WHEN pnl IS NOT NULL THEN pnl_pct ELSE NULL END) as avg_pnl_pct
        FROM trades
        WHERE timestamp LIKE ?
    """, (f"{today}%",))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else {}


def get_recent_trades(limit=10):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, symbol, action, entry_price, pnl, pnl_pct, reasoning
        FROM trades ORDER BY timestamp DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]
