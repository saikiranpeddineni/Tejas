"""
db.py — Database Layer
SQLite WAL mode. All tables and migrations here.
"""
import sqlite3
import logging
import threading
from pathlib import Path
from config import Config

log = logging.getLogger(__name__)
_local = threading.local()

_SCHEMA = """
-- Daily/intraday bars cache for feature calculation
CREATE TABLE IF NOT EXISTS price_bars (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL DEFAULT '1Day',
    bar_time    TEXT    NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      INTEGER NOT NULL,
    vwap        REAL    DEFAULT 0,
    recorded_at TEXT    DEFAULT (datetime('now','utc')),
    UNIQUE(symbol, timeframe, bar_time)
);
CREATE INDEX IF NOT EXISTS idx_bars_sym_time ON price_bars (symbol, timeframe, bar_time DESC);

-- ML training data (feature vector + outcome)
CREATE TABLE IF NOT EXISTS training_data (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        TEXT    NOT NULL UNIQUE,
    symbol          TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    features_json   TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    entry_time      TEXT    NOT NULL,
    exit_time       TEXT,
    exit_reason     TEXT,
    pnl_pct         REAL,
    outcome         INTEGER,   -- 1=WIN 0=LOSS NULL=pending
    created_at      TEXT    DEFAULT (datetime('now','utc'))
);
CREATE INDEX IF NOT EXISTS idx_td_outcome ON training_data (outcome);
CREATE INDEX IF NOT EXISTS idx_td_entry   ON training_data (entry_time DESC);

-- Trade ledger (open + closed positions)
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id            TEXT    NOT NULL UNIQUE,
    symbol              TEXT    NOT NULL,
    side                TEXT    NOT NULL,
    shares              REAL    NOT NULL,
    size_usd            REAL    NOT NULL,
    entry_price         REAL    NOT NULL,
    stop_loss           REAL    NOT NULL,
    take_profit         REAL    NOT NULL,
    ml_probability      REAL,
    entry_quality       REAL,
    kelly_pct           REAL,
    alpaca_order_id     TEXT    DEFAULT '',
    sector_code         INTEGER DEFAULT 6,
    status              TEXT    DEFAULT 'open',
    pnl_usd             REAL    DEFAULT 0,
    pnl_pct             REAL    DEFAULT 0,
    entry_time          TEXT    NOT NULL DEFAULT (datetime('now','utc')),
    exit_time           TEXT,
    exit_price          REAL,
    exit_reason         TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol);

-- ML model metrics
CREATE TABLE IF NOT EXISTS model_metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trained_at  TEXT    NOT NULL,
    n_samples   INTEGER,
    train_auc   REAL,
    val_auc     REAL,
    accuracy    REAL,
    log_loss    REAL,
    overfit_flag INTEGER DEFAULT 0,
    top_features TEXT
);

-- Rejected signals log
CREATE TABLE IF NOT EXISTS rejected_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT,
    reason      TEXT,
    edge        REAL,
    quality     REAL,
    rejected_at TEXT DEFAULT (datetime('now','utc'))
);

-- Daily portfolio snapshots (equity curve)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_date   TEXT    NOT NULL UNIQUE,
    bankroll    REAL    NOT NULL,
    open_trades INTEGER DEFAULT 0,
    total_pnl   REAL    DEFAULT 0,
    recorded_at TEXT    DEFAULT (datetime('now','utc'))
);
"""


def init_db() -> None:
    Path(Config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(Config.DB_PATH)
    try:
        conn.executescript(_SCHEMA)
        # ── Migrations (additive only) ────────────────────────────────────────
        # trades.highest_price: persists the trailing-stop high-water mark so a
        # restart doesn't silently disarm profit protection on open positions.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
        if "highest_price" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN highest_price REAL DEFAULT 0")
            log.info("[DB] migrated: trades.highest_price added")
        # Staged exits: shares/price of the first-target trim.
        if "trim_shares" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN trim_shares REAL DEFAULT 0")
            conn.execute("ALTER TABLE trades ADD COLUMN trim_price REAL")
            log.info("[DB] migrated: trades.trim_shares/trim_price added")
        # Per-trade entry rationale (the WHY, shown in Telegram + dashboard).
        if "rationale" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN rationale TEXT DEFAULT ''")
            log.info("[DB] migrated: trades.rationale added")
        conn.commit()
        log.info(f"✓ DB ready: {Config.DB_PATH}")
    finally:
        conn.close()


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(Config.DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-32000")
    return conn


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = _make_conn()
    return _local.conn


def fetchall(sql: str, params=()) -> list:
    conn = _get_conn()
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        conn.rollback()
        raise


def fetchone(sql: str, params=()):
    conn = _get_conn()
    try:
        return conn.execute(sql, params).fetchone()
    except Exception:
        conn.rollback()
        raise


def execute(sql: str, params=()) -> int:
    conn = _get_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    except Exception:
        conn.rollback()
        raise


def executemany(sql: str, params_list) -> int:
    conn = _get_conn()
    try:
        cur = conn.executemany(sql, params_list)
        conn.commit()
        return cur.rowcount
    except Exception:
        conn.rollback()
        raise
