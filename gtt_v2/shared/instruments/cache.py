"""Two-layer instrument cache: L1 in-memory dict (~10µs) + L2 Redis (~2ms)."""
import structlog
from typing import Optional
import redis.asyncio as aioredis

log = structlog.get_logger()

INSTRUMENTS_INDEX_KEY = "instruments:index"
INSTRUMENTS_DATA_PREFIX = "instruments:data:"
TTL_SECONDS = 86_400   # 24 hours


class InstrumentNotFound(KeyError):
    pass


class InstrumentCache:
    def __init__(self, redis: aioredis.Redis):
        self._redis = redis
        self._local: dict[str, dict] = {}

    @staticmethod
    def _index_key(underlying: str, strike: int, opt_type: str, expiry_ymd: str) -> str:
        return f"{underlying.upper()}:{strike}:{opt_type.upper()}:{expiry_ymd}"

    async def resolve(
        self,
        underlying: str,
        strike: int,
        opt_type: str,
        expiry_ymd: str,
    ) -> dict:
        """Return instrument metadata. Raises InstrumentNotFound if not cached."""
        k = self._index_key(underlying, strike, opt_type, expiry_ymd)

        # L1 hit
        if k in self._local:
            return self._local[k]

        # L2 Redis hit
        instrument_key = await self._redis.hget(INSTRUMENTS_INDEX_KEY, k)
        if instrument_key:
            data = await self._redis.hgetall(f"{INSTRUMENTS_DATA_PREFIX}{instrument_key}")
            if data:
                self._local[k] = data
                return data

        raise InstrumentNotFound(f"Instrument not found: {k}")

    async def load(self, instruments: list[dict]) -> int:
        """
        Bulk-load instruments into L1 and L2 Redis.
        instruments: list of dicts from Upstox instruments.json.gz
        Returns number of options loaded.
        """
        pipe = self._redis.pipeline()
        loaded = 0

        for inst in instruments:
            segment = inst.get("segment", "")
            if segment not in ("NSE_FO", "BSE_FO"):
                continue
            inst_type = str(inst.get("instrument_type", "")).upper()
            if inst_type not in ("CE", "PE"):
                continue

            underlying = str(inst.get("underlying_symbol", "")).upper()
            strike_raw = inst.get("strike_price")
            expiry_ts = inst.get("expiry")
            instrument_key = inst.get("instrument_key")

            if not (underlying and strike_raw is not None and expiry_ts and instrument_key):
                continue

            try:
                from datetime import datetime
                strike = int(float(strike_raw))
                expiry_ymd = datetime.fromtimestamp(expiry_ts / 1000).strftime("%Y-%m-%d")
            except Exception:
                continue

            k = self._index_key(underlying, strike, inst_type, expiry_ymd)
            data = {kk: str(vv) for kk, vv in inst.items() if vv is not None}

            self._local[k] = data
            pipe.hset(INSTRUMENTS_INDEX_KEY, k, instrument_key)
            pipe.hset(f"{INSTRUMENTS_DATA_PREFIX}{instrument_key}", mapping=data)
            pipe.expire(f"{INSTRUMENTS_DATA_PREFIX}{instrument_key}", TTL_SECONDS)
            loaded += 1

        pipe.expire(INSTRUMENTS_INDEX_KEY, TTL_SECONDS)
        await pipe.execute()
        log.info("instruments_loaded", count=loaded, l1_size=len(self._local))
        return loaded

    def clear_l1(self) -> None:
        self._local.clear()
