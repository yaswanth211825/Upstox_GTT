"""
db.py — SQLite database layer for GTT Signal Tracking
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gtt_signals.db")

DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS signals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    redis_message_id  TEXT UNIQUE NOT NULL,
    signal_summary    TEXT NOT NULL,
    action            TEXT NOT NULL,
    underlying        TEXT NOT NULL,
    strike            INTEGER,
    option_type       TEXT,
    expiry            TEXT,
    product           TEXT,
    quantity          INTEGER,
    instrument_key    TEXT,
    entry_low         REAL NOT NULL,
    entry_high        REAL,
    stoploss          REAL NOT NULL,
    target1           REAL,
    target2           REAL,
    target3           REAL,
    status            TEXT NOT NULL DEFAULT 'PENDING',
    gtt_order_ids     TEXT,
    gtt_place_price   REAL,
    entry_price       REAL,
    exit_price        REAL,
    pnl               REAL,
    upstox_gtt_status TEXT,
    signal_at         TEXT NOT NULL,
    gtt_placed_at     TEXT,
    activated_at      TEXT,
    resolved_at       TEXT,
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_instrument_key ON signals (instrument_key);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals (status);

CREATE TABLE IF NOT EXISTS price_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   INTEGER NOT NULL REFERENCES signals(id),
    event_type  TEXT NOT NULL,
    price       REAL NOT NULL,
    threshold   REAL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_price_events_signal_id ON price_events (signal_id);

CREATE TABLE IF NOT EXISTS gtt_status_checks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     INTEGER NOT NULL REFERENCES signals(id),
    gtt_order_id  TEXT NOT NULL,
    upstox_status TEXT,
    checked_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gtt_status_signal_id ON gtt_status_checks (signal_id);
"""


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


_MIGRATIONS = [
    "ALTER TABLE signals ADD COLUMN entry_price REAL",
    "ALTER TABLE signals ADD COLUMN exit_price REAL",
    "ALTER TABLE signals ADD COLUMN pnl REAL",
    "ALTER TABLE signals ADD COLUMN upstox_gtt_status TEXT",
]


def _run_migrations(con: sqlite3.Connection):
    for stmt in _MIGRATIONS:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists


def init_db():
    with _conn() as con:
        con.executescript(DDL)
        _run_migrations(con)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_signal(
    redis_message_id: str,
    signal: dict,
    instrument_key: str,
    gtt_ids: list,
    status: str,
    notes: str = None,
    ltp_at_placement: float = None,
) -> int:
    targets = signal.get("targets", [])
    row = {
        "redis_message_id": redis_message_id,
        "signal_summary": signal.get("signal_summary", ""),
        "action": signal.get("action", "BUY"),
        "underlying": signal.get("underlying", signal.get("instrument", "")),
        "strike": signal.get("strike"),
        "option_type": signal.get("option_type"),
        "expiry": signal.get("expiry"),
        "product": signal.get("product", "I"),
        "quantity": signal.get("quantity"),
        "instrument_key": instrument_key,
        "entry_low": float(signal.get("entry_low", 0)),
        "entry_high": float(signal.get("entry_high")) if signal.get("entry_high") else None,
        "stoploss": float(signal.get("stoploss", 0)),
        "target1": float(targets[0]) if len(targets) > 0 else None,
        "target2": float(targets[1]) if len(targets) > 1 else None,
        "target3": float(targets[2]) if len(targets) > 2 else None,
        "status": status,
        "gtt_order_ids": json.dumps(gtt_ids) if gtt_ids else None,
        "gtt_place_price": ltp_at_placement,
        "signal_at": _now_iso(),
        "gtt_placed_at": _now_iso() if status == "PENDING" else None,
        "notes": notes,
    }
    with _conn() as con:
        cur = con.execute(
            """
            INSERT OR IGNORE INTO signals
                (redis_message_id, signal_summary, action, underlying, strike,
                 option_type, expiry, product, quantity, instrument_key,
                 entry_low, entry_high, stoploss, target1, target2, target3,
                 status, gtt_order_ids, gtt_place_price, signal_at, gtt_placed_at, notes)
            VALUES
                (:redis_message_id, :signal_summary, :action, :underlying, :strike,
                 :option_type, :expiry, :product, :quantity, :instrument_key,
                 :entry_low, :entry_high, :stoploss, :target1, :target2, :target3,
                 :status, :gtt_order_ids, :gtt_place_price, :signal_at, :gtt_placed_at, :notes)
            """,
            row,
        )
        return cur.lastrowid


def update_signal_status(signal_id: int, new_status: str, notes: str = None):
    now = _now_iso()
    terminal = {"TARGET1_HIT", "STOPLOSS_HIT", "EXPIRED", "FAILED"}
    with _conn() as con:
        if new_status in terminal:
            con.execute(
                "UPDATE signals SET status=?, resolved_at=?, notes=COALESCE(?,notes) WHERE id=?",
                (new_status, now, notes, signal_id),
            )
        else:
            con.execute(
                "UPDATE signals SET status=?, notes=COALESCE(?,notes) WHERE id=?",
                (new_status, notes, signal_id),
            )


def update_signal_activated(signal_id: int):
    con = _conn()
    with con:
        con.execute(
            "UPDATE signals SET status='ACTIVE', activated_at=? WHERE id=?",
            (_now_iso(), signal_id),
        )


def get_watchable_signals() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM signals WHERE status IN ('PENDING','ACTIVE')"
        ).fetchall()
    return [dict(r) for r in rows]


def insert_price_event(signal_id: int, event_type: str, price: float, threshold: float = None):
    with _conn() as con:
        con.execute(
            "INSERT INTO price_events (signal_id, event_type, price, threshold, recorded_at) VALUES (?,?,?,?,?)",
            (signal_id, event_type, price, threshold, _now_iso()),
        )


def insert_gtt_status_check(signal_id: int, gtt_order_id: str, upstox_status: str):
    with _conn() as con:
        con.execute(
            "INSERT INTO gtt_status_checks (signal_id, gtt_order_id, upstox_status, checked_at) VALUES (?,?,?,?)",
            (signal_id, gtt_order_id, upstox_status, _now_iso()),
        )


def get_signals_with_gtt_ids() -> list[dict]:
    """Return PENDING/ACTIVE signals that have a non-null gtt_order_ids value."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM signals WHERE status IN ('PENDING','ACTIVE') AND gtt_order_ids IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def update_signal_entry_executed(signal_id: int, entry_price: float):
    """Mark signal ACTIVE with the actual entry fill price from Upstox."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET status='ACTIVE', activated_at=?, entry_price=? WHERE id=?",
            (_now_iso(), entry_price, signal_id),
        )


def update_signal_exit_executed(signal_id: int, exit_price: float, pnl: float, exit_reason: str):
    """Mark signal terminal with actual exit fill price and gross P&L."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET status=?, exit_price=?, pnl=?, resolved_at=? WHERE id=?",
            (exit_reason, exit_price, pnl, _now_iso(), signal_id),
        )


def update_signal_upstox_status(signal_id: int, upstox_status: str):
    """Persist the raw Upstox GTT status string for debugging."""
    with _conn() as con:
        con.execute(
            "UPDATE signals SET upstox_gtt_status=? WHERE id=?",
            (upstox_status, signal_id),
        )


def get_signals_needing_backfill() -> list[dict]:
    """Return ALL signals (any status) with GTT IDs but missing entry_price or exit_price."""
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM signals
               WHERE gtt_order_ids IS NOT NULL
               AND (entry_price IS NULL OR exit_price IS NULL)"""
        ).fetchall()
    return [dict(r) for r in rows]


def backfill_signal_prices(
    signal_id: int,
    entry_price: float | None,
    exit_price: float | None,
    pnl: float | None,
):
    """Backfill price fields only — does not change status or resolved_at."""
    sets: list[str] = []
    vals: list = []
    if entry_price is not None:
        sets.append("entry_price=?")
        vals.append(entry_price)
    if exit_price is not None:
        sets.append("exit_price=?")
        vals.append(exit_price)
    if pnl is not None:
        sets.append("pnl=?")
        vals.append(pnl)
    if not sets:
        return
    vals.append(signal_id)
    with _conn() as con:
        con.execute(f"UPDATE signals SET {', '.join(sets)} WHERE id=?", vals)


if __name__ == "__main__":
    init_db()
    print(f"DB initialised at {DB_PATH}")
