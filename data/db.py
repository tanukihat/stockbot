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
    # Migrate existing DBs — add peak_pnl_pct if it doesn't exist yet
    try:
        conn.execute("ALTER TABLE position_log ADD COLUMN peak_pnl_pct REAL")
        conn.commit()
        logger.info("Migrated position_log: added peak_pnl_pct column")
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


def close_position_db(symbol, exit_price, pnl, pnl_pct):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        UPDATE position_log
        SET close_time = ?, exit_price = ?, pnl = ?, pnl_pct = ?, status = 'closed'
        WHERE symbol = ? AND status = 'open'
    """, (
        datetime.now(timezone.utc).isoformat(),
        exit_price, pnl, pnl_pct, symbol
    ))
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
