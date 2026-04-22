"""Async Telegram listener — pre-filter → rolling context → AI parse → dedup → Redis Stream."""
import asyncio
import sys
import os
import re
import time
import structlog
from telethon import TelegramClient, events

from shared.config import settings, new_trace_id
from shared.redis.client import stream_add
from shared.redis.pipeline_log import log_event as _plog

log = structlog.get_logger()

# Pre-filter: messages matching these patterns are noise — skip AI entirely
_SKIP_PATTERNS = (
    "ORDER ACTIVATED",
    "POINTS DONE",
    "SL HIT",
    "TARGET DONE",
    "TARGET HIT",
    "BOOK PARTIAL",
    "BOOKING",
    "DONE ✅",
    "✅ DONE",
)

CONTEXT_WINDOW_SIZE = 10
CONTEXT_TTL = 1800   # 30 minutes

# Regex patterns for trader profile flags — require word boundaries to avoid false positives
_SAFE_ONLY_RE = re.compile(
    r'\bSAFE\s+(TRADERS?|ONLY)\b|'
    r'\bFOR\s+SAFE\s+TRADERS?\b|'
    r'\bONLY\s+FOR\s+SAFE\b',
    re.IGNORECASE,
)
_RISK_ONLY_RE = re.compile(
    r'\bRISK\s+ONLY\b|\bRISKY\s+TRADERS?\b|\bJACKPOT\b',
    re.IGNORECASE,
)


def _should_skip(text: str) -> bool:
    upper = text.upper()
    return any(p in upper for p in _SKIP_PATTERNS)


async def _update_context(redis, chat_id: int, text: str) -> list[str]:
    key = f"msg_context:{chat_id}"
    await redis.lpush(key, text)
    await redis.ltrim(key, 0, CONTEXT_WINDOW_SIZE - 1)
    await redis.expire(key, CONTEXT_TTL)
    # Return last 5 for AI context (oldest first)
    recent = await redis.lrange(key, 0, 4)
    return list(reversed(recent))


async def _level1_dedup(redis, chat_id: int, msg_id: int) -> bool:
    """Returns True if this message should be processed (not a duplicate)."""
    key = f"signal_msg:{chat_id}:{msg_id}"
    return bool(await redis.set(key, "1", nx=True, ex=86_400))



class SignalIngestor:
    def __init__(self, redis, stream: str, parser):
        self._redis = redis
        self._stream = stream
        self._parser = parser

    async def handle_message(self, chat_id: int, msg_id: int, text: str) -> None:
        trace_id = new_trace_id()
        logger = log.bind(trace_id=trace_id, chat_id=chat_id, msg_id=msg_id)

        if not text or not text.strip():
            return

        snippet = text[:120].replace("\n", " ")

        await _plog(self._redis, "telegram_received", "info",
                    f"Message received from group {chat_id} (msg #{msg_id}): {snippet}",
                    trace_id=trace_id)

        # Level-1 dedup
        if not await _level1_dedup(self._redis, chat_id, msg_id):
            logger.debug("msg_duplicate_skipped")
            await _plog(self._redis, "dedup_skip", "info",
                        f"Message #{msg_id} already processed — skipped (duplicate Telegram delivery).",
                        trace_id=trace_id)
            return

        # Pre-filter noise
        if _should_skip(text):
            logger.debug("msg_prefiltered", text=text[:80])
            await _plog(self._redis, "prefilter_skip", "info",
                        f"Message filtered out — matched a noise pattern (e.g. ORDER ACTIVATED / SL HIT / TARGET DONE). Not a new signal.",
                        trace_id=trace_id)
            return

        # Update rolling context window (for future multi-message context; parser still sees full text)
        await _update_context(self._redis, chat_id, text)

        await _plog(self._redis, "ai_parsing", "info",
                    f"Sending message to AI parser to extract signal details…",
                    trace_id=trace_id)

        # AI parse — parser.parse() is sync, run in thread pool
        try:
            loop = asyncio.get_event_loop()
            signal = await loop.run_in_executor(None, self._parser.parse, text)
        except Exception as e:
            logger.error("ai_parse_error", error=str(e))
            await _plog(self._redis, "ai_parse_error", "error",
                        f"AI parser threw an exception and could not process the message: {e}",
                        trace_id=trace_id)
            return

        if not signal:
            logger.debug("ai_no_signal", text=text[:80])
            await _plog(self._redis, "ai_no_signal", "info",
                        "AI determined this message is not a trading signal — no action taken.",
                        trace_id=trace_id)
            return

        # Detect safe_only / risk_only from message text using word-boundary regex
        safe_only = bool(_SAFE_ONLY_RE.search(text))
        risk_only = bool(_RISK_ONLY_RE.search(text))

        # Build stream payload from TradingSignal dataclass
        entry_low = signal.entry_price[0] if signal.entry_price else None
        entry_high = signal.entry_price[1] if signal.entry_price and signal.entry_price[1] != signal.entry_price[0] else None
        average = getattr(signal, "average", None)

        payload = {
            "action":       signal.action,
            "instrument":   signal.instrument,
            "strike":       str(signal.strike),
            "option_type":  signal.option_type,
            "entry_low":    str(entry_low or ""),
            "entry_high":   str(entry_high or ""),
            "stoploss":     str(signal.stoploss or ""),
            "targets":      "/".join(str(t) for t in signal.targets),
            "average":      str(average or ""),
            "expiry":       signal.expiry or "",
            "safe_only":    "1" if safe_only else "0",
            "risk_only":    "1" if risk_only else "0",
            "group_id":     str(chat_id),
            "message_id":   str(msg_id),
            "trace_id":     trace_id,
            "timestamp":    str(int(time.time())),
        }

        msg_stream_id = await stream_add(self._redis, self._stream, payload)
        logger.info("signal_published", stream_id=msg_stream_id, instrument=payload["instrument"])

        instr_label = f"{signal.instrument} {signal.strike or ''}{signal.option_type or ''}".strip()
        flags = []
        if safe_only: flags.append("SAFE ONLY")
        if risk_only: flags.append("RISK ONLY")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        await _plog(self._redis, "signal_queued", "success",
                    f"Signal parsed and queued for trading engine — {instr_label} "
                    f"{signal.action} | Entry: {entry_low}{f'–{entry_high}' if entry_high else ''} "
                    f"| SL: {signal.stoploss} | Targets: {'/'.join(str(t) for t in signal.targets)}{flag_str}",
                    signal=instr_label, trace_id=trace_id)


async def run_listener(redis, parser) -> None:
    session_path = "data/session_name.session"
    if not os.path.exists(session_path):
        log.error(
            "telegram_session_missing",
            path=session_path,
            hint=(
                "The Telegram session file does not exist. "
                "Run the entity_finder script locally first to authenticate, "
                "then copy data/session_name.session to the server's data/ directory."
            ),
        )
        raise FileNotFoundError(
            f"Telegram session file not found: {session_path}. "
            "Authenticate locally and copy the session file to the server."
        )

    client = TelegramClient(
        "data/session_name",   # reuse existing authenticated session
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    await client.start(phone=settings.telegram_phone)
    log.info("telegram_connected")

    # Build list of channels to watch (filter out empty)
    channel_names = [settings.group_entity_name]
    if settings.group_entity_name_2:
        channel_names.append(settings.group_entity_name_2)

    entities = []
    for name in channel_names:
        try:
            entity = await _resolve_group(client, name)
            entities.append(entity)
            log.info("telegram_channel_resolved", channel=name)
        except Exception as e:
            log.error("telegram_channel_failed", channel=name, error=str(e))

    if not entities:
        raise RuntimeError("No Telegram channels could be resolved")

    ingestor = SignalIngestor(redis, settings.redis_stream_name, parser)

    @client.on(events.NewMessage(chats=entities))
    async def _on_message(event):
        try:
            await ingestor.handle_message(
                chat_id=event.chat_id,
                msg_id=event.message.id,
                text=event.message.text or "",
            )
        except Exception as e:
            log.error("telegram_handler_error", error=str(e))

    log.info("telegram_listening", channels=channel_names)

    # Keep-alive: ping Telegram every 5 minutes to prevent silent connection drops
    async def _keepalive():
        while True:
            await asyncio.sleep(300)
            try:
                await client.get_me()
                log.debug("telegram_keepalive_ok")
            except Exception as e:
                log.warning("telegram_keepalive_failed", error=str(e))

    asyncio.create_task(_keepalive())
    await client.run_until_disconnected()


async def _resolve_group(client: TelegramClient, name: str):
    # Try entity_finder from v1 if available
    try:
        v1_root = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
        sys.path.insert(0, os.path.abspath(v1_root))
        from entity_finder import resolve_entity  # type: ignore
        return await resolve_entity(client, name)
    except ImportError:
        pass
    # Try direct username lookup (works for public groups/channels)
    username = name.lstrip("@")
    try:
        return await client.get_entity(username)
    except Exception:
        pass
    # Fall back to iterating dialogs (works for private groups)
    async for dialog in client.iter_dialogs():
        if name.lower() in dialog.name.lower():
            return dialog.entity
    raise RuntimeError(f"Could not find Telegram group: {name!r}. Make sure the account has joined or messaged in this group.")
