"""
upstox_order_tracker.py -- Event-driven tracker for Upstox GTT lifecycle.

Primary source of truth:
1. Portfolio Stream Feed (`order` + `gtt_order`)
2. Order trades / order history API lookups for exact fills
3. GTT detail backfill on startup for recovery
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

import requests
import websockets
from dotenv import load_dotenv

import db
from latency import duration_ms, log_latency, now_perf_ns
from settings import LOG_DIR

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")
UPSTOX_BASE_URL = os.getenv("UPSTOX_BASE_URL", "https://api.upstox.com")
TRACKER_RECONCILE_INTERVAL = int(os.getenv("TRACKER_RECONCILE_INTERVAL", "120"))

logger = logging.getLogger("upstox_order_tracker")
if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    _ch = logging.StreamHandler()
    _ch.setFormatter(_fmt)
    _fh = logging.FileHandler(LOG_DIR / "upstox_order_tracker.log")
    _fh.setFormatter(_fmt)
    logger.addHandler(_ch)
    logger.addHandler(_fh)
logger.setLevel(logging.INFO)


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
    }


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _get_portfolio_ws_url() -> str:
    started_perf = now_perf_ns()
    url = f"{UPSTOX_BASE_URL}/v2/feed/portfolio-stream-feed/authorize"
    resp = requests.get(
        url,
        headers=_headers(),
        params={"update_types": "order,gtt_order"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    ws_url = data.get("authorized_redirect_uri") or data.get("authorizedRedirectUri")
    if not ws_url:
        raise RuntimeError("Portfolio WS authorize response did not include authorized URI")
    logger.info("🔗 Portfolio WebSocket URL obtained")
    log_latency(logger, "unknown", "tracker_ws_auth", auth_ms=duration_ms(started_perf), status="success")
    return ws_url


def _fetch_gtt_details(gtt_order_id: str) -> dict | None:
    url = f"{UPSTOX_BASE_URL}/v3/order/gtt"
    started_perf = now_perf_ns()
    try:
        resp = requests.get(
            url,
            headers={**_headers(), "Content-Type": "application/json"},
            params={"gtt_order_id": gtt_order_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        if isinstance(data, list):
            # Some responses return a list; select the matching GTT row when possible.
            for item in data:
                if isinstance(item, dict) and str(item.get("gtt_order_id") or item.get("id") or "") == str(gtt_order_id):
                    data = item
                    break
            else:
                data = data[0] if data and isinstance(data[0], dict) else {}
        if not isinstance(data, dict):
            data = {}
        log_latency(
            logger,
            "unknown",
            "tracker_api_fetch",
            api="gtt_details",
            gtt_order_id=gtt_order_id,
            api_backfill_ms=duration_ms(started_perf),
            status="success",
        )
        return data
    except requests.RequestException as exc:
        logger.debug(f"Could not fetch GTT details for {gtt_order_id}: {exc}")
        log_latency(
            logger,
            "unknown",
            "tracker_api_fetch",
            api="gtt_details",
            gtt_order_id=gtt_order_id,
            api_backfill_ms=duration_ms(started_perf),
            status="error",
        )
        return None


def _fetch_order_history(order_id: str) -> list[dict]:
    url = f"{UPSTOX_BASE_URL}/v2/order/history"
    started_perf = now_perf_ns()
    try:
        resp = requests.get(
            url,
            headers={**_headers(), "Content-Type": "application/json"},
            params={"order_id": order_id},
            timeout=10,
        )
        resp.raise_for_status()
        log_latency(
            logger,
            "unknown",
            "tracker_api_fetch",
            api="order_history",
            order_id=order_id,
            api_backfill_ms=duration_ms(started_perf),
            status="success",
        )
        return resp.json().get("data", []) or []
    except requests.RequestException as exc:
        logger.debug(f"Could not fetch order history for {order_id}: {exc}")
        log_latency(
            logger,
            "unknown",
            "tracker_api_fetch",
            api="order_history",
            order_id=order_id,
            api_backfill_ms=duration_ms(started_perf),
            status="error",
        )
        return []


def _fetch_order_trades(order_id: str) -> list[dict]:
    url = f"{UPSTOX_BASE_URL}/v2/order/trades"
    started_perf = now_perf_ns()
    try:
        resp = requests.get(
            url,
            headers={**_headers(), "Content-Type": "application/json"},
            params={"order_id": order_id},
            timeout=10,
        )
        resp.raise_for_status()
        log_latency(
            logger,
            "unknown",
            "tracker_api_fetch",
            api="order_trades",
            order_id=order_id,
            api_backfill_ms=duration_ms(started_perf),
            status="success",
        )
        return resp.json().get("data", []) or []
    except requests.RequestException as exc:
        logger.debug(f"Could not fetch order trades for {order_id}: {exc}")
        log_latency(
            logger,
            "unknown",
            "tracker_api_fetch",
            api="order_trades",
            order_id=order_id,
            api_backfill_ms=duration_ms(started_perf),
            status="error",
        )
        return []


def _weighted_price(trades: list[dict]) -> Optional[float]:
    total_qty = 0
    total_val = 0.0
    for trade in trades:
        qty = _safe_int(trade.get("quantity")) or 0
        price = _safe_float(trade.get("average_price")) or _safe_float(trade.get("price")) or 0.0
        total_qty += qty
        total_val += qty * price
    if total_qty <= 0:
        return None
    return round(total_val / total_qty, 4)


def _final_price_from_payload(order_payload: dict, trades: list[dict]) -> Optional[float]:
    return (
        _weighted_price(trades)
        or _safe_float(order_payload.get("average_price"))
        or _safe_float(order_payload.get("price"))
    )


def _is_fill_event(order_payload: dict) -> bool:
    filled = _safe_int(order_payload.get("filled_quantity")) or 0
    pending = _safe_int(order_payload.get("pending_quantity"))
    status = str(order_payload.get("status", "")).lower()
    return filled > 0 or "complete" in status or (pending == 0 and "cancel" not in status and "reject" not in status and "fail" not in status)


def _is_rejected_or_cancelled(order_payload: dict) -> bool:
    status = str(order_payload.get("status", "")).lower()
    return any(token in status for token in ("reject", "cancel", "fail"))


def _compute_exit_pnl(signal: dict, exit_price: float) -> float:
    action = (signal.get("action") or "BUY").upper()
    quantity = signal.get("quantity") or 0
    entry_price = signal.get("entry_price") or signal.get("gtt_place_price") or signal.get("entry_low") or 0.0
    lot_sign = 1 if action == "BUY" else -1
    return round((exit_price - float(entry_price)) * int(quantity) * lot_sign, 2)


def _record_raw_event(signal_id: Optional[int], payload: dict):
    update_type = payload.get("update_type", "unknown")
    entity_id = (
        payload.get("gtt_order_id")
        or payload.get("order_id")
        or payload.get("instrument_key")
        or payload.get("instrument_token")
    )
    db.record_upstox_event(signal_id, update_type, entity_id, payload)


def _apply_entry_fill(signal_id: int, signal: dict, price: Optional[float]):
    if price is None:
        return
    if signal.get("status") != "ACTIVE":
        db_started_perf = now_perf_ns()
        logger.info(f"✅ ENTRY EXECUTED | signal_id={signal_id} | price={price}")
        db.update_signal_entry_executed(signal_id, price)
        db.insert_price_event(signal_id, "ENTRY_EXECUTED", price, signal.get("entry_low"))
        log_latency(
            logger,
            "unknown",
            "tracker_db_update",
            update_type="entry",
            signal_id=signal_id,
            db_ms=duration_ms(db_started_perf),
            status="success",
        )


def _apply_exit_fill(signal_id: int, signal: dict, strategy: str, price: Optional[float]):
    if price is None:
        return
    if signal.get("status") in ("TARGET1_HIT", "STOPLOSS_HIT", "FAILED", "CANCELLED", "EXPIRED"):
        return
    exit_reason = "TARGET1_HIT" if strategy == "TARGET" else "STOPLOSS_HIT"
    pnl = _compute_exit_pnl(signal, price)
    db_started_perf = now_perf_ns()
    logger.info(f"💰 EXIT EXECUTED | signal_id={signal_id} | strategy={strategy} | price={price} | pnl={pnl}")
    db.update_signal_exit_executed(signal_id, price, pnl, exit_reason)
    db.insert_price_event(signal_id, exit_reason, price)
    log_latency(
        logger,
        "unknown",
        "tracker_db_update",
        update_type=strategy.lower(),
        signal_id=signal_id,
        db_ms=duration_ms(db_started_perf),
        status="success",
    )


def _sync_order_from_api(signal_id: int, order_id: str, strategy: str):
    total_started_perf = now_perf_ns()
    db_started_perf = None
    trades = _fetch_order_trades(order_id)
    db_started_perf = now_perf_ns()
    if trades:
        db.replace_trade_executions(signal_id, order_id, trades)
    history = _fetch_order_history(order_id)
    if history:
        for update in history:
            db.insert_order_update(signal_id, strategy, update)
        order_payload = history[-1]
    else:
        order_payload = {"order_id": order_id}
    final_price = _final_price_from_payload(order_payload, trades)
    signal = db.get_signal(signal_id)
    db_ms = duration_ms(db_started_perf)
    if not signal:
        log_latency(
            logger,
            "unknown",
            "tracker_sync",
            signal_id=signal_id,
            order_id=order_id,
            strategy=strategy,
            db_ms=db_ms,
            total_ms=duration_ms(total_started_perf),
            status="signal_missing",
        )
        return
    if strategy == "ENTRY":
        _apply_entry_fill(signal_id, signal, final_price)
    elif strategy in ("TARGET", "STOPLOSS"):
        _apply_exit_fill(signal_id, signal, strategy, final_price)

    log_latency(
        logger,
        "unknown",
        "tracker_sync",
        signal_id=signal_id,
        order_id=order_id,
        strategy=strategy,
        db_ms=db_ms,
        total_ms=duration_ms(total_started_perf),
        status="success",
    )


def _handle_gtt_update(payload: dict):
    started_perf = now_perf_ns()
    gtt_order_id = payload.get("gtt_order_id")
    signal = db.get_signal_by_gtt_order_id(gtt_order_id)
    signal_id = signal["id"] if signal else None
    _record_raw_event(signal_id, payload)
    if not signal:
        logger.warning(f"⚠️ GTT update received for unknown gtt_order_id={gtt_order_id}")
        log_latency(
            logger,
            "unknown",
            "tracker_event",
            update_type="gtt_order",
            gtt_order_id=gtt_order_id,
            total_ms=duration_ms(started_perf),
            status="unknown_signal",
        )
        return

    db.set_signal_gtt_order(signal_id, gtt_order_id)
    db.update_tracker_fields(signal_id, upstox_gtt_status=payload.get("type"))

    for rule in payload.get("rules", []):
        strategy = str(rule.get("strategy", "")).upper()
        rule_status = str(rule.get("status", "")).upper()
        order_id = rule.get("order_id")

        db.upsert_gtt_rule(signal_id, gtt_order_id, rule)

        if strategy == "ENTRY":
            if rule_status == "PENDING":
                db.update_tracker_fields(signal_id, tracker_status="ENTRY_PENDING")
            elif rule_status == "COMPLETED":
                db.update_tracker_fields(signal_id, tracker_status="ENTRY_TRIGGERED")
            elif rule_status == "FAILED":
                db.update_tracker_fields(signal_id, tracker_status="ENTRY_FAILED", notes=rule.get("message") or "Entry rule failed")
                db.update_signal_status(signal_id, "FAILED", rule.get("message") or "Entry rule failed")
            elif rule_status == "CANCELLED":
                db.update_tracker_fields(signal_id, tracker_status="CANCELLED", notes=rule.get("message") or "Entry rule cancelled")
                db.update_signal_status(signal_id, "CANCELLED", rule.get("message") or "Entry rule cancelled")
        elif strategy == "TARGET" and rule_status == "COMPLETED":
            db.update_tracker_fields(signal_id, tracker_status="TARGET_TRIGGERED")
        elif strategy == "STOPLOSS" and rule_status == "COMPLETED":
            db.update_tracker_fields(signal_id, tracker_status="STOPLOSS_TRIGGERED")

        if order_id:
            _sync_order_from_api(signal_id, order_id, strategy)

    log_latency(
        logger,
        "unknown",
        "tracker_event",
        update_type="gtt_order",
        gtt_order_id=gtt_order_id,
        signal_id=signal_id,
        total_ms=duration_ms(started_perf),
        status="processed",
    )


def _handle_order_update(payload: dict):
    started_perf = now_perf_ns()
    order_id = payload.get("order_id")
    signal = db.get_signal_by_order_id(order_id)
    signal_id = signal["id"] if signal else None
    strategy = db.get_signal_strategy_by_order_id(order_id)
    _record_raw_event(signal_id, payload)
    db.insert_order_update(signal_id, strategy, payload)

    if not signal or not strategy:
        logger.warning(f"⚠️ Order update received for unknown order_id={order_id}")
        log_latency(
            logger,
            "unknown",
            "tracker_event",
            update_type="order",
            order_id=order_id,
            total_ms=duration_ms(started_perf),
            status="unknown_signal",
        )
        return

    trades = _fetch_order_trades(order_id)
    if trades:
        db.replace_trade_executions(signal_id, order_id, trades)
    final_price = _final_price_from_payload(payload, trades)

    if _is_fill_event(payload):
        if strategy == "ENTRY":
            _apply_entry_fill(signal_id, signal, final_price)
        elif strategy in ("TARGET", "STOPLOSS"):
            _apply_exit_fill(signal_id, signal, strategy, final_price)
    elif _is_rejected_or_cancelled(payload):
        notes = payload.get("status_message") or payload.get("status_message_raw") or payload.get("status") or "Order rejected/cancelled"
        if strategy == "ENTRY":
            db.update_tracker_fields(signal_id, tracker_status="ENTRY_FAILED", notes=notes)
            db.update_signal_status(signal_id, "FAILED", notes)
        else:
            db.update_tracker_fields(signal_id, tracker_status=f"{strategy}_ORDER_ISSUE", notes=notes)

    log_latency(
        logger,
        "unknown",
        "tracker_event",
        update_type="order",
        order_id=order_id,
        signal_id=signal_id,
        total_ms=duration_ms(started_perf),
        status="processed",
    )


def _process_payload(payload: dict):
    started_perf = now_perf_ns()
    update_type = payload.get("update_type")
    if update_type == "gtt_order":
        _handle_gtt_update(payload)
    elif update_type == "order":
        _handle_order_update(payload)
    log_latency(
        logger,
        "unknown",
        "tracker_payload",
        update_type=update_type or "unknown",
        total_ms=duration_ms(started_perf),
        status="processed",
    )


def _startup_backfill():
    signals = db.get_signals_with_gtt_ids()
    if not signals:
        logger.info("✅ No GTT signals to backfill")
        return
    logger.info(f"🔁 Startup backfill for {len(signals)} signal(s)")
    for signal in signals:
        gtt_order_id = signal.get("gtt_order_id")
        if gtt_order_id:
            gtt_data = _fetch_gtt_details(gtt_order_id)
            if gtt_data:
                if isinstance(gtt_data, dict):
                    gtt_data["update_type"] = "gtt_order"
                    _process_payload(gtt_data)
                    db.insert_gtt_status_check(signal["id"], gtt_order_id, str(gtt_data.get("status", "")))
                else:
                    logger.warning(f"⚠️ Skipping unexpected gtt_details payload type: {type(gtt_data).__name__}")

        for strategy, order_id in (
            ("ENTRY", signal.get("entry_order_id")),
            ("TARGET", signal.get("target_order_id")),
            ("STOPLOSS", signal.get("stoploss_order_id")),
        ):
            if order_id:
                _sync_order_from_api(signal["id"], order_id, strategy)


async def _reconcile_loop():
    while True:
        await asyncio.sleep(TRACKER_RECONCILE_INTERVAL)
        try:
            _startup_backfill()
        except Exception as exc:
            logger.warning(f"⚠️ Reconcile loop error: {exc}")


async def _ws_loop():
    while True:
        try:
            ws_url = _get_portfolio_ws_url()
            async with websockets.connect(ws_url) as ws:
                logger.info("✅ Portfolio WebSocket connected")
                async for raw in ws:
                    if isinstance(raw, bytes):
                        try:
                            raw = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            logger.debug("Skipping non-text WebSocket frame")
                            continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug(f"Skipping non-JSON frame: {raw!r}")
                        continue
                    if isinstance(payload, dict):
                        _process_payload(payload)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.warning(f"⚠️ Portfolio WebSocket error: {exc}. Reconnecting in 5s...")
            await asyncio.sleep(5)


async def _main():
    db.init_db()
    logger.info("=" * 80)
    logger.info(f"🚀 Upstox Order Tracker started at {datetime.now().isoformat()}")
    logger.info("=" * 80)

    if not UPSTOX_ACCESS_TOKEN:
        raise SystemExit("UPSTOX_ACCESS_TOKEN not set in .env")

    _startup_backfill()
    await asyncio.gather(
        _ws_loop(),
        _reconcile_loop(),
    )


if __name__ == "__main__":
    asyncio.run(_main())
