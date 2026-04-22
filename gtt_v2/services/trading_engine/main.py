"""Trading Engine — Redis Stream consumer → full Trading Floor rules → GTT placement."""
import asyncio
import json
import os
import time
import structlog
import asyncpg
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from shared.config import settings, configure_logging, new_trace_id
from shared.db.postgres import create_pool, run_migrations
from shared.db.signals import insert_signal, update_signal_status, compute_signal_hash
from shared.db.gtt_rules import upsert_gtt_rule
from shared.db.writer import AsyncDBWriter
from shared.redis.client import create_redis, ensure_consumer_group, stream_read_group, stream_ack, stream_reclaim_pending, notify_subscribe_instrument
from shared.redis.rate_limiter import acquire as rate_acquire
from shared.signal.parser import parse_message_to_signal
from shared.signal.types import RawSignal, AdjustedSignal
from shared.rules.buffer import apply_buffer
from shared.rules.trade_type import detect_trade_type
from shared.rules.timing import MarketTimingGate
from shared.rules.pnl_guard import DailyPnLGuard
# split_lots removed — single fixed-quantity order per signal (DEFAULT_QUANTITY env var)
from shared.rules.missed_entry import check_missed_entry
from shared.instruments.cache import InstrumentCache, InstrumentNotFound
from shared.instruments.loader import download_instruments
from shared.upstox.client import UpstoxClient, CircuitOpenError, MaxRetriesError, UpstoxRejectError
from shared.upstox.gtt_builder import build_gtt_payload, resolve_lot_size
from shared.heartbeat import heartbeat_loop
from shared.redis.pipeline_log import log_event as _plog

log = structlog.get_logger()

CONSUMER_GROUP = "trading-engine-cg"
CONSUMER_NAME  = f"trading-engine-{os.getpid()}"


async def process_signal(
    msg_id: str,
    fields: dict,
    upstox: UpstoxClient,
    instrument_cache: InstrumentCache,
    db_writer: AsyncDBWriter,
    redis,
    pool: asyncpg.Pool,
    timing_gate: MarketTimingGate,
    pnl_guard: DailyPnLGuard,
) -> None:
    trace_id = new_trace_id()
    t_start = time.monotonic()
    logger = log.bind(trace_id=trace_id, msg_id=msg_id)

    # ── 1. Parse ─────────────────────────────────────────────────────────────
    signal = parse_message_to_signal(fields, redis_message_id=msg_id)
    if signal is None:
        logger.warning("signal_parse_failed", fields=fields)
        print(f"[REJECTED] Could not parse signal from stream message {msg_id} — required fields missing or malformed.")
        await _plog(redis, "parse_failed", "error",
                    "Trading engine could not parse the signal — required fields (action, entry, SL, targets) are missing or malformed.",
                    trace_id=trace_id)
        return

    signal_hash = compute_signal_hash(
        signal.underlying,
        signal.strike,
        signal.option_type,
        signal.entry_low,
        signal.stoploss,
        signal.targets,
        str(signal.expiry) if signal.expiry else None,
    )
    signal = signal.model_copy(update={"signal_hash": signal_hash})

    instr_label = f"{signal.underlying} {signal.strike or ''}{signal.option_type or ''}".strip()
    logger = logger.bind(
        instrument=instr_label,
        action=signal.action,
    )
    await _plog(redis, "engine_received", "info",
                f"Trading engine picked up signal: {instr_label} {signal.action}",
                signal=instr_label, trace_id=trace_id)

    # ── 2. Trader profile filter ─── DISABLED FOR NOW — will re-enable later ─
    # All signals are accepted regardless of safe_only / risk_only flags.
    # To re-enable, uncomment the block below.
    # profile = settings.trader_profile.upper()
    # if signal.safe_only and profile == "RISK":
    #     logger.info("signal_skipped_safe_only")
    #     print(f"[SKIPPED] {signal.underlying} {signal.strike}{signal.option_type} — signal is marked SAFE ONLY but this account is RISK profile.")
    #     return
    # if signal.risk_only and profile == "SAFE":
    #     logger.info("signal_skipped_risk_only")
    #     print(f"[SKIPPED] {signal.underlying} {signal.strike}{signal.option_type} — signal is marked RISK ONLY but this account is SAFE profile.")
    #     return
    profile = settings.trader_profile.upper()   # used by timing gate (SAFE blocks before 9:30)

    # ── 3. Timing gate ────────────────────────────────────────────────────────
    _TIMING_REASONS = {
        "before_market_open": "market has not opened yet (opens at 9:15 AM IST)",
        "safe_before_9_30":   "SAFE profile cannot trade before 9:30 AM IST",
        "after_entry_close":  "market entry window is closed (after 3:30 PM IST)",
        "weekend":            "market is closed on weekends",
    }
    if not settings.dry_run:
        ok, reason = timing_gate.check(profile)
        if not ok:
            human = _TIMING_REASONS.get(reason, reason)
            logger.info("signal_blocked_timing", reason=reason)
            print(f"[BLOCKED] {signal.underlying} {signal.strike}{signal.option_type} — Timing gate: {human}.")
            await _plog(redis, "timing_blocked", "warning",
                        f"Blocked — {instr_label}: {human}",
                        signal=instr_label, trace_id=trace_id)
            await _block_signal(db_writer, signal, f"timing:{reason}")
            return

    # ── 4. Daily P&L guard ────────────────────────────────────────────────────
    if not await pnl_guard.allow(redis):
        logger.warning("signal_blocked_pnl_limit")
        print(f"[BLOCKED] {signal.underlying} {signal.strike}{signal.option_type} — Daily P&L limit reached (10% of capital). No more trades today.")
        await _plog(redis, "pnl_limit_blocked", "warning",
                    f"Blocked — {instr_label}: Daily P&L limit reached (±10% of capital). No more trades will be placed today.",
                    signal=instr_label, trace_id=trace_id)
        await _block_signal(db_writer, signal, "daily_pnl_limit")
        return

    # ── 4b. Targets required guard ───────────────────────────────────────────
    if not signal.targets:
        logger.warning("signal_blocked_no_targets")
        print(
            f"[BLOCKED] {signal.underlying} {signal.strike}{signal.option_type} — "
            "Signal has no targets. Cannot place GTT without at least one target."
        )
        await _plog(redis, "no_targets_blocked", "warning",
                    f"Blocked — {instr_label}: Signal has no target prices. GTT requires at least one target to place the exit leg.",
                    signal=instr_label, trace_id=trace_id)
        await _block_signal(db_writer, signal, "no_targets")
        return

    # ── 5. Buffer + trade type ────────────────────────────────────────────────
    trade_type = detect_trade_type(signal)
    signal = signal.model_copy(update={"trade_type": trade_type})
    adj = apply_buffer(signal)

    # ── 5b. Risk-Reward ratio check (minimum 1:2, raw signal prices only) ────
    # Uses signal.entry_low (raw) — never the midpoint or any adjusted price.
    # Buffer is an execution detail and must not affect R:R gating.
    if signal.targets:
        t1_raw = signal.targets[0]
        if signal.action == "BUY":
            risk   = signal.entry_low - signal.stoploss
            reward = t1_raw - signal.entry_low
        else:
            risk   = signal.stoploss - signal.entry_low
            reward = signal.entry_low - t1_raw
        if risk > 0 and (reward / risk) < 2.0:
            rr_str = f"{reward / risk:.2f}"
            logger.info("signal_blocked_rr", rr=rr_str, risk=risk, reward=reward)
            print(
                f"[BLOCKED] {signal.underlying} {signal.strike}{signal.option_type} — "
                f"R:R {rr_str} (raw entry={signal.entry_low}, "
                f"risk={risk:.1f} pts, reward={reward:.1f} pts). Minimum 1:2."
            )
            await _plog(redis, "rr_blocked", "warning",
                        f"Blocked — {instr_label}: Risk:Reward ratio is {rr_str} "
                        f"(risk={risk:.1f} pts, reward={reward:.1f} pts). Minimum required is 1:2. "
                        f"The target is too close to the entry price.",
                        signal=instr_label, trace_id=trace_id)
            await _block_signal(db_writer, signal, f"rr_ratio:{rr_str}")
            return

    # ── 6. Instrument lookup ──────────────────────────────────────────────────
    expiry_ymd = str(signal.expiry) if signal.expiry else ""
    try:
        instrument = await instrument_cache.resolve(
            signal.underlying, signal.strike or 0, signal.option_type or "", expiry_ymd
        )
    except InstrumentNotFound:
        logger.error("instrument_not_found")
        print(
            f"[BLOCKED] {signal.underlying} {signal.strike}{signal.option_type} — "
            f"Instrument not found in Upstox for expiry {expiry_ymd}. "
            f"The strike may not exist or the expiry date is wrong."
        )
        await _plog(redis, "instrument_not_found", "error",
                    f"Blocked — {instr_label}: Instrument not found in Upstox master for expiry {expiry_ymd or '(none)'}. "
                    f"The strike may not exist yet or the expiry date in the signal is wrong.",
                    signal=instr_label, trace_id=trace_id)
        await _block_signal(db_writer, signal, "instrument_not_found")
        return

    instrument_key = instrument.get("instrument_key", "")
    adj = adj.model_copy(update={"instrument_key": instrument_key})
    signal = signal.model_copy(update={"instrument_key": instrument_key})

    # ── 7. Missed entry guard ─────────────────────────────────────────────────
    can_place, miss_reason = await check_missed_entry(adj, redis)
    if not can_place:
        logger.warning("signal_blocked_missed_entry", reason=miss_reason)
        print(f"[BLOCKED] {signal.underlying} {signal.strike}{signal.option_type} — Missed entry: {miss_reason}. Price has already moved past the target or stoploss before GTT could be placed.")
        await _plog(redis, "missed_entry_blocked", "warning",
                    f"Blocked — {instr_label}: Missed entry — price has already moved past the entry zone by the time the signal was processed. Detail: {miss_reason}",
                    signal=instr_label, trace_id=trace_id)
        await _block_signal(db_writer, signal, f"missed_entry:{miss_reason}")
        return

    # ── 8. Insert signal row (Level-2 dedup via UNIQUE signal_hash) ───────────
    lot_size = resolve_lot_size(signal.underlying, instrument)
    signal_data = _signal_to_dict(signal, adj, lot_size)
    async with pool.acquire() as conn:
        signal_id = await insert_signal(conn, signal_data)
    if signal_id is None:
        logger.info("signal_dedup_skipped")
        print(f"[SKIPPED] {signal.underlying} {signal.strike}{signal.option_type} — Duplicate signal, already processed before (same instrument/strike/entry/SL/targets).")
        await _plog(redis, "duplicate_signal", "info",
                    f"Duplicate — {instr_label}: Identical signal already exists in the database (same strike, entry, SL, and targets). Skipped to avoid placing the same GTT twice.",
                    signal=instr_label, trace_id=trace_id)
        return

    logger = logger.bind(signal_id=signal_id)

    # ── 9. Build single GTT order ─────────────────────────────────────────────
    # Quantity = DEFAULT_QUANTITY (env, default 1 lot) * lot_size (per instrument)
    # One signal = one GTT order, no splitting.
    target_adj  = adj.targets_adj[0] if adj.targets_adj else adj.entry_price
    quantity    = settings.default_quantity * lot_size

    # ── 10. Rate limit check ──────────────────────────────────────────────────
    if not await rate_acquire(redis, "gtt_place"):
        logger.warning("signal_rate_limited_gtt_place")
        print(f"[BLOCKED] {signal.underlying} {signal.strike}{signal.option_type} — Rate limit hit. Will retry automatically.")
        await _plog(redis, "rate_limited", "warning",
                    f"Rate limit — {instr_label}: Too many GTT orders placed in a short window. This signal was dropped. Will need to be re-sent.",
                    signal=instr_label, trace_id=trace_id)
        await db_writer.enqueue(update_signal_status, signal_id, "FAILED", "rate_limited")
        return

    # ── 11. Place single GTT ──────────────────────────────────────────────────
    payload = build_gtt_payload(
        instrument_token=instrument_key,
        action=signal.action,
        product=signal.product,
        entry_price=adj.entry_price,   # midpoint for RANGING, exact for BUY_ABOVE
        quantity=quantity,
        stoploss_adj=adj.stoploss_adj,
        target_adj=target_adj,
    )
    gtt_ids = []
    try:
        if settings.dry_run:
            logger.info("dry_run_gtt", payload=payload)
            gtt_ids.append(f"dry-run-{new_trace_id()}")
            await _plog(redis, "gtt_placed", "success",
                        f"[DRY RUN] GTT would be placed — {instr_label} {signal.action} "
                        f"| Entry: {adj.entry_price} | SL: {adj.stoploss_adj} | T1: {target_adj} | Qty: {quantity}",
                        signal=instr_label, trace_id=trace_id)
        else:
            resp = await upstox.place_gtt(payload)
            gtt_ids = resp.get("data", {}).get("gtt_order_ids", [])
            if not gtt_ids:
                logger.error("gtt_no_ids_returned", payload=payload)
                print(f"[FAILED] {signal.underlying} {signal.strike}{signal.option_type} — Upstox returned no GTT IDs. Marking FAILED.")
                await _plog(redis, "gtt_failed", "error",
                            f"Failed — {instr_label}: Upstox accepted the request but returned no GTT IDs. The order may not have been created. Signal marked FAILED.",
                            signal=instr_label, trace_id=trace_id)
                await db_writer.enqueue(update_signal_status, signal_id, "FAILED", "no_gtt_ids_returned")
                return
            logger.info("gtt_placed", gtt_ids=gtt_ids)
            print(
                f"[GTT PLACED] {signal.underlying} {signal.strike}{signal.option_type} — "
                f"Entry: {adj.entry_price}, SL: {adj.stoploss_adj}, T1: {target_adj} | "
                f"qty: {quantity} | GTT IDs: {gtt_ids}"
            )
            await _plog(redis, "gtt_placed", "success",
                        f"GTT placed on Upstox — {instr_label} {signal.action} "
                        f"| Entry: {adj.entry_price} | SL: {adj.stoploss_adj} | T1: {target_adj} "
                        f"| Qty: {quantity} | GTT IDs: {', '.join(str(g) for g in gtt_ids)}",
                        signal=instr_label, trace_id=trace_id)
            for gid in gtt_ids:
                for strategy, trigger_type, trigger_price in (
                    ("ENTRY", "ABOVE" if signal.action == "BUY" else "BELOW", adj.entry_price),
                    ("TARGET", "IMMEDIATE", target_adj),
                    ("STOPLOSS", "IMMEDIATE", adj.stoploss_adj),
                ):
                    await db_writer.enqueue(upsert_gtt_rule, {
                        "signal_id": signal_id,
                        "gtt_order_id": gid,
                        "strategy": strategy,
                        "trigger_type": trigger_type,
                        "trigger_price": trigger_price,
                        "transaction_type": signal.action,
                        "status": "PENDING",
                        "payload_json": json.dumps(payload),
                    })
    except UpstoxRejectError as e:
        reject_msg = str(e)
        logger.error("gtt_rejected_by_upstox", error=reject_msg)
        print(f"[REJECTED BY UPSTOX] {signal.underlying} {signal.strike}{signal.option_type} — {reject_msg}")
        await _plog(redis, "gtt_rejected", "error",
                    f"Upstox rejected GTT — {instr_label}: {reject_msg}",
                    signal=instr_label, trace_id=trace_id)
        await db_writer.enqueue(update_signal_status, signal_id, "FAILED", f"upstox_reject:{reject_msg}")
        return
    except (CircuitOpenError, MaxRetriesError) as e:
        logger.error("gtt_place_failed", error=str(e))
        print(f"[FAILED] {signal.underlying} {signal.strike}{signal.option_type} — Upstox API error: {e}")
        await _plog(redis, "gtt_api_error", "error",
                    f"Upstox API error while placing GTT for {instr_label}: {e}",
                    signal=instr_label, trace_id=trace_id)
        await db_writer.enqueue(update_signal_status, signal_id, "FAILED", str(e))
        return

    latency_ms = round((time.monotonic() - t_start) * 1000)
    logger.info(
        "signal_processed",
        trade_type=trade_type,
        entry_adj=adj.entry_low_adj,
        sl_adj=adj.stoploss_adj,
        targets_adj=adj.targets_adj,
        gtt_ids=gtt_ids,
        latency_ms=latency_ms,
    )

    # Stay PENDING — order-tracker moves to ACTIVE when entry leg triggers on Upstox
    await db_writer.enqueue(
        update_signal_status, signal_id, "PENDING",
        None, gtt_ids, adj.ltp_at_check
    )

    # Notify price monitor immediately — no need to wait for its 60s refresh cycle
    await notify_subscribe_instrument(redis, instrument_key)


async def _block_signal(
    db_writer: AsyncDBWriter, signal: RawSignal, reason: str
) -> None:
    data = _signal_to_dict(signal, None, 1)
    data["status"] = "BLOCKED"

    async def _do(conn, d):
        sid = await insert_signal(conn, d)
        if sid:
            await update_signal_status(conn, sid, "BLOCKED", reason)

    await db_writer.enqueue(_do, data)


def _signal_to_dict(
    signal: RawSignal, adj: AdjustedSignal | None, lot_size: int
) -> dict:
    return {
        "redis_message_id": signal.redis_message_id,
        "signal_hash": signal.signal_hash,
        "action": signal.action,
        "underlying": signal.underlying,
        "strike": signal.strike,
        "option_type": signal.option_type,
        "expiry": signal.expiry,
        "product": signal.product,
        "quantity_lots": signal.quantity_lots,
        "instrument_key": signal.instrument_key,
        "entry_low_raw": signal.entry_low,
        "entry_high_raw": signal.entry_high,
        "stoploss_raw": signal.stoploss,
        "targets_raw": signal.targets or [],
        "entry_low_adj": adj.entry_price if adj else None,   # actual GTT trigger (midpoint for RANGING)
        "entry_high_adj": adj.entry_high_adj if adj else None,
        "stoploss_adj": adj.stoploss_adj if adj else None,
        "targets_adj": adj.targets_adj if adj else [],
        "trade_type": signal.trade_type,
        "safe_only": signal.safe_only,
        "risk_only": signal.risk_only,
        "status": "PENDING",
    }


async def reconcile_stale(
    pool: asyncpg.Pool,
    upstox: UpstoxClient,
    db_writer: AsyncDBWriter,
) -> None:
    """Cancel GTTs older than GTT_EXPIRY_HOURS and expire them in DB."""
    from shared.db.signals import get_signals_older_than
    async with pool.acquire() as conn:
        stale = await get_signals_older_than(conn, settings.gtt_expiry_hours)
    for s in stale:
        for gtt_id in (s.get("gtt_order_ids") or []):
            try:
                await upstox.cancel_gtt(gtt_id)
            except Exception as e:
                log.warning("stale_gtt_cancel_failed", gtt_id=gtt_id, error=str(e))
        await db_writer.enqueue(update_signal_status, s["id"], "EXPIRED", "stale_expiry")
        log.info("stale_signal_expired", signal_id=s["id"])


_IST = ZoneInfo("Asia/Kolkata")
_MARKET_OPEN_HOUR   = 9
_MARKET_OPEN_MINUTE = 15


async def _fetch_and_cache_capital(upstox: UpstoxClient, redis) -> None:
    """Fetch available equity margin from Upstox and cache in Redis as daily_capital:{date}."""
    try:
        data = await upstox.get_funds_and_margin(segment="SEC")
        # Response is a list of segment dicts: [{segment, available_margin, used_margin, ...}]
        available: float | None = None
        if isinstance(data, list):
            for item in data:
                seg = str(item.get("segment") or "").upper()
                if seg in ("SEC", "EQ", "EQUITY"):
                    available = float(item.get("available_margin") or 0)
                    break
        elif isinstance(data, dict):
            # Some Upstox versions wrap in {"equity": {...}}
            eq = data.get("equity") or data
            available = float(eq.get("available_margin") or 0)

        if available is not None and available > 0:
            date_key = datetime.now(_IST).strftime("%Y%m%d")
            await redis.set(f"daily_capital:{date_key}", str(available), ex=90_000)
            log.info("daily_capital_cached", capital=available)
            print(f"[CAPITAL] Fetched from Upstox: ₹{available:,.0f} available margin (SEC segment)")
        else:
            log.warning("daily_capital_zero_or_missing", data=str(data)[:200])
            print("[CAPITAL] Upstox returned 0 or no equity margin — falling back to ACCOUNT_CAPITAL env var")
    except Exception as e:
        log.error("daily_capital_fetch_failed", error=str(e))
        print(f"[CAPITAL] Upstox API error — using ACCOUNT_CAPITAL env fallback: {e}")


async def _capital_refresh_loop(upstox: UpstoxClient, redis) -> None:
    """Fetch and cache capital once daily at 9:15 IST (market open).

    On startup: if already past 9:15 today and no cached value exists,
    fetch immediately so the guard has a live capital value right away.
    """
    date_key = datetime.now(_IST).strftime("%Y%m%d")
    existing = await redis.get(f"daily_capital:{date_key}")
    if not existing:
        log.info("daily_capital_startup_fetch")
        await _fetch_and_cache_capital(upstox, redis)

    while True:
        now = datetime.now(_IST)
        target = now.replace(
            hour=_MARKET_OPEN_HOUR, minute=_MARKET_OPEN_MINUTE, second=5, microsecond=0
        )
        if now >= target:
            target += timedelta(days=1)
        wait_secs = (target - now).total_seconds()
        log.info("capital_refresh_scheduled", wait_secs=int(wait_secs), next_fetch=target.isoformat())
        await asyncio.sleep(wait_secs)
        await _fetch_and_cache_capital(upstox, redis)


async def main() -> None:
    configure_logging("trading-engine")
    log.info("trading_engine_starting")

    pool = await create_pool(settings.postgres_dsn)
    await run_migrations(pool)

    redis = await create_redis(settings.redis_host, settings.redis_port)
    await ensure_consumer_group(redis, settings.redis_stream_name, CONSUMER_GROUP)

    async with httpx.AsyncClient() as http:
        from shared.instruments.loader import download_instruments as _dl
        instruments = await _dl(http)

    instrument_cache = InstrumentCache(redis)
    await instrument_cache.load(instruments)

    upstox = UpstoxClient(settings.upstox_access_token, settings.upstox_base_url)
    db_writer = AsyncDBWriter(pool)
    timing_gate = MarketTimingGate()
    pnl_guard = DailyPnLGuard()

    writer_task = asyncio.create_task(db_writer.run())
    asyncio.create_task(heartbeat_loop())
    asyncio.create_task(_capital_refresh_loop(upstox, redis))   # daily 9:15 IST capital fetch

    # Reconcile loop every 2 minutes
    async def _reconcile_loop():
        while True:
            await asyncio.sleep(120)
            try:
                await reconcile_stale(pool, upstox, db_writer)
            except Exception as e:
                log.error("reconcile_error", error=str(e))

    reconcile_task = asyncio.create_task(_reconcile_loop())

    log.info("trading_engine_ready", stream=settings.redis_stream_name, consumer_group=CONSUMER_GROUP)

    # C-2: Reclaim messages stuck in PEL from previous crash
    pending = await stream_reclaim_pending(redis, settings.redis_stream_name, CONSUMER_GROUP, CONSUMER_NAME)
    for msg_id, fields in pending:
        try:
            await process_signal(msg_id, fields, upstox, instrument_cache,
                                 db_writer, redis, pool, timing_gate, pnl_guard)
            await stream_ack(redis, settings.redis_stream_name, CONSUMER_GROUP, msg_id)
        except Exception as e:
            log.error("pel_reclaim_error", msg_id=msg_id, error=str(e))

    try:
        while True:
            messages = await stream_read_group(
                redis,
                settings.redis_stream_name,
                CONSUMER_GROUP,
                CONSUMER_NAME,
                count=10,
                block_ms=2000,
            )
            for msg_id, fields in messages:
                try:
                    await process_signal(
                        msg_id, fields, upstox, instrument_cache,
                        db_writer, redis, pool, timing_gate, pnl_guard,
                    )
                    # C-1: Only ACK on successful (permanent) completion
                    await stream_ack(redis, settings.redis_stream_name, CONSUMER_GROUP, msg_id)
                except asyncpg.PostgresConnectionError as e:
                    log.error("transient_db_error_no_ack", msg_id=msg_id, error=str(e))
                    # Don't ACK — stays in PEL for reclaim on restart
                except Exception as e:
                    log.error("signal_processing_error", msg_id=msg_id, error=str(e))
                    await stream_ack(redis, settings.redis_stream_name, CONSUMER_GROUP, msg_id)
    finally:
        reconcile_task.cancel()
        await db_writer.stop()
        writer_task.cancel()
        await upstox.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
