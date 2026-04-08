"""
db.py -- SQLite database layer for GTT signal tracking.

The schema keeps the original signal lifecycle table and adds event-oriented
tables for GTT rules, order updates, trade executions, and raw Upstox events.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

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
    gtt_order_id      TEXT,
    tracker_status    TEXT,
    entry_rule_status TEXT,
    target_rule_status TEXT,
    stoploss_rule_status TEXT,
    entry_order_id    TEXT,
    target_order_id   TEXT,
    stoploss_order_id TEXT,
    gtt_place_price   REAL,
    entry_price       REAL,
    exit_price        REAL,
    pnl               REAL,
    upstox_gtt_status TEXT,
    signal_at         TEXT NOT NULL,
    gtt_placed_at     TEXT,
    activated_at      TEXT,
    resolved_at       TEXT,
    last_event_at     TEXT,
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_instrument_key ON signals (instrument_key);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals (status);
CREATE INDEX IF NOT EXISTS idx_signals_gtt_order_id ON signals (gtt_order_id);
CREATE INDEX IF NOT EXISTS idx_signals_entry_order_id ON signals (entry_order_id);
CREATE INDEX IF NOT EXISTS idx_signals_target_order_id ON signals (target_order_id);
CREATE INDEX IF NOT EXISTS idx_signals_stoploss_order_id ON signals (stoploss_order_id);

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

CREATE TABLE IF NOT EXISTS gtt_rules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id        INTEGER NOT NULL REFERENCES signals(id),
    gtt_order_id     TEXT NOT NULL,
    strategy         TEXT NOT NULL,
    trigger_type     TEXT,
    trigger_price    REAL,
    transaction_type TEXT,
    status           TEXT,
    order_id         TEXT,
    message          TEXT,
    trailing_gap     REAL,
    payload_json     TEXT,
    last_event_at    TEXT NOT NULL,
    UNIQUE(signal_id, strategy)
);

CREATE INDEX IF NOT EXISTS idx_gtt_rules_gtt_order_id ON gtt_rules (gtt_order_id);
CREATE INDEX IF NOT EXISTS idx_gtt_rules_order_id ON gtt_rules (order_id);

CREATE TABLE IF NOT EXISTS order_updates (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id          INTEGER REFERENCES signals(id),
    order_id           TEXT NOT NULL,
    strategy           TEXT,
    status             TEXT,
    transaction_type   TEXT,
    average_price      REAL,
    price              REAL,
    trigger_price      REAL,
    quantity           INTEGER,
    filled_quantity    INTEGER,
    pending_quantity   INTEGER,
    exchange_order_id  TEXT,
    status_message     TEXT,
    status_message_raw TEXT,
    exchange_timestamp TEXT,
    order_timestamp    TEXT,
    payload_json       TEXT NOT NULL,
    recorded_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_order_updates_order_id ON order_updates (order_id);
CREATE INDEX IF NOT EXISTS idx_order_updates_signal_id ON order_updates (signal_id);

CREATE TABLE IF NOT EXISTS trade_executions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id          INTEGER REFERENCES signals(id),
    order_id           TEXT NOT NULL,
    trade_id           TEXT,
    exchange_order_id  TEXT,
    side               TEXT,
    quantity           INTEGER,
    price              REAL,
    trade_timestamp    TEXT,
    payload_json       TEXT NOT NULL,
    recorded_at        TEXT NOT NULL,
    UNIQUE(order_id, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_trade_executions_order_id ON trade_executions (order_id);
CREATE INDEX IF NOT EXISTS idx_trade_executions_signal_id ON trade_executions (signal_id);

CREATE TABLE IF NOT EXISTS upstox_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   INTEGER REFERENCES signals(id),
    update_type TEXT NOT NULL,
    entity_id   TEXT,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_upstox_events_signal_id ON upstox_events (signal_id);
CREATE INDEX IF NOT EXISTS idx_upstox_events_entity_id ON upstox_events (entity_id);
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
    "ALTER TABLE signals ADD COLUMN gtt_order_id TEXT",
    "ALTER TABLE signals ADD COLUMN tracker_status TEXT",
    "ALTER TABLE signals ADD COLUMN entry_rule_status TEXT",
    "ALTER TABLE signals ADD COLUMN target_rule_status TEXT",
    "ALTER TABLE signals ADD COLUMN stoploss_rule_status TEXT",
    "ALTER TABLE signals ADD COLUMN entry_order_id TEXT",
    "ALTER TABLE signals ADD COLUMN target_order_id TEXT",
    "ALTER TABLE signals ADD COLUMN stoploss_order_id TEXT",
    "ALTER TABLE signals ADD COLUMN last_event_at TEXT",
]


def _run_migrations(con: sqlite3.Connection):
    for stmt in _MIGRATIONS:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass


def init_db():
    with _conn() as con:
        con.executescript(DDL)
        _run_migrations(con)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=True)


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
        "gtt_order_ids": _json(gtt_ids) if gtt_ids else None,
        "gtt_order_id": gtt_ids[0] if gtt_ids else None,
        "tracker_status": "GTT_PLACED" if status == "PENDING" else status,
        "gtt_place_price": ltp_at_placement,
        "signal_at": _now_iso(),
        "gtt_placed_at": _now_iso() if status == "PENDING" else None,
        "last_event_at": _now_iso(),
        "notes": notes,
    }
    with _conn() as con:
        cur = con.execute(
            """
            INSERT OR IGNORE INTO signals
                (redis_message_id, signal_summary, action, underlying, strike,
                 option_type, expiry, product, quantity, instrument_key,
                 entry_low, entry_high, stoploss, target1, target2, target3,
                 status, gtt_order_ids, gtt_order_id, tracker_status, gtt_place_price,
                 signal_at, gtt_placed_at, last_event_at, notes)
            VALUES
                (:redis_message_id, :signal_summary, :action, :underlying, :strike,
                 :option_type, :expiry, :product, :quantity, :instrument_key,
                 :entry_low, :entry_high, :stoploss, :target1, :target2, :target3,
                 :status, :gtt_order_ids, :gtt_order_id, :tracker_status, :gtt_place_price,
                 :signal_at, :gtt_placed_at, :last_event_at, :notes)
            """,
            row,
        )
        signal_id = cur.lastrowid
        if signal_id and gtt_ids:
            for gtt_id in gtt_ids:
                con.execute(
                    "UPDATE signals SET gtt_order_id=COALESCE(gtt_order_id, ?) WHERE id=?",
                    (gtt_id, signal_id),
                )
        return signal_id


def update_signal_status(signal_id: int, new_status: str, notes: str = None):
    now = _now_iso()
    terminal = {"TARGET1_HIT", "STOPLOSS_HIT", "EXPIRED", "FAILED", "CANCELLED"}
    with _conn() as con:
        if new_status in terminal:
            con.execute(
                """
                UPDATE signals
                SET status=?, tracker_status=?, resolved_at=?, last_event_at=?, notes=COALESCE(?, notes)
                WHERE id=?
                """,
                (new_status, new_status, now, now, notes, signal_id),
            )
        else:
            con.execute(
                """
                UPDATE signals
                SET status=?, tracker_status=?, last_event_at=?, notes=COALESCE(?, notes)
                WHERE id=?
                """,
                (new_status, new_status, now, notes, signal_id),
            )


def update_signal_activated(signal_id: int):
    now = _now_iso()
    with _conn() as con:
        con.execute(
            """
            UPDATE signals
            SET status='ACTIVE', tracker_status='ENTRY_EXECUTED', activated_at=?, last_event_at=?
            WHERE id=?
            """,
            (now, now, signal_id),
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
    with _conn() as con:
        rows = con.execute(
            """
            SELECT * FROM signals
            WHERE status IN ('PENDING','ACTIVE')
              AND (gtt_order_id IS NOT NULL OR gtt_order_ids IS NOT NULL)
            """
        ).fetchall()
    return [dict(r) for r in rows]


def update_signal_entry_executed(signal_id: int, entry_price: float):
    with _conn() as con:
        con.execute(
            """
            UPDATE signals
            SET status='ACTIVE', tracker_status='ENTRY_EXECUTED', activated_at=?, entry_price=?, last_event_at=?
            WHERE id=?
            """,
            (_now_iso(), entry_price, _now_iso(), signal_id),
        )


def update_signal_exit_executed(signal_id: int, exit_price: float, pnl: float, exit_reason: str):
    with _conn() as con:
        con.execute(
            """
            UPDATE signals
            SET status=?, tracker_status=?, exit_price=?, pnl=?, resolved_at=?, last_event_at=?
            WHERE id=?
            """,
            (exit_reason, exit_reason, exit_price, pnl, _now_iso(), _now_iso(), signal_id),
        )


def update_signal_upstox_status(signal_id: int, upstox_status: str):
    with _conn() as con:
        con.execute(
            """
            UPDATE signals
            SET upstox_gtt_status=?, last_event_at=?
            WHERE id=?
            """,
            (upstox_status, _now_iso(), signal_id),
        )


def get_signals_needing_backfill() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """
            SELECT * FROM signals
            WHERE (gtt_order_id IS NOT NULL OR gtt_order_ids IS NOT NULL)
              AND (entry_price IS NULL OR exit_price IS NULL)
            """
        ).fetchall()
    return [dict(r) for r in rows]


def backfill_signal_prices(
    signal_id: int,
    entry_price: float | None,
    exit_price: float | None,
    pnl: float | None,
):
    sets: list[str] = []
    vals: list[Any] = []
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
    sets.append("last_event_at=?")
    vals.append(_now_iso())
    vals.append(signal_id)
    with _conn() as con:
        con.execute(f"UPDATE signals SET {', '.join(sets)} WHERE id=?", vals)


def get_signal(signal_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    return dict(row) if row else None


def get_signal_by_gtt_order_id(gtt_order_id: str) -> dict | None:
    if not gtt_order_id:
        return None
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM signals WHERE gtt_order_id=? OR gtt_order_ids LIKE ? LIMIT 1",
            (gtt_order_id, f"%{gtt_order_id}%"),
        ).fetchone()
    return dict(row) if row else None


def get_signal_by_order_id(order_id: str) -> dict | None:
    if not order_id:
        return None
    with _conn() as con:
        row = con.execute(
            """
            SELECT * FROM signals
            WHERE entry_order_id=? OR target_order_id=? OR stoploss_order_id=?
            LIMIT 1
            """,
            (order_id, order_id, order_id),
        ).fetchone()
        if row:
            return dict(row)
        row = con.execute(
            "SELECT signal_id FROM gtt_rules WHERE order_id=? LIMIT 1",
            (order_id,),
        ).fetchone()
        if not row:
            return None
        row2 = con.execute("SELECT * FROM signals WHERE id=?", (row["signal_id"],)).fetchone()
    return dict(row2) if row2 else None


def set_signal_gtt_order(signal_id: int, gtt_order_id: str):
    if not gtt_order_id:
        return
    with _conn() as con:
        row = con.execute("SELECT gtt_order_ids FROM signals WHERE id=?", (signal_id,)).fetchone()
        gtt_ids = []
        if row and row["gtt_order_ids"]:
            try:
                gtt_ids = json.loads(row["gtt_order_ids"])
            except Exception:
                gtt_ids = []
        if gtt_order_id not in gtt_ids:
            gtt_ids.append(gtt_order_id)
        con.execute(
            """
            UPDATE signals
            SET gtt_order_id=COALESCE(gtt_order_id, ?),
                gtt_order_ids=?,
                last_event_at=?
            WHERE id=?
            """,
            (gtt_order_id, _json(gtt_ids), _now_iso(), signal_id),
        )


def upsert_gtt_rule(signal_id: int, gtt_order_id: str, rule: dict):
    strategy = str(rule.get("strategy", "")).upper()
    if not strategy:
        return
    now = _now_iso()
    payload = _json(rule)
    order_id = rule.get("order_id")
    status = rule.get("status")
    with _conn() as con:
        con.execute(
            """
            INSERT INTO gtt_rules
                (signal_id, gtt_order_id, strategy, trigger_type, trigger_price,
                 transaction_type, status, order_id, message, trailing_gap,
                 payload_json, last_event_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(signal_id, strategy) DO UPDATE SET
                gtt_order_id=excluded.gtt_order_id,
                trigger_type=excluded.trigger_type,
                trigger_price=excluded.trigger_price,
                transaction_type=excluded.transaction_type,
                status=excluded.status,
                order_id=COALESCE(excluded.order_id, gtt_rules.order_id),
                message=excluded.message,
                trailing_gap=excluded.trailing_gap,
                payload_json=excluded.payload_json,
                last_event_at=excluded.last_event_at
            """,
            (
                signal_id,
                gtt_order_id,
                strategy,
                rule.get("trigger_type"),
                rule.get("trigger_price"),
                rule.get("transaction_type"),
                status,
                order_id,
                rule.get("message"),
                rule.get("trailing_gap"),
                payload,
                now,
            ),
        )

        column_map = {
            "ENTRY": ("entry_rule_status", "entry_order_id"),
            "TARGET": ("target_rule_status", "target_order_id"),
            "STOPLOSS": ("stoploss_rule_status", "stoploss_order_id"),
        }
        status_col, order_col = column_map.get(strategy, (None, None))
        if status_col:
            if order_id:
                con.execute(
                    f"""
                    UPDATE signals
                    SET {status_col}=?,
                        {order_col}=COALESCE({order_col}, ?),
                        last_event_at=?
                    WHERE id=?
                    """,
                    (status, order_id, now, signal_id),
                )
            else:
                con.execute(
                    f"UPDATE signals SET {status_col}=?, last_event_at=? WHERE id=?",
                    (status, now, signal_id),
                )


def get_signal_strategy_by_order_id(order_id: str) -> str | None:
    if not order_id:
        return None
    with _conn() as con:
        row = con.execute(
            "SELECT strategy FROM gtt_rules WHERE order_id=? LIMIT 1",
            (order_id,),
        ).fetchone()
    return str(row["strategy"]).upper() if row and row["strategy"] else None


def insert_order_update(signal_id: int | None, strategy: str | None, payload: dict):
    with _conn() as con:
        con.execute(
            """
            INSERT INTO order_updates
                (signal_id, order_id, strategy, status, transaction_type, average_price,
                 price, trigger_price, quantity, filled_quantity, pending_quantity,
                 exchange_order_id, status_message, status_message_raw, exchange_timestamp,
                 order_timestamp, payload_json, recorded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                signal_id,
                payload.get("order_id"),
                strategy,
                payload.get("status"),
                payload.get("transaction_type"),
                payload.get("average_price"),
                payload.get("price"),
                payload.get("trigger_price"),
                payload.get("quantity"),
                payload.get("filled_quantity"),
                payload.get("pending_quantity"),
                payload.get("exchange_order_id"),
                payload.get("status_message"),
                payload.get("status_message_raw"),
                payload.get("exchange_timestamp"),
                payload.get("order_timestamp"),
                _json(payload),
                _now_iso(),
            ),
        )


def replace_trade_executions(signal_id: int | None, order_id: str, trades: list[dict]):
    with _conn() as con:
        con.execute("DELETE FROM trade_executions WHERE order_id=?", (order_id,))
        for trade in trades:
            con.execute(
                """
                INSERT OR IGNORE INTO trade_executions
                    (signal_id, order_id, trade_id, exchange_order_id, side,
                     quantity, price, trade_timestamp, payload_json, recorded_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    signal_id,
                    order_id,
                    trade.get("trade_id") or trade.get("tradeId"),
                    trade.get("exchange_order_id") or trade.get("exchangeOrderId"),
                    trade.get("transaction_type") or trade.get("side"),
                    trade.get("quantity") or trade.get("filled_quantity"),
                    trade.get("price") or trade.get("average_price"),
                    trade.get("trade_timestamp") or trade.get("order_timestamp") or trade.get("exchange_timestamp"),
                    _json(trade),
                    _now_iso(),
                ),
            )


def record_upstox_event(signal_id: int | None, update_type: str, entity_id: str | None, payload: dict):
    with _conn() as con:
        con.execute(
            """
            INSERT INTO upstox_events (signal_id, update_type, entity_id, payload_json, received_at)
            VALUES (?,?,?,?,?)
            """,
            (signal_id, update_type, entity_id, _json(payload), _now_iso()),
        )


def update_tracker_fields(
    signal_id: int,
    *,
    tracker_status: str | None = None,
    upstox_gtt_status: str | None = None,
    notes: str | None = None,
):
    sets = ["last_event_at=?"]
    vals: list[Any] = [_now_iso()]
    if tracker_status is not None:
        sets.append("tracker_status=?")
        vals.append(tracker_status)
    if upstox_gtt_status is not None:
        sets.append("upstox_gtt_status=?")
        vals.append(upstox_gtt_status)
    if notes is not None:
        sets.append("notes=?")
        vals.append(notes)
    vals.append(signal_id)
    with _conn() as con:
        con.execute(f"UPDATE signals SET {', '.join(sets)} WHERE id=?", vals)

