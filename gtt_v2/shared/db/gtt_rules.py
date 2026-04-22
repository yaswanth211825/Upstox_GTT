import asyncpg
from typing import Optional


async def upsert_gtt_rule(conn: asyncpg.Connection, data: dict) -> None:
    await conn.execute(
        """
        INSERT INTO gtt_rules (
            signal_id, gtt_order_id, strategy, trigger_type,
            trigger_price, transaction_type, status, order_id,
            message, payload_json
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT (gtt_order_id, strategy) DO UPDATE SET
            status = EXCLUDED.status,
            order_id = EXCLUDED.order_id,
            message = EXCLUDED.message,
            payload_json = EXCLUDED.payload_json,
            last_event_at = now()
        """,
        data["signal_id"],
        data["gtt_order_id"],
        data["strategy"],
        data.get("trigger_type"),
        data.get("trigger_price"),
        data.get("transaction_type"),
        data.get("status"),
        data.get("order_id"),
        data.get("message"),
        data.get("payload_json"),
    )


async def get_gtt_rules_for_signal(
    conn: asyncpg.Connection, signal_id: int
) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM gtt_rules WHERE signal_id = $1 ORDER BY id", signal_id
    )
    return [dict(r) for r in rows]


async def get_gtt_rule_by_order_id(
    conn: asyncpg.Connection, gtt_order_id: str
) -> Optional[dict]:
    """Backward-compatible alias: look up by gtt_order_id."""
    return await get_gtt_rule_by_gtt_order_id(conn, gtt_order_id)


async def get_gtt_rule_by_gtt_order_id(
    conn: asyncpg.Connection, gtt_order_id: str
) -> Optional[dict]:
    row = await conn.fetchrow(
        "SELECT * FROM gtt_rules WHERE gtt_order_id = $1 LIMIT 1", gtt_order_id
    )
    return dict(row) if row else None


async def get_gtt_rule_by_child_order_id(
    conn: asyncpg.Connection, order_id: str
) -> Optional[dict]:
    row = await conn.fetchrow(
        "SELECT * FROM gtt_rules WHERE order_id = $1 ORDER BY id DESC LIMIT 1", order_id
    )
    return dict(row) if row else None


async def get_gtt_rules_by_gtt_order_id(
    conn: asyncpg.Connection, gtt_order_id: str
) -> list[dict]:
    """Return ALL leg rows for a given gtt_order_id (ENTRY, TARGET, STOPLOSS)."""
    rows = await conn.fetch(
        "SELECT * FROM gtt_rules WHERE gtt_order_id = $1 ORDER BY id", gtt_order_id
    )
    return [dict(r) for r in rows]
