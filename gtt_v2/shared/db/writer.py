"""Async non-blocking DB write queue — signal processing never waits on disk I/O."""
import asyncio
import structlog
import asyncpg
from typing import Callable, Any

log = structlog.get_logger()


class AsyncDBWriter:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self._running = False

    async def enqueue(self, fn: Callable, *args: Any) -> None:
        """Non-blocking — returns instantly. Drops if queue full (logs warning)."""
        try:
            self._queue.put_nowait((fn, args))
        except asyncio.QueueFull:
            log.warning("db_write_queue_full", qsize=self._queue.qsize())

    async def run(self) -> None:
        self._running = True
        log.info("db_writer_started")
        while self._running:
            try:
                fn, args = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                async with self.pool.acquire() as conn:
                    await fn(conn, *args)
            except Exception as e:
                log.error("db_write_failed", error=str(e), fn=fn.__name__)
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        self._running = False
        await self._queue.join()
