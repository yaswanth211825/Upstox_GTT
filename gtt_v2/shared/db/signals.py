"""Signal repository — all DB operations for the signals table."""
import hashlib
import json
from typing import Optional
import asyncpg
import structlog

log = structlog.get_logger()


def compute_signal_hash(
    underlying: str,
    strike: Optional[int],
    option_type: Optional[str],
    entry_low: float,
    stoploss: float,
    targets: list[float],
    expiry: Optional[str],
) -> str:
    payload = json.dumps(
        {
            "u": underlying,
            "k": strike,
            "o": option_type,
            "e": entry_low,
            "s": stoploss,
            "t": sorted(targets or []),
            "x": expiry,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def insert_signal(conn: asyncpg.Connection, data: dict) -> int:
    return await conn.fetchval(
        """
        INSERT INTO signals (
            redis_message_id, signal_hash, action, underlying, strike,
            option_type, expiry, product, quantity_lots, instrument_key,
            entry_low_raw, entry_high_raw, stoploss_raw, targets_raw,
            entry_low_adj, entry_high_adj, stoploss_adj, targets_adj,
            trade_type, safe_only, risk_only, status
        ) VALUES (
            $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
            $11,$12,$13,$14,$15,$16,$17,$18,
            $19,$20,$21,$22
        )
        ON CONFLICT (signal_hash) DO NOTHING
        RETURNING id
        """,
        data["redis_message_id"],
        data.get("signal_hash"),
        data["action"],
        data["underlying"],
        data.get("strike"),
        data.get("option_type"),
        data.get("expiry"),
        data.get("product", "I"),
        data["quantity_lots"],
        data.get("instrument_key"),
        data["entry_low_raw"],
        data.get("entry_high_raw"),
        data["stoploss_raw"],
        data.get("targets_raw"),
        data.get("entry_low_adj"),
        data.get("entry_high_adj"),
        data.get("stoploss_adj"),
        data.get("targets_adj"),
        data.get("trade_type"),
        data.get("safe_only", False),
        data.get("risk_only", False),
        data.get("status", "PENDING"),
    )


async def update_signal_status(
    conn: asyncpg.Connection,
    signal_id: int,
    status: str,
    block_reason: Optional[str] = None,
    gtt_order_ids: Optional[list[str]] = None,
    ltp_at_placement: Optional[float] = None,
    notes: Optional[str] = None,
) -> None:
    await conn.execute(
        """
        UPDATE signals SET
            status = $2,
            block_reason = COALESCE($3, block_reason),
            gtt_order_ids = COALESCE($4, gtt_order_ids),
            ltp_at_placement = COALESCE($5, ltp_at_placement),
            activated_at = CASE WHEN $2 = 'ACTIVE' THEN COALESCE(activated_at, now()) ELSE activated_at END,
            resolved_at = CASE WHEN $2 IN ('TARGET1_HIT','TARGET2_HIT','TARGET3_HIT',
                'STOPLOSS_HIT','EXPIRED','CANCELLED','FAILED') THEN now() ELSE resolved_at END,
            notes = COALESCE($6, notes),
            last_event_at = now()
        WHERE id = $1
        """,
        signal_id, status, block_reason, gtt_order_ids, ltp_at_placement, notes,
    )


async def update_signal_pnl(
    conn: asyncpg.Connection,
    signal_id: int,
    entry_price: float,
    exit_price: float,
    pnl: float,
) -> None:
    await conn.execute(
        """
        UPDATE signals SET
            entry_price = $2,
            exit_price = $3,
            pnl = $4,
            activated_at = COALESCE(activated_at, now()),
            last_event_at = now()
        WHERE id = $1
        """,
        signal_id, entry_price, exit_price, pnl,
    )


async def get_signal(conn: asyncpg.Connection, signal_id: int) -> Optional[dict]:
    row = await conn.fetchrow("SELECT * FROM signals WHERE id = $1", signal_id)
    return dict(row) if row else None


async def get_signals_older_than(
    conn: asyncpg.Connection, hours: int
) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT * FROM signals
        WHERE status = 'PENDING'
          AND signal_at < now() - make_interval(hours => $1)
        """,
        hours,
    )
    return [dict(r) for r in rows]


async def get_pending_signals_after_close(conn: asyncpg.Connection) -> list[dict]:
    rows = await conn.fetch(
        """
        SELECT * FROM signals
        WHERE status IN ('PENDING','ACTIVE')
          AND (signal_at AT TIME ZONE 'Asia/Kolkata')::date = CURRENT_DATE AT TIME ZONE 'Asia/Kolkata'
        """
    )
    return [dict(r) for r in rows]
