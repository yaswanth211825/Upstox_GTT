"""
Simulated price feed — moves LTP in Redis so the system behaves as if
a live Market Data WebSocket is running.
"""
import asyncio
import structlog
import redis.asyncio as aioredis

log = structlog.get_logger()

LTP_TTL = 30   # longer TTL than live (1s) so prices persist in simulation


async def set_ltp(redis: aioredis.Redis, instrument_key: str, price: float) -> None:
    await redis.set(f"ltp:{instrument_key}", str(price), ex=LTP_TTL)


async def run_price_sequence(
    redis: aioredis.Redis,
    instrument_key: str,
    prices: list[float],
    interval_ms: int = 100,
) -> None:
    """
    Push a sequence of prices to Redis, simulating a price move over time.
    e.g. prices=[200, 205, 210, 215] moves price up in 4 steps.
    """
    for price in prices:
        await set_ltp(redis, instrument_key, price)
        await asyncio.sleep(interval_ms / 1000)


async def simulate_entry_fill(
    redis: aioredis.Redis,
    instrument_key: str,
    entry_trigger_price: float,
    action: str = "BUY",
) -> None:
    """
    Simulate price crossing entry trigger.
    For BUY ABOVE: ramp price up through entry_trigger_price.
    For SELL BELOW: ramp price down through entry_trigger_price.
    """
    if action == "BUY":
        prices = [
            entry_trigger_price - 5,
            entry_trigger_price - 2,
            entry_trigger_price,
            entry_trigger_price + 2,
        ]
    else:
        prices = [
            entry_trigger_price + 5,
            entry_trigger_price + 2,
            entry_trigger_price,
            entry_trigger_price - 2,
        ]
    await run_price_sequence(redis, instrument_key, prices)


async def simulate_target_hit(
    redis: aioredis.Redis,
    instrument_key: str,
    from_price: float,
    target_price: float,
) -> None:
    """Simulate price moving from entry fill to target."""
    step = (target_price - from_price) / 4
    prices = [from_price + step * i for i in range(1, 5)]
    await run_price_sequence(redis, instrument_key, prices)


async def simulate_stoploss_hit(
    redis: aioredis.Redis,
    instrument_key: str,
    from_price: float,
    sl_price: float,
) -> None:
    """Simulate price moving from entry fill down to stoploss."""
    step = (sl_price - from_price) / 4
    prices = [from_price + step * i for i in range(1, 5)]
    await run_price_sequence(redis, instrument_key, prices)
