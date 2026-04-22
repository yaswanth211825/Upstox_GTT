"""Price Monitor — REST LTP polling → threshold detection → PG.

Polls Upstox v3/market-quote/ltp every POLL_INTERVAL seconds for all tracked
instrument keys, checks LTP against signal thresholds, auto-cancels GTTs when
entry is never filled but first target is hit.

Subscription management:
  - On startup: tracks all instrument keys with PENDING/ACTIVE signals.
  - On threshold refresh (every 60s): adds new keys, removes stale ones.
  - Instant add/remove via Redis pub/sub from trading engine.
  - On signal terminal (auto-cancel): removes key if no more active signals.
"""
import asyncio
import time
from datetime import datetime, timezone
import structlog

from shared.config import settings, configure_logging
from shared.db.postgres import create_pool, run_migrations
from shared.db.writer import AsyncDBWriter
from shared.redis.client import (
    create_redis,
    INSTRUMENT_SUBSCRIBE_CHANNEL,
    INSTRUMENT_UNSUBSCRIBE_CHANNEL,
)
from shared.upstox.client import UpstoxClient
from shared.heartbeat import heartbeat_loop

log = structlog.get_logger()

POLL_INTERVAL = 3   # seconds between LTP REST calls
LTP_REDIS_TTL = 10  # seconds — slightly longer than poll interval


def old_expiry_logic(trade: dict, ltp: float) -> bool:
    """Preserve legacy behavior: expire if first target is hit while still PENDING."""
    if trade.get("status") != "PENDING":
        return False
    t1 = trade.get("t1")
    if t1 is None:
        return False

    action = trade.get("action", "BUY")
    if action == "SELL":
        return ltp <= t1
    return ltp >= t1


def should_expire_trade(trade: dict, ltp: float, timestamp: datetime) -> bool:
    """Single expiry decision point for all entry-not-filled expiry checks."""
    if not settings.use_entry_touch_logic:
        log.debug("expiry_logic_path", signal_id=trade.get("id"), path="legacy")
        return old_expiry_logic(trade, ltp)

    log.debug("expiry_logic_path", signal_id=trade.get("id"), path="entry_touch")

    signal_timestamp = trade.get("signal_timestamp")
    if signal_timestamp and timestamp < signal_timestamp:
        return False

    if not trade.get("entry_range_touched", False):
        entry_low = trade.get("entry_low")
        entry_high = trade.get("entry_high")

        if entry_high is None:
            # BUY_ABOVE (no range given): synthesise entry_high = entry_low + 5.
            # Entry is "touched" once LTP comes within that upper bound.
            synthetic_high = (entry_low + 5) if entry_low is not None else None
            touched = synthetic_high is not None and ltp <= synthetic_high
        else:
            touched = entry_low is not None and entry_low <= ltp <= entry_high

        if touched:
            trade["entry_range_touched"] = True
            log.info("entry_range_touched", signal_id=trade.get("id"), ltp=ltp)

    if trade.get("entry_range_touched", False):
        t1 = trade.get("t1")
        entry_filled = bool(trade.get("entry_filled", False))
        if t1 is None:
            return False

        action = trade.get("action", "BUY")
        crossed_t1 = ltp < t1 if action == "SELL" else ltp > t1

        if not entry_filled and crossed_t1:
            log.info("trade_expired_via_new_logic", signal_id=trade.get("id"), ltp=ltp, t1=t1)
            return True

    return False


async def _set_entry_range_touched(conn, signal_id: int) -> None:
    await conn.execute(
        "UPDATE signals SET entry_range_touched = TRUE WHERE id = $1", signal_id
    )


async def _insert_price_event(conn, data: dict) -> None:
    await conn.execute(
        """
        INSERT INTO price_events (signal_id, instrument_key, ltp, event_type)
        VALUES ($1, $2, $3, $4)
        """,
        data.get("signal_id"),
        data["instrument_key"],
        data["ltp"],
        data["event_type"],
    )


class ThresholdChecker:
    """Check LTP against all active signal thresholds.

    Maintains its own set of tracked instrument keys. The polling loop reads
    `tracked_keys` and calls `check()` for each LTP received.
    """

    def __init__(self, pool, db_writer: AsyncDBWriter, redis, upstox: UpstoxClient):
        self._pool = pool
        self._db_writer = db_writer
        self._redis = redis
        self._upstox = upstox
        self._thresholds: dict[str, list[dict]] = {}   # instrument_key → signals
        self._tracked_keys: set[str] = set()
        self._last_refresh = 0.0

    @property
    def tracked_keys(self) -> set[str]:
        return self._tracked_keys

    def add_instrument(self, key: str) -> None:
        if key:
            self._tracked_keys.add(key)

    def remove_instrument(self, key: str) -> None:
        self._tracked_keys.discard(key)

    async def load_initial_keys(self) -> None:
        """On startup, load all instrument keys that have PENDING/ACTIVE signals."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT instrument_key FROM signals
                WHERE status IN ('PENDING','ACTIVE') AND instrument_key IS NOT NULL
                """
            )
        for r in rows:
            self._tracked_keys.add(r["instrument_key"])
        log.info("price_monitor_startup_keys", count=len(self._tracked_keys), keys=list(self._tracked_keys))

    async def _refresh_thresholds(self) -> None:
        if time.monotonic() - self._last_refresh < 60:
            return
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                  SELECT id, instrument_key, action, entry_low_adj, entry_high_adj,
                      stoploss_adj, targets_adj, status, gtt_order_ids, signal_at,
                      entry_range_touched
                FROM signals WHERE status IN ('PENDING','ACTIVE')
                  AND instrument_key IS NOT NULL
                """
            )

        new_thresholds: dict[str, list[dict]] = {}
        for r in rows:
            key = r["instrument_key"]
            sig = dict(r)
            existing = self._thresholds.get(key, [])
            existing_state = next((x for x in existing if x.get("id") == sig.get("id")), None)

            sig["t1"] = (sig.get("targets_adj") or [None])[0]
            sig["entry_low"] = sig.get("entry_low_adj")
            sig["entry_high"] = sig.get("entry_high_adj")
            sig["signal_timestamp"] = sig.get("signal_at")
            sig["entry_filled"] = sig.get("status") != "PENDING"
            # Load persisted state from DB; also carry forward any in-memory True from before refresh
            db_touched = bool(sig.get("entry_range_touched", False))
            mem_touched = bool(existing_state.get("entry_range_touched", False)) if existing_state else False
            sig["entry_range_touched"] = db_touched or mem_touched

            new_thresholds.setdefault(key, []).append(sig)

        # Sync tracked keys: add new, remove stale
        new_keys = set(new_thresholds.keys())
        old_keys = set(self._thresholds.keys())
        for k in new_keys - old_keys:
            self._tracked_keys.add(k)
        for k in old_keys - new_keys:
            self._tracked_keys.discard(k)

        self._thresholds = new_thresholds
        self._last_refresh = time.monotonic()
        log.debug("thresholds_refreshed", count=len(rows), tracked=len(self._tracked_keys))

    async def check(self, instrument_key: str, ltp: float, timestamp: datetime) -> None:
        await self._refresh_thresholds()
        for sig in list(self._thresholds.get(instrument_key, [])):
            await self._check_signal(sig, instrument_key, ltp, timestamp)

    async def _check_signal(self, sig: dict, key: str, ltp: float, timestamp: datetime) -> None:
        targets = sig.get("targets_adj") or []
        sl      = sig.get("stoploss_adj")
        action  = sig.get("action", "BUY")
        sig_id  = sig["id"]

        # Update entry_range_touched on every tick so the gate is set as soon as LTP
        # enters the entry zone, even before T1 is reached on a later tick.
        if settings.use_entry_touch_logic and not sig.get("entry_range_touched", False):
            entry_low = sig.get("entry_low")
            entry_high = sig.get("entry_high")
            if entry_high is None:
                synthetic_high = (entry_low + 5) if entry_low is not None else None
                _touched = synthetic_high is not None and ltp <= synthetic_high
            else:
                _touched = entry_low is not None and entry_low <= ltp <= entry_high
            if _touched:
                sig["entry_range_touched"] = True
                log.info("entry_range_touched", signal_id=sig_id, ltp=ltp)
                await self._db_writer.enqueue(_set_entry_range_touched, sig_id)

        if action == "BUY":
            for i, t in enumerate(targets):
                if ltp >= t:
                    await self._db_writer.enqueue(_insert_price_event, {
                        "signal_id": sig_id,
                        "instrument_key": key,
                        "ltp": ltp,
                        "event_type": f"TARGET{i+1}_REACHED",
                    })
                    log.info("price_target_reached", signal_id=sig_id, ltp=ltp, target=t)
                    if i == 0 and should_expire_trade(sig, ltp, timestamp):
                        await self._cancel_unfilled_gtts(sig, ltp, t)
                    return

            if sl and ltp <= sl:
                await self._db_writer.enqueue(_insert_price_event, {
                    "signal_id": sig_id,
                    "instrument_key": key,
                    "ltp": ltp,
                    "event_type": "STOPLOSS_REACHED",
                })

        elif action == "SELL":
            for i, t in enumerate(targets):
                if ltp <= t:
                    await self._db_writer.enqueue(_insert_price_event, {
                        "signal_id": sig_id,
                        "instrument_key": key,
                        "ltp": ltp,
                        "event_type": f"TARGET{i+1}_REACHED",
                    })
                    log.info("price_target_reached", signal_id=sig_id, ltp=ltp, target=t)
                    if i == 0 and should_expire_trade(sig, ltp, timestamp):
                        await self._cancel_unfilled_gtts(sig, ltp, t)
                    return

            if sl and ltp >= sl:
                await self._db_writer.enqueue(_insert_price_event, {
                    "signal_id": sig_id,
                    "instrument_key": key,
                    "ltp": ltp,
                    "event_type": "STOPLOSS_REACHED",
                })

    async def _cancel_unfilled_gtts(self, sig: dict, ltp: float, target: float) -> None:
        signal_id = sig["id"]
        instr_key = sig.get("instrument_key", "")
        gtt_ids: list[str] = list(sig.get("gtt_order_ids") or [])

        if not gtt_ids:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT gtt_order_id FROM gtt_rules WHERE signal_id = $1 AND status = 'PENDING'",
                    signal_id,
                )
            gtt_ids = [r["gtt_order_id"] for r in rows]

        cancelled = []
        for gtt_id in gtt_ids:
            try:
                await self._upstox.cancel_gtt(gtt_id)
                cancelled.append(gtt_id)
                log.info("auto_cancel_entry_not_filled", gtt_id=gtt_id, signal_id=signal_id, ltp=ltp, target=target)
            except Exception as e:
                log.warning("auto_cancel_failed", gtt_id=gtt_id, error=str(e))

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE signals
                SET status = 'EXPIRED',
                    block_reason = $2,
                    resolved_at  = now(),
                    last_event_at = now()
                WHERE id = $1 AND status = 'PENDING'
                """,
                signal_id,
                f"entry_not_filled_target_hit:ltp={ltp:.2f}:target={target:.2f}",
            )

        # Evict from local threshold cache
        if instr_key in self._thresholds:
            self._thresholds[instr_key] = [
                s for s in self._thresholds[instr_key] if s["id"] != signal_id
            ]
            if not self._thresholds[instr_key]:
                del self._thresholds[instr_key]
                self._tracked_keys.discard(instr_key)
                log.info("price_monitor_stopped_tracking", instrument_key=instr_key)

        log.info(
            "auto_cancel_complete",
            signal_id=signal_id,
            ltp=ltp,
            target=target,
            cancelled_gtts=cancelled,
        )
        print(
            f"[AUTO-CANCEL] Signal {signal_id} — "
            f"LTP {ltp:.2f} hit T1 {target:.2f} with entry NOT filled. "
            f"Cancelled GTTs: {cancelled}"
        )


async def _ltp_poll_loop(checker: ThresholdChecker, upstox: UpstoxClient, redis) -> None:
    """Poll Upstox v3 LTP REST endpoint for all tracked instruments every POLL_INTERVAL s."""
    while True:
        keys = list(checker.tracked_keys)
        if keys:
            try:
                ltp_data = await upstox.get_ltp_multi(keys)
                if ltp_data:
                    log.debug("ltp_poll_result", count=len(ltp_data))
                    for key, ltp in ltp_data.items():
                        await redis.set(f"ltp:{key}", str(ltp), ex=LTP_REDIS_TTL)
                        await checker.check(key, ltp, datetime.now(timezone.utc))
                else:
                    log.debug("ltp_poll_empty", keys=keys)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("ltp_poll_error", error=str(e))
        await asyncio.sleep(POLL_INTERVAL)


async def main() -> None:
    configure_logging("price-monitor")
    log.info("price_monitor_starting")

    pool = await create_pool(settings.postgres_dsn)
    await run_migrations(pool)
    redis = await create_redis(settings.redis_host, settings.redis_port)

    upstox = UpstoxClient(settings.upstox_access_token, settings.upstox_base_url)
    db_writer = AsyncDBWriter(pool)

    checker = ThresholdChecker(pool, db_writer, redis, upstox)
    await checker.load_initial_keys()

    # Listen for instant add/remove notifications from trading engine via Redis pub/sub
    async def _pubsub_listener() -> None:
        import redis.asyncio as aioredis
        pubsub_conn = aioredis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            decode_responses=True,
        )
        pubsub = pubsub_conn.pubsub()
        await pubsub.subscribe(INSTRUMENT_SUBSCRIBE_CHANNEL, INSTRUMENT_UNSUBSCRIBE_CHANNEL)
        log.info("price_monitor_pubsub_listening")
        try:
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg.get("type") == "message":
                    instrument_key = msg["data"]
                    channel = msg["channel"]
                    if channel == INSTRUMENT_SUBSCRIBE_CHANNEL:
                        checker.add_instrument(instrument_key)
                        log.info("pubsub_tracking_added", instrument_key=instrument_key)
                    elif channel == INSTRUMENT_UNSUBSCRIBE_CHANNEL:
                        checker.remove_instrument(instrument_key)
                        log.info("pubsub_tracking_removed", instrument_key=instrument_key)
                else:
                    await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe()
            await pubsub_conn.aclose()

    writer_task  = asyncio.create_task(db_writer.run())
    pubsub_task  = asyncio.create_task(_pubsub_listener())
    poll_task    = asyncio.create_task(_ltp_poll_loop(checker, upstox, redis))
    asyncio.create_task(heartbeat_loop())
    try:
        # Run until cancelled or poll task crashes
        await asyncio.gather(poll_task, pubsub_task, return_exceptions=False)
    except Exception as e:
        log.error("price_monitor_crash", error=str(e))
    finally:
        pubsub_task.cancel()
        poll_task.cancel()
        await db_writer.stop()
        writer_task.cancel()
        await upstox.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
