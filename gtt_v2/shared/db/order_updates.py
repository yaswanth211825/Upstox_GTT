import asyncpg
from typing import Optional


async def insert_order_update(conn: asyncpg.Connection, data: dict) -> None:
    await conn.execute(
        """
        INSERT INTO order_updates (
            signal_id, order_id, status, message,
            average_price, quantity, payload_json
        ) VALUES ($1,$2,$3,$4,$5,$6,$7)
        """,
        data.get("signal_id"),
        data["order_id"],
        data.get("status"),
        data.get("message"),
        data.get("average_price"),
        data.get("quantity"),
        data.get("payload_json"),
    )


async def insert_trade_execution(conn: asyncpg.Connection, data: dict) -> None:
    await conn.execute(
        """
        INSERT INTO trade_executions (
            signal_id, order_id, trade_id, quantity, price, side
        ) VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT DO NOTHING
        """,
        data.get("signal_id"),
        data["order_id"],
        data.get("trade_id"),
        data.get("quantity"),
        data.get("price"),
        data.get("side"),
    )


async def get_trades_for_order(
    conn: asyncpg.Connection, order_id: str
) -> list[dict]:
    rows = await conn.fetch(
        "SELECT * FROM trade_executions WHERE order_id = $1 ORDER BY executed_at",
        order_id,
    )
    return [dict(r) for r in rows]


def weighted_avg_price(trades: list[dict]) -> Optional[float]:
    """VWAP across trade fills."""
    total_qty = sum(t["quantity"] for t in trades if t.get("quantity"))
    if not total_qty:
        return None
    total_val = sum(
        t["price"] * t["quantity"]
        for t in trades
        if t.get("price") and t.get("quantity")
    )
    return round(total_val / total_qty, 2)
