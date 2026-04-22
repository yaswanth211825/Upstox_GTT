"""Token bucket rate limiter — atomic Redis Lua script."""
import time
import redis.asyncio as aioredis

# Lua script: atomic token bucket
# KEYS[1]=bucket_key  ARGV[1]=max_tokens  ARGV[2]=refill_per_ms  ARGV[3]=now_ms
_TOKEN_BUCKET_LUA = """
local data = redis.call('HMGET', KEYS[1], 'tokens', 'last_refill')
local tokens = tonumber(data[1])
local last   = tonumber(data[2])
local max_t  = tonumber(ARGV[1])
local now    = tonumber(ARGV[3])

if tokens == nil then
    tokens = max_t
    last   = now
end

tokens = math.min(max_t, tokens + (now - last) * tonumber(ARGV[2]))

if tokens < 1 then
    redis.call('HMSET', KEYS[1], 'tokens', tokens, 'last_refill', now)
    redis.call('PEXPIRE', KEYS[1], 60000)
    return 0
end

tokens = tokens - 1
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'last_refill', now)
redis.call('PEXPIRE', KEYS[1], 60000)
return 1
"""

# Endpoint configs: (max_tokens, refill_per_second)
RATE_LIMITS: dict[str, tuple[int, float]] = {
    "gtt_place":          (5,  2.0),
    "gtt_get":            (10, 5.0),
    "gtt_cancel":         (5,  2.0),
    "ltp_fetch":          (20, 10.0),
    "order_history":      (10, 5.0),
    "portfolio_ws_auth":  (3,  0.1),
}

_script_sha: dict = {}


async def _load_script(redis: aioredis.Redis) -> str:
    if "sha" not in _script_sha:
        _script_sha["sha"] = await redis.script_load(_TOKEN_BUCKET_LUA)
    return _script_sha["sha"]


async def acquire(redis: aioredis.Redis, endpoint: str) -> bool:
    """Returns True if the request is allowed, False if rate-limited."""
    max_tokens, refill_per_sec = RATE_LIMITS.get(endpoint, (10, 5.0))
    refill_per_ms = refill_per_sec / 1000.0
    now_ms = int(time.time() * 1000)
    key = f"rate_limit:{endpoint}"
    sha = await _load_script(redis)
    result = await redis.evalsha(sha, 1, key, str(max_tokens), str(refill_per_ms), str(now_ms))
    return bool(result)
