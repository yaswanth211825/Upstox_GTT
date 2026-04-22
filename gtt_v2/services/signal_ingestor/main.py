"""Signal Ingestor entrypoint."""
import asyncio
import sys
import os
import structlog

from shared.config import settings, configure_logging
from shared.redis.client import create_redis
from shared.heartbeat import heartbeat_loop
from .telegram_listener import run_listener

log = structlog.get_logger()


async def main() -> None:
    configure_logging("signal-ingestor")
    log.info("signal_ingestor_starting")

    redis = await create_redis(settings.redis_host, settings.redis_port)
    asyncio.create_task(heartbeat_loop())

    # Import AI parser from v1 codebase (unchanged)
    v1_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
    )
    sys.path.insert(0, v1_root)
    from ParseerWithAI.ai_signal_parser import AISignalParser  # type: ignore

    provider = settings.ai_provider.upper()
    api_key = os.getenv("OPENAI_API_KEY", "") if provider == "OPENAI" else settings.gemini_api_key

    # Guard: never pass a Gemini model name to OpenAI and vice versa
    model = settings.ai_model or None
    if model and provider == "OPENAI" and "gemini" in model.lower():
        model = None   # let AISignalParser default to gpt-4o-mini
    if model and provider == "GEMINI" and ("gpt" in model.lower() or "o1" in model.lower()):
        model = None   # let AISignalParser default to gemini-2.0-flash

    log.info("ai_parser_init", provider=provider, model=model or "default")
    parser = AISignalParser(
        api_key=api_key,
        provider=provider,
        model=model,
    )

    await run_listener(redis, parser)


if __name__ == "__main__":
    asyncio.run(main())
