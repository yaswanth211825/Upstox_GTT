"""Redis async client — pool setup and helper wrappers."""
import redis.asyncio as aioredis
from typing import Optional
import structlog

log = structlog.get_logger()

_redis: Optional[aioredis.Redis] = None


async def create_redis(host: str = "redis", port: int = 6379) -> aioredis.Redis:
    global _redis
    _redis = aioredis.Redis(
        host=host,
        port=port,
        decode_responses=True,
        socket_keepalive=True,
        socket_connect_timeout=5,
        health_check_interval=30,
    )
    await _redis.ping()
    log.info("redis_connected", host=host, port=port)
    return _redis


async def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized — call create_redis() first")
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


# ── Pub/Sub helpers ──────────────────────────────────────────────────────────

INSTRUMENT_SUBSCRIBE_CHANNEL = "price_monitor:subscribe"
INSTRUMENT_UNSUBSCRIBE_CHANNEL = "price_monitor:unsubscribe"


async def notify_subscribe_instrument(redis: aioredis.Redis, instrument_key: str) -> None:
    """Notify price monitor to immediately subscribe to an instrument key."""
    await redis.publish(INSTRUMENT_SUBSCRIBE_CHANNEL, instrument_key)


async def notify_unsubscribe_instrument(redis: aioredis.Redis, instrument_key: str) -> None:
    """Notify price monitor to unsubscribe from an instrument key."""
    await redis.publish(INSTRUMENT_UNSUBSCRIBE_CHANNEL, instrument_key)


# ── Stream helpers ───────────────────────────────────────────────────────────

async def stream_add(
    redis: aioredis.Redis, stream: str, fields: dict, maxlen: int = 50_000
) -> str:
    return await redis.xadd(stream, fields, maxlen=maxlen, approximate=True)


async def ensure_consumer_group(
    redis: aioredis.Redis, stream: str, group: str
) -> None:
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def stream_read_group(
    redis: aioredis.Redis,
    stream: str,
    group: str,
    consumer: str,
    count: int = 10,
    block_ms: int = 2000,
) -> list[tuple[str, dict]]:
    result = await redis.xreadgroup(
        groupname=group,
        consumername=consumer,
        streams={stream: ">"},
        count=count,
        block=block_ms,
    )
    if not result:
        return []
    messages = []
    for _stream, entries in result:
        for msg_id, fields in entries:
            messages.append((msg_id, fields))
    return messages


async def stream_ack(
    redis: aioredis.Redis, stream: str, group: str, msg_id: str
) -> None:
    await redis.xack(stream, group, msg_id)


async def stream_reclaim_pending(
    redis: aioredis.Redis,
    stream: str,
    group: str,
    consumer: str,
    min_idle_ms: int = 60_000,
    count: int = 50,
) -> list[tuple[str, dict]]:
    """Claim messages stuck in PEL (not ACKed) for longer than min_idle_ms.
    Called at startup to retry signals that were interrupted mid-processing."""
    result = await redis.xautoclaim(
        stream, group, consumer,
        min_idle_time=min_idle_ms,
        start_id="0-0",
        count=count,
    )
    # xautoclaim returns (next_start_id, messages, deleted_ids)
    entries = result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else []
    messages = []
    for msg_id, fields in (entries or []):
        if fields:  # None fields = deleted message
            messages.append((msg_id, fields))
    if messages:
        log.info("pel_reclaimed", count=len(messages), stream=stream)
    return messages
