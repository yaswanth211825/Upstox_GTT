"""Pipeline audit log — writes human-readable events to a Redis ring buffer.

All services write here.  Admin API reads via /pipeline-log.
Ring buffer: last 300 entries (LPUSH + LTRIM).
"""
import json
from datetime import datetime, timezone, timedelta

PIPELINE_LOG_KEY = "pipeline_log"
PIPELINE_LOG_MAX = 300

_IST = timezone(timedelta(hours=5, minutes=30))


async def log_event(
    redis,
    stage: str,
    status: str,          # "info" | "success" | "warning" | "error"
    message: str,
    signal: str = "",     # human label e.g. "NIFTY 22000 CE" — empty if not yet parsed
    trace_id: str = "",
    detail: dict | None = None,
) -> None:
    """Push one pipeline event to Redis.  Never raises — failures are silently swallowed."""
    try:
        entry = json.dumps({
            "ts": datetime.now(_IST).strftime("%H:%M:%S"),
            "stage": stage,
            "status": status,
            "message": message,
            "signal": signal or "",
            "trace_id": trace_id or "",
            "detail": detail or {},
        }, default=str)
        await redis.lpush(PIPELINE_LOG_KEY, entry)
        await redis.ltrim(PIPELINE_LOG_KEY, 0, PIPELINE_LOG_MAX - 1)
    except Exception:
        pass


async def get_events(redis, count: int = 50) -> list[dict]:
    """Return the most recent `count` events, newest first."""
    try:
        raw = await redis.lrange(PIPELINE_LOG_KEY, 0, count - 1)
        return [json.loads(r) for r in raw]
    except Exception:
        return []
