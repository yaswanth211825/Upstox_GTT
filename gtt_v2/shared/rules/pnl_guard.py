"""Daily P&L guard — blocks new GTTs once ±10% daily limit is hit.

Capital source (priority order):
  1. Redis key  daily_capital:{YYYYMMDD}  — set at 9:15 IST by trading engine
  2. ACCOUNT_CAPITAL env var              — static fallback if API fetch failed

Guard triggers on EITHER:
  • Cumulative realized P&L  >=  +capital * limit_pct  (profit target reached)
  • Cumulative realized P&L  <= -capital * limit_pct   (loss cap breached)
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo
import redis.asyncio as aioredis
import asyncpg
import structlog

from ..db.daily_pnl import upsert_daily_pnl, mark_pnl_limit_hit

log = structlog.get_logger()
IST = ZoneInfo("Asia/Kolkata")

# Lua: one round-trip — reads capital from Redis, checks ±threshold, sets flag atomically.
# KEYS[1] = pnl_limit:{date}   KEYS[2] = daily_pnl:{date}   KEYS[3] = daily_capital:{date}
# ARGV[1] = env_capital (fallback)   ARGV[2] = limit_pct
_ALLOW_SCRIPT = """
local limit_flag = redis.call('GET', KEYS[1])
if limit_flag == '1' then return 0 end

local pnl = tonumber(redis.call('GET', KEYS[2]) or '0')

-- prefer live-fetched capital; fall back to env value passed from Python
local raw_cap = redis.call('GET', KEYS[3])
local cap = tonumber(raw_cap or ARGV[1])
local threshold = cap * tonumber(ARGV[2])

if pnl >= threshold then
    redis.call('SET', KEYS[1], '1', 'EX', 90000)
    return 0
end
if pnl <= -threshold then
    redis.call('SET', KEYS[1], '1', 'EX', 90000)
    return 0
end
return 1
"""


def _today_key() -> str:
    return datetime.now(IST).strftime("%Y%m%d")


def _env_capital() -> float:
    return float(os.getenv("ACCOUNT_CAPITAL", "100000"))


def _limit_pct() -> float:
    return float(os.getenv("DAILY_PNL_LIMIT_PCT", "0.10"))


class DailyPnLGuard:
    async def allow(self, redis: aioredis.Redis) -> bool:
        date = _today_key()
        result = await redis.eval(
            _ALLOW_SCRIPT,
            3,                             # 3 keys
            f"pnl_limit:{date}",
            f"daily_pnl:{date}",
            f"daily_capital:{date}",       # live capital fetched at 9:15 IST
            str(_env_capital()),           # ARGV[1] fallback
            str(_limit_pct()),             # ARGV[2]
        )
        return bool(result)

    async def record(
        self, redis: aioredis.Redis, pool: asyncpg.Pool, pnl: float
    ) -> None:
        date = _today_key()
        new_pnl = await redis.incrbyfloat(f"daily_pnl:{date}", pnl)
        await redis.expire(f"daily_pnl:{date}", 90_000)

        # Read live capital (fallback to env)
        raw_cap = await redis.get(f"daily_capital:{date}")
        capital = float(raw_cap) if raw_cap else _env_capital()
        threshold = capital * _limit_pct()

        limit_hit = new_pnl >= threshold or new_pnl <= -threshold
        if limit_hit:
            await redis.set(f"pnl_limit:{date}", "1", ex=90_000)
            direction = "profit_target" if new_pnl >= threshold else "loss_cap"
            log.warning(
                "daily_pnl_limit_hit",
                direction=direction,
                pnl=new_pnl,
                threshold=threshold,
                capital=capital,
            )
            print(
                f"[PNL GUARD] Daily limit hit ({direction}): "
                f"₹{new_pnl:,.0f} vs ±₹{threshold:,.0f}. No more trades today."
            )

        async with pool.acquire() as conn:
            await upsert_daily_pnl(conn, pnl)
            if limit_hit:
                await mark_pnl_limit_hit(conn)
