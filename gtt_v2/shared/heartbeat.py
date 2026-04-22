"""Background task that touches /tmp/alive every 30s for Docker healthcheck."""
import asyncio
import pathlib


async def heartbeat_loop(interval: int = 30) -> None:
    path = pathlib.Path("/tmp/alive")
    while True:
        path.touch()
        await asyncio.sleep(interval)
