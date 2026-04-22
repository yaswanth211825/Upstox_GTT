"""Portfolio WebSocket — exponential backoff + 30s heartbeat."""
import asyncio
import json
import random
import structlog
from typing import Callable, Awaitable
import websockets

log = structlog.get_logger()

HEARTBEAT_INTERVAL = 30
MAX_RECONNECT_DELAY = 120


class PortfolioWebSocket:
    def __init__(
        self,
        url_factory: Callable[[], Awaitable[str]],
        on_message: Callable[[dict], Awaitable[None]],
    ):
        self._url_factory = url_factory
        self._on_message = on_message
        self._running = False

    async def run(self) -> None:
        self._running = True
        attempt = 0
        while self._running:
            try:
                url = await self._url_factory()
                async with websockets.connect(url) as ws:
                    attempt = 0
                    log.info("portfolio_ws_connected")
                    await asyncio.gather(
                        self._recv_loop(ws),
                        self._heartbeat_loop(ws),
                    )
            except Exception as e:
                attempt += 1
                delay = min(2 ** attempt + random.uniform(0, 1), MAX_RECONNECT_DELAY)
                log.warning("portfolio_ws_disconnected", error=str(e), reconnect_in=delay)
                await asyncio.sleep(delay)

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
                await self._on_message(msg)
            except Exception as e:
                log.error("portfolio_ws_parse_error", error=str(e))

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await ws.send(json.dumps({"type": "ping"}))
            except Exception:
                break

    def stop(self) -> None:
        self._running = False
