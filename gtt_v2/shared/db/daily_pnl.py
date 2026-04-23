import asyncpg
from typing import Optional


async def upsert_daily_pnl(
    conn: asyncpg.Connection, pnl_delta: float
) -> float:
    row = await conn.fetchrow(
        """
        INSERT INTO daily_pnl (trading_date, realized_pnl, trade_count)
        VALUES ((now() AT TIME ZONE 'Asia/Kolkata')::date, $1, 1)
        ON CONFLICT (trading_date) DO UPDATE SET
            realized_pnl = daily_pnl.realized_pnl + EXCLUDED.realized_pnl,
            trade_count  = daily_pnl.trade_count + 1,
            updated_at   = now()
        RETURNING realized_pnl
        """,
        pnl_delta,
    )
    return float(row["realized_pnl"])


async def get_today_pnl(conn: asyncpg.Connection) -> Optional[float]:
    val = await conn.fetchval(
        """
        SELECT realized_pnl FROM daily_pnl
        WHERE trading_date = (now() AT TIME ZONE 'Asia/Kolkata')::date
        """
    )
    return float(val) if val is not None else None


async def mark_pnl_limit_hit(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        UPDATE daily_pnl SET pnl_limit_hit = true
        WHERE trading_date = (now() AT TIME ZONE 'Asia/Kolkata')::date
        """
    )
