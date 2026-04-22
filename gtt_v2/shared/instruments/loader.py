"""Download and parse Upstox instruments.json.gz."""
import gzip
import json
import structlog
import httpx

log = structlog.get_logger()

INSTRUMENTS_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
)


async def download_instruments(client: httpx.AsyncClient) -> list[dict]:
    """Download and decompress the full Upstox instruments list."""
    log.info("instruments_download_start", url=INSTRUMENTS_URL)
    response = await client.get(INSTRUMENTS_URL, timeout=60)
    response.raise_for_status()
    data = json.loads(gzip.decompress(response.content))
    log.info("instruments_download_done", total=len(data))
    return data
