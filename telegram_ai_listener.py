"""
telegram_ai_listener.py -- AI-powered Telegram signal listener for the unified app.
"""

from dotenv import load_dotenv
import os
import asyncio
import logging
import time
import traceback

import httpx
import redis.asyncio as redis
from telethon import TelegramClient, events

from entity_finder import resolve_entity
from latency import duration_ms, log_latency, now_ms, now_perf_ns
from ParseerWithAI.ai_signal_parser import AISignalParser
from settings import LOG_DIR, TELEGRAM_SESSION_PATH

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE")
GROUP_ENTITY_NAME = os.getenv("GROUP_ENTITY_NAME", "MS-OPTIONS-PREMIUM")
AI_PROVIDER = os.getenv("AI_PROVIDER", "GEMINI").upper()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AI_MODEL = os.getenv("AI_MODEL")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_STREAM_KEY = os.getenv("REDIS_STREAM_KEY", os.getenv("REDIS_STREAM_NAME", "raw_trade_signals"))
OPENALGO_WEBHOOK_URL = os.getenv("OPENALGO_WEBHOOK_URL", "")
OPENALGO_API_KEY = os.getenv("OPENALGO_API_KEY", "")

logger = logging.getLogger("telegram_ai_listener")
if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    _ch = logging.StreamHandler()
    _ch.setFormatter(_fmt)
    _fh = logging.FileHandler(LOG_DIR / "telegram_ai_listener.log")
    _fh.setFormatter(_fmt)
    logger.addHandler(_ch)
    logger.addHandler(_fh)
logger.setLevel(getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))

redis_client = None


async def connect_to_redis():
    global redis_client
    try:
        redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        await redis_client.ping()
        logger.info("✅ Redis connection established")
        return redis_client
    except Exception as exc:
        logger.critical(f"❌ Failed to connect to Redis: {exc}")
        return None


async def main():
    if not all([TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE]):
        logger.critical("Missing Telegram credentials in .env")
        return

    if AI_PROVIDER == "GEMINI" and not GEMINI_API_KEY:
        logger.critical("Missing GEMINI_API_KEY (provider=GEMINI)")
        return
    if AI_PROVIDER == "OPENAI" and not OPENAI_API_KEY:
        logger.critical("Missing OPENAI_API_KEY (provider=OPENAI)")
        return

    r = await connect_to_redis()
    if not r:
        return

    client = TelegramClient(str(TELEGRAM_SESSION_PATH), TELEGRAM_API_ID, TELEGRAM_API_HASH)
    try:
        await client.start(phone=TELEGRAM_PHONE)
        logger.info("✅ Telegram client connected")
    except Exception as exc:
        logger.critical(f"❌ Error starting Telegram: {exc}")
        return

    try:
        group = await resolve_entity(client, GROUP_ENTITY_NAME)
        if not group:
            logger.critical(f"Could not find group: {GROUP_ENTITY_NAME}")
            await client.disconnect()
            return
        logger.info(f"📡 Monitoring: {getattr(group, 'title', GROUP_ENTITY_NAME)} ({group.id})")
    except Exception as exc:
        logger.critical(f"❌ Error resolving group: {exc}")
        await client.disconnect()
        return

    api_key = GEMINI_API_KEY if AI_PROVIDER == "GEMINI" else OPENAI_API_KEY
    parser = AISignalParser(api_key=api_key, provider=AI_PROVIDER, model=AI_MODEL)
    dedupe_zset = os.getenv("REDIS_DEDUPE_KEY", "processed_signals_zset")
    max_dedupe = int(os.getenv("REDIS_MAX_DEDUPE", "5000"))

    @client.on(events.NewMessage(chats=group))
    async def signal_handler(event):
        listener_started_perf = now_perf_ns()
        received_fallback_ms = now_ms()
        telegram_received_at_ms = received_fallback_ms
        try:
            if getattr(event.message, "date", None):
                telegram_received_at_ms = int(event.message.date.timestamp() * 1000)
        except Exception:
            telegram_received_at_ms = received_fallback_ms

        trace_id = f"tg:{group.id}:{event.message.id}"
        message_text = event.message.text
        if not message_text:
            return

        prefilter_ms = now_ms() - telegram_received_at_ms
        if not parser.should_process(message_text):
            logger.debug("Message filtered (not a signal)")
            log_latency(
                logger,
                trace_id,
                "listener",
                telegram_received_to_prefilter_ms=max(prefilter_ms, 0),
                status="filtered",
                total_ms=duration_ms(listener_started_perf),
            )
            return

        msg_dedup_key = f"msg:{group.id}:{event.message.id}"
        msg_dedupe_started_perf = now_perf_ns()
        now_ms_pre = now_ms()
        try:
            msg_added = await r.zadd(dedupe_zset, {msg_dedup_key: now_ms_pre}, nx=True)
        except Exception:
            msg_added = 1
        msg_dedupe_ms = duration_ms(msg_dedupe_started_perf)
        if msg_added == 0:
            logger.info(f"Message already processed by another parser, skipping: {msg_dedup_key}")
            log_latency(
                logger,
                trace_id,
                "listener",
                telegram_received_to_prefilter_ms=max(prefilter_ms, 0),
                message_dedupe_ms=msg_dedupe_ms,
                status="message_duplicate",
                total_ms=duration_ms(listener_started_perf),
            )
            return

        try:
            ai_started_perf = now_perf_ns()
            signal = parser.parse(message_text)
            ai_parse_ms = duration_ms(ai_started_perf)
            if not signal:
                log_latency(
                    logger,
                    trace_id,
                    "listener",
                    telegram_received_to_prefilter_ms=max(prefilter_ms, 0),
                    message_dedupe_ms=msg_dedupe_ms,
                    ai_ms=ai_parse_ms,
                    status="ai_no_signal",
                    total_ms=duration_ms(listener_started_perf),
                )
                return

            ai_latency_ms = getattr(signal, "ai_latency_ms", None)
            if ai_latency_ms is not None:
                logger.info(f"⏱️ AI parse latency: {ai_latency_ms:.0f} ms")
            else:
                ai_latency_ms = ai_parse_ms

            now_ms_value = now_ms()
            signal_dedupe_started_perf = now_perf_ns()
            try:
                added = await r.zadd(dedupe_zset, {signal.signal_hash: now_ms_value}, nx=True)
            except Exception as exc:
                logger.error(f"Dedupe check failed: {exc}")
                added = 1
            signal_dedupe_ms = duration_ms(signal_dedupe_started_perf)

            if added == 0:
                logger.info(f"🔄 Duplicate detected, skipping: {signal.to_one_line()}")
                log_latency(
                    logger,
                    trace_id,
                    "listener",
                    telegram_received_to_prefilter_ms=max(prefilter_ms, 0),
                    message_dedupe_ms=msg_dedupe_ms,
                    ai_ms=ai_latency_ms,
                    signal_dedupe_ms=signal_dedupe_ms,
                    status="signal_duplicate",
                    total_ms=duration_ms(listener_started_perf),
                )
                return

            try:
                count = await r.zcard(dedupe_zset)
                if count > max_dedupe:
                    await r.zremrangebyrank(dedupe_zset, 0, int(count - max_dedupe) - 1)
            except Exception:
                pass

            cleaned = signal.to_one_line()
            listener_published_at_ms = now_ms()
            stream_payload = {
                "timestamp": str(int(time.time())),
                "message": cleaned,
                "hash": signal.signal_hash,
                "event_type": signal.event_type,
                "priority": "HIGH" if signal.event_type == "REENTRY" else "NORMAL",
                "action": signal.action,
                "instrument": signal.instrument,
                "strike": signal.strike,
                "option_type": signal.option_type,
                "entry_low": str(int(signal.entry_price[0])) if signal.entry_price else "",
                "entry_high": str(int(signal.entry_price[1])) if signal.entry_price else "",
                "stoploss": str(int(signal.stoploss)) if signal.stoploss is not None else "",
                "targets": "/".join(str(int(t)) for t in signal.targets) if signal.targets else "",
                "expiry": signal.expiry or "",
                "group_id": str(group.id),
                "message_id": str(event.message.id),
                "sender_id": str(event.message.sender_id or ""),
                "trace_id": trace_id,
                "telegram_received_at_ms": str(telegram_received_at_ms),
                "listener_published_at_ms": str(listener_published_at_ms),
                "ai_latency_ms": str(int(ai_latency_ms)),
                "source_message_id": str(event.message.id),
            }

            try:
                publish_started_perf = now_perf_ns()
                msgid = await r.xadd(REDIS_STREAM_KEY, stream_payload)
                redis_publish_ms = duration_ms(publish_started_perf)
                listener_total_ms = duration_ms(listener_started_perf)
                logger.info(f"✅ Published to {REDIS_STREAM_KEY}: {cleaned}")
                logger.info(f"   Redis ID: {msgid}")
                print(f"✅ SIGNAL PUBLISHED: {cleaned}")
                log_latency(
                    logger,
                    trace_id,
                    "listener",
                    telegram_received_to_prefilter_ms=max(prefilter_ms, 0),
                    message_dedupe_ms=msg_dedupe_ms,
                    ai_ms=int(ai_latency_ms),
                    signal_dedupe_ms=signal_dedupe_ms,
                    redis_publish_ms=redis_publish_ms,
                    total_ms=listener_total_ms,
                    message_id=event.message.id,
                    redis_message_id=msgid,
                    status="published",
                )
            except Exception as exc:
                logger.error(f"❌ Failed to publish to Redis: {exc}")
                logger.error(f"Raw message: {message_text}")
                logger.error(traceback.format_exc())
                log_latency(
                    logger,
                    trace_id,
                    "listener",
                    telegram_received_to_prefilter_ms=max(prefilter_ms, 0),
                    message_dedupe_ms=msg_dedupe_ms,
                    ai_ms=int(ai_latency_ms),
                    signal_dedupe_ms=signal_dedupe_ms,
                    total_ms=duration_ms(listener_started_perf),
                    status="publish_failed",
                )

            if OPENALGO_WEBHOOK_URL and OPENALGO_API_KEY:
                openalgo_payload = {
                    "instrument": stream_payload["instrument"],
                    "strike": stream_payload["strike"],
                    "option_type": stream_payload["option_type"],
                    "action": stream_payload["action"],
                    "entry_low": stream_payload["entry_low"],
                    "entry_high": stream_payload["entry_high"],
                    "stoploss": stream_payload["stoploss"],
                    "targets": stream_payload["targets"],
                    "expiry": stream_payload["expiry"],
                    "signal_hash": stream_payload["hash"],
                    "event_type": stream_payload["event_type"],
                }
                try:
                    webhook_url = f"{OPENALGO_WEBHOOK_URL}/{OPENALGO_API_KEY}"
                    async with httpx.AsyncClient(timeout=5.0) as client_http:
                        resp = await client_http.post(webhook_url, json=openalgo_payload)
                    logger.info(f"🔗 OpenAlgo webhook: {resp.status_code} {resp.text[:120]}")
                except Exception as exc:
                    logger.error(f"❌ OpenAlgo webhook call failed: {exc}")

        except Exception as exc:
            logger.error(f"Unexpected error in handler: {exc}")
            logger.error(traceback.format_exc())
            log_latency(
                logger,
                trace_id,
                "listener",
                total_ms=duration_ms(listener_started_perf),
                status="handler_exception",
            )

    logger.info("🚀 AI-Powered Telegram Gateway running...")
    logger.info("   Pre-filtering messages with regex")
    logger.info(f"   Parsing signals with {AI_PROVIDER} AI")
    logger.info("   Publishing to Redis stream")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Gateway shutting down")
    except Exception as exc:
        logger.critical(f"Fatal error: {exc}")
        logger.critical(traceback.format_exc())
