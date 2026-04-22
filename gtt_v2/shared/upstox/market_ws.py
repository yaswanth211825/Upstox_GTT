"""Market Data WebSocket — protobuf decode + LTP Redis cache + dynamic subscription."""
import asyncio
import json
import random
import struct
import uuid
import structlog
from typing import Callable, Awaitable
import websockets
import redis.asyncio as aioredis

log = structlog.get_logger()

LTP_TTL = 5   # seconds — short TTL, price monitor must be running
MAX_RECONNECT_DELAY = 120


class MarketDataWebSocket:
    def __init__(
        self,
        url_factory: Callable[[], Awaitable[str]],
        redis: aioredis.Redis,
        on_ltp: Callable[[str, float], Awaitable[None]] | None = None,
    ):
        self._url_factory = url_factory
        self._redis = redis
        self._on_ltp = on_ltp
        self._running = False
        self._ws = None                     # live websocket reference
        self._subscribed: set[str] = set()  # instrument keys currently subscribed

    # ── Public subscription API ───────────────────────────────────────────────

    async def subscribe(self, instrument_keys: list[str]) -> None:
        """Subscribe to LTP feed for the given instrument keys."""
        new_keys = [k for k in instrument_keys if k and k not in self._subscribed]
        if not new_keys:
            return
        self._subscribed.update(new_keys)
        await self._send_sub_message("sub", new_keys)
        log.info("market_ws_subscribed", keys=new_keys, total=len(self._subscribed))

    async def unsubscribe(self, instrument_keys: list[str]) -> None:
        """Unsubscribe from LTP feed for the given instrument keys."""
        keys_to_remove = [k for k in instrument_keys if k in self._subscribed]
        if not keys_to_remove:
            return
        for k in keys_to_remove:
            self._subscribed.discard(k)
        await self._send_sub_message("unsub", keys_to_remove)
        log.info("market_ws_unsubscribed", keys=keys_to_remove, total=len(self._subscribed))

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _send_sub_message(self, method: str, keys: list[str]) -> None:
        """Send a sub/unsub JSON frame to Upstox. No-op if socket not connected."""
        if self._ws is None:
            return
        try:
            msg = json.dumps({
                "guid": uuid.uuid4().hex,
                "method": method,
                "data": {
                    "mode": "ltpc",
                    "instrumentKeys": keys,
                },
            })
            await self._ws.send(msg)
        except Exception as e:
            log.warning("market_ws_send_failed", method=method, error=str(e))

    async def run(self) -> None:
        self._running = True
        attempt = 0
        while self._running:
            try:
                url = await self._url_factory()
                async with websockets.connect(url) as ws:
                    attempt = 0
                    self._ws = ws
                    log.info("market_ws_connected")

                    # Resubscribe to all tracked keys on reconnect
                    if self._subscribed:
                        await self._send_sub_message("sub", list(self._subscribed))
                        log.info("market_ws_resubscribed", count=len(self._subscribed))

                    await self._recv_loop(ws)
            except Exception as e:
                self._ws = None
                attempt += 1
                delay = min(2 ** attempt + random.uniform(0, 1), MAX_RECONNECT_DELAY)
                log.warning("market_ws_disconnected", error=str(e), reconnect_in=round(delay, 1))
                await asyncio.sleep(delay)

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            try:
                feeds = _decode_market_feed(raw)
                for instrument_key, ltp in feeds:
                    await self._redis.set(f"ltp:{instrument_key}", str(ltp), ex=LTP_TTL)
                    if self._on_ltp:
                        await self._on_ltp(instrument_key, ltp)
            except Exception as e:
                log.error("market_ws_decode_error", error=str(e))

    def stop(self) -> None:
        self._running = False


def _decode_market_feed(raw: bytes) -> list[tuple[str, float]]:
    """
    Decode Upstox MarketDataFeedV3 protobuf response.
    Proto file is at /app/MarketDataFeedV3_pb2.py (copied by Dockerfile).
    """
    try:
        import sys
        import os
        # /app/shared/upstox/ → go up 2 levels → /app/
        app_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        if app_root not in sys.path:
            sys.path.insert(0, app_root)
        from MarketDataFeedV3_pb2 import FeedResponse  # type: ignore
        resp = FeedResponse()
        resp.ParseFromString(raw)
        results = []
        for key, feed in resp.feeds.items():
            ltp = feed.ff.marketFF.ltpc.ltp
            if ltp:
                results.append((key, float(ltp)))
        return results
    except Exception as e:
        log.debug("market_ws_proto_decode_failed", error=str(e))
        return []
