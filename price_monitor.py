"""
price_monitor.py — Upstox WebSocket v3 price monitor for GTT signal tracking

Connects to Upstox Market Data Feed WebSocket, subscribes to instruments
with active GTT signals, and updates the SQLite DB when price thresholds
(entry, target1, stoploss) are crossed.

Run independently alongside gtt_strategy.py:
    python3 price_monitor.py
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, time as dtime, timezone

import requests
from requests.exceptions import RequestException
from dotenv import load_dotenv

import db
import MarketDataFeedV3_pb2 as pb

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── Config ────────────────────────────────────────────────────────────────────
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")
UPSTOX_BASE_URL = os.getenv("UPSTOX_BASE_URL", "https://api.upstox.com")

# Market hours (IST, UTC+5:30)
MARKET_OPEN  = dtime(9, 0)
MARKET_CLOSE = dtime(15, 35)

# How often (seconds) to refresh signal list from DB and re-subscribe new instruments
REFRESH_INTERVAL = 60

# How often (seconds) to poll Upstox GTT order status API
GTT_RECONCILE_INTERVAL = 60

# ── Logging ───────────────────────────────────────────────────────────────────
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_log_dir, exist_ok=True)

logger = logging.getLogger("price_monitor")
if not logger.handlers:
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    _ch = logging.StreamHandler()
    _ch.setFormatter(_fmt)
    _fh = logging.FileHandler(os.path.join(_log_dir, "price_monitor.log"))
    _fh.setFormatter(_fmt)
    logger.addHandler(_ch)
    logger.addHandler(_fh)
logger.setLevel(logging.INFO)

# ── IST helpers ───────────────────────────────────────────────────────────────
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except ImportError:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")


def _ist_now() -> datetime:
    return datetime.now(IST)


def _is_market_hours() -> bool:
    now = _ist_now()
    if now.weekday() >= 5:          # Saturday / Sunday
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


# ── Upstox WebSocket auth ─────────────────────────────────────────────────────
def _get_ws_url() -> str:
    """Fetch authorized WebSocket URI from Upstox."""
    url = f"{UPSTOX_BASE_URL}/v3/feed/market-data-feed/authorize"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
    }
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    ws_url = data["data"]["authorizedRedirectUri"]
    logger.info(f"🔗 WebSocket URL obtained")
    return ws_url


# ── Signal cache (in-memory, refreshed from DB) ───────────────────────────────
# { signal_id: signal_dict }
_signal_cache: dict[int, dict] = {}
# { instrument_key: [signal_id, ...] }
_instrument_index: dict[str, list[int]] = {}
# instrument_keys currently subscribed
_subscribed: set[str] = set()


def _rebuild_cache():
    """Reload PENDING/ACTIVE signals from DB into memory."""
    global _signal_cache, _instrument_index
    signals = db.get_watchable_signals()
    new_cache = {s["id"]: s for s in signals if s.get("instrument_key")}
    new_index: dict[str, list[int]] = {}
    for sid, s in new_cache.items():
        ik = s["instrument_key"]
        new_index.setdefault(ik, []).append(sid)
    _signal_cache = new_cache
    _instrument_index = new_index
    logger.info(f"📋 Cache refreshed: {len(_signal_cache)} watchable signal(s), {len(_instrument_index)} instrument(s)")


def _new_instrument_keys() -> set[str]:
    """Return instrument keys in cache but not yet subscribed."""
    return set(_instrument_index.keys()) - _subscribed


# ── Threshold checker ─────────────────────────────────────────────────────────
def _check_thresholds(instrument_key: str, ltp: float):
    if ltp <= 0:
        return
    signal_ids = _instrument_index.get(instrument_key, [])
    for sid in list(signal_ids):
        signal = _signal_cache.get(sid)
        if not signal:
            continue
        status = signal.get("status")
        action = (signal.get("action") or "BUY").upper()
        entry_low = signal.get("entry_low") or 0
        stoploss  = signal.get("stoploss") or 0
        quantity  = signal.get("quantity") or 0

        if status == "PENDING":
            if (action == "BUY" and ltp >= entry_low) or (action == "SELL" and ltp <= entry_low):
                logger.info(f"🔔 ENTRY HIT  | signal_id={sid} | {instrument_key} | LTP={ltp} | entry={entry_low}")
                db.update_signal_entry_executed(sid, ltp)
                db.insert_price_event(sid, "ENTRY_HIT", ltp, entry_low)
                signal["status"] = "ACTIVE"
                signal["entry_price"] = ltp
            elif stoploss and ((action == "BUY" and ltp <= stoploss) or (action == "SELL" and ltp >= stoploss)):
                logger.info(f"🛑 STOPLOSS HIT (pending) | signal_id={sid} | {instrument_key} | LTP={ltp} | SL={stoploss}")
                db.update_signal_exit_executed(sid, ltp, 0.0, "STOPLOSS_HIT")
                db.insert_price_event(sid, "STOPLOSS_HIT", ltp, stoploss)
                _signal_cache.pop(sid, None)
                idx = _instrument_index.get(instrument_key, [])
                if sid in idx:
                    idx.remove(sid)
                continue

        if status == "ACTIVE" or signal.get("status") == "ACTIVE":
            already_hit = signal.setdefault("_targets_hit", set())
            all_targets = [
                ("target1", signal.get("target1")),
                ("target2", signal.get("target2")),
                ("target3", signal.get("target3")),
            ]
            for t_key, t_val in all_targets:
                if not t_val or t_key in already_hit:
                    continue
                if (action == "BUY" and ltp >= t_val) or (action == "SELL" and ltp <= t_val):
                    logger.info(f"🎯 {t_key.upper()}_HIT | signal_id={sid} | {instrument_key} | LTP={ltp} | {t_key}={t_val}")
                    if t_key == "target1":
                        # T1 = actual GTT exit: mark DB terminal + compute pnl
                        actual_entry = signal.get("entry_price") or signal.get("gtt_place_price") or 0.0
                        lot_sign = 1 if action == "BUY" else -1
                        pnl = round((ltp - actual_entry) * quantity * lot_sign, 2)
                        db.update_signal_exit_executed(sid, ltp, pnl, "TARGET1_HIT")
                        db.insert_price_event(sid, "TARGET1_HIT", ltp, t_val)
                    else:
                        # T2/T3 are analytics only
                        db.insert_price_event(sid, t_key.upper() + "_HIT", ltp, t_val)
                    already_hit.add(t_key)

            # Check if all defined targets are now hit → remove from cache (no more to watch)
            defined_targets = {k for k, v in all_targets if v}
            if defined_targets and defined_targets.issubset(already_hit):
                logger.info(f"📊 All targets hit | signal_id={sid} — removed from watch")
                _signal_cache.pop(sid, None)
                idx = _instrument_index.get(instrument_key, [])
                if sid in idx:
                    idx.remove(sid)
                continue

            # Stoploss check (only while signal is still in cache — not after all targets hit)
            if stoploss and ((action == "BUY" and ltp <= stoploss) or (action == "SELL" and ltp >= stoploss)):
                logger.info(f"🛑 STOPLOSS HIT | signal_id={sid} | {instrument_key} | LTP={ltp} | SL={stoploss}")
                actual_entry = signal.get("entry_price") or signal.get("gtt_place_price") or 0.0
                lot_sign = 1 if action == "BUY" else -1
                pnl = round((ltp - actual_entry) * quantity * lot_sign, 2)
                db.update_signal_exit_executed(sid, ltp, pnl, "STOPLOSS_HIT")
                db.insert_price_event(sid, "STOPLOSS_HIT", ltp, stoploss)
                _signal_cache.pop(sid, None)
                idx = _instrument_index.get(instrument_key, [])
                if sid in idx:
                    idx.remove(sid)


# ── Subscription helpers ──────────────────────────────────────────────────────
def _sub_message(instrument_keys: list[str]) -> bytes:
    return json.dumps({
        "guid": "price-monitor-v1",
        "method": "sub",
        "data": {
            "mode": "ltpc",
            "instrumentKeys": instrument_keys,
        },
    }).encode()


def _unsub_message(instrument_keys: list[str]) -> bytes:
    return json.dumps({
        "guid": "price-monitor-v1",
        "method": "unsub",
        "data": {
            "instrumentKeys": instrument_keys,
        },
    }).encode()


# ── GTT Reconciler ────────────────────────────────────────────────────────────

def _fetch_gtt_details(gtt_id: str) -> dict | None:
    """Call Upstox v3 GTT order details endpoint. Returns the data dict or None on error."""
    url = f"{UPSTOX_BASE_URL}/v3/order/gtt/{gtt_id}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        resp.raise_for_status()
        return resp.json().get("data", {})
    except RequestException as e:
        logger.warning(f"⚠️ GTT fetch failed for {gtt_id}: {e}")
        return None


def _process_gtt_reconciliation(signal: dict, gtt_data: dict, force_backfill: bool = False):
    """
    Map Upstox GTT response to local DB state.

    Upstox MULTIPLE GTT statuses:
      active    → still waiting for entry; no DB change needed
      triggered → at least one leg fired — inspect rules to find which
      completed → all legs settled (entry + exit)
      cancelled → user or system cancelled
      expired   → GTT window expired

    force_backfill=True: bypass terminal status guards and only write price/pnl
    fields (does not change status). Used at startup to backfill existing records.
    """
    sid = signal["id"]
    local_status = signal.get("status")
    upstox_status = (gtt_data.get("status") or "").lower()
    action = (signal.get("action") or "BUY").upper()
    quantity = signal.get("quantity") or 0
    terminal_statuses = {"CANCELLED", "EXPIRED", "TARGET1_HIT", "STOPLOSS_HIT"}

    db.update_signal_upstox_status(sid, upstox_status)

    if upstox_status == "active" and not force_backfill:
        return  # nothing to update

    if upstox_status == "cancelled":
        if local_status not in terminal_statuses:
            logger.info(f"🚫 GTT CANCELLED | signal_id={sid} | gtt={gtt_data.get('gtt_order_id')}")
            db.update_signal_exit_executed(sid, 0.0, 0.0, "CANCELLED")
            db.insert_price_event(sid, "GTT_CANCELLED", 0.0)
            _signal_cache.pop(sid, None)
            for ik, ids in _instrument_index.items():
                if sid in ids:
                    ids.remove(sid)
        return

    if upstox_status == "expired":
        if local_status not in terminal_statuses:
            logger.info(f"⌛ GTT EXPIRED | signal_id={sid} | gtt={gtt_data.get('gtt_order_id')}")
            db.update_signal_exit_executed(sid, 0.0, 0.0, "EXPIRED")
            db.insert_price_event(sid, "GTT_EXPIRED", 0.0)
            _signal_cache.pop(sid, None)
            for ik, ids in _instrument_index.items():
                if sid in ids:
                    ids.remove(sid)
        return

    # For triggered / completed — inspect individual rule legs
    rules = gtt_data.get("rules") or []
    entry_price = None
    exit_price = None
    exit_reason = None

    for rule in rules:
        strategy = (rule.get("strategy") or "").upper()
        rule_status = (rule.get("status") or "").lower()
        fill_price = rule.get("price") or rule.get("trigger_price") or 0.0

        if strategy == "ENTRY" and rule_status == "triggered":
            entry_price = float(fill_price)
        elif strategy == "TARGET" and rule_status == "triggered":
            exit_price = float(fill_price)
            exit_reason = "TARGET1_HIT"
        elif strategy == "STOPLOSS" and rule_status == "triggered":
            exit_price = float(fill_price)
            exit_reason = "STOPLOSS_HIT"

    if force_backfill:
        # Backfill mode: only write missing price fields, never change status
        actual_entry = entry_price or signal.get("entry_price") or signal.get("gtt_place_price") or 0.0
        bp_entry = entry_price if signal.get("entry_price") is None else None
        bp_exit = exit_price if signal.get("exit_price") is None else None
        bp_pnl = None
        if bp_exit and exit_reason and actual_entry and signal.get("pnl") is None:
            lot_sign = 1 if action == "BUY" else -1
            bp_pnl = round((bp_exit - actual_entry) * quantity * lot_sign, 2)
        if any(v is not None for v in (bp_entry, bp_exit, bp_pnl)):
            logger.info(f"🔁 Backfilled | signal_id={sid} | entry={bp_entry} exit={bp_exit} pnl={bp_pnl}")
            db.backfill_signal_prices(sid, bp_entry, bp_exit, bp_pnl)
        return

    # Normal reconciliation path
    if entry_price and local_status == "PENDING":
        logger.info(f"✅ GTT ENTRY EXECUTED | signal_id={sid} | entry_price={entry_price}")
        db.update_signal_entry_executed(sid, entry_price)
        db.insert_price_event(sid, "GTT_ENTRY_EXECUTED", entry_price, signal.get("entry_low"))
        if sid in _signal_cache:
            _signal_cache[sid]["status"] = "ACTIVE"
            _signal_cache[sid]["entry_price"] = entry_price

    if exit_price and exit_reason:
        if local_status not in terminal_statuses:
            actual_entry = entry_price or signal.get("entry_price") or signal.get("gtt_place_price") or 0.0
            lot_sign = 1 if action == "BUY" else -1
            pnl = round((exit_price - actual_entry) * quantity * lot_sign, 2)
            logger.info(
                f"💰 GTT EXIT EXECUTED | signal_id={sid} | reason={exit_reason} "
                f"| entry={actual_entry} exit={exit_price} pnl={pnl}"
            )
            db.update_signal_exit_executed(sid, exit_price, pnl, exit_reason)
            db.insert_price_event(sid, "GTT_EXIT_EXECUTED", exit_price)
            _signal_cache.pop(sid, None)
            for ik, ids in _instrument_index.items():
                if sid in ids:
                    ids.remove(sid)


async def _run_startup_backfill():
    """On startup, fetch Upstox GTT status for any signal with missing price data."""
    signals = db.get_signals_needing_backfill()
    if not signals:
        logger.info("✅ No signals need price backfill")
        return
    logger.info(f"🔁 Starting price backfill for {len(signals)} signal(s)...")
    for signal in signals:
        gtt_ids = json.loads(signal.get("gtt_order_ids") or "[]")
        for gtt_id in gtt_ids:
            gtt_data = _fetch_gtt_details(gtt_id)
            if gtt_data:
                _process_gtt_reconciliation(signal, gtt_data, force_backfill=True)
                db.insert_gtt_status_check(signal["id"], gtt_id, gtt_data.get("status", ""))
    logger.info("✅ Price backfill complete")


async def _gtt_reconciler_loop():
    """Periodically poll Upstox GTT API to sync actual execution state into the local DB."""
    await asyncio.sleep(GTT_RECONCILE_INTERVAL)   # initial delay — let WS connect first
    while True:
        if _is_market_hours():
            signals = db.get_signals_with_gtt_ids()
            logger.debug(f"🔄 GTT reconciler: checking {len(signals)} signal(s)")
            for signal in signals:
                gtt_ids = json.loads(signal.get("gtt_order_ids") or "[]")
                for gtt_id in gtt_ids:
                    gtt_data = _fetch_gtt_details(gtt_id)
                    if gtt_data:
                        _process_gtt_reconciliation(signal, gtt_data)
                        db.insert_gtt_status_check(
                            signal["id"], gtt_id, gtt_data.get("status", "")
                        )
        await asyncio.sleep(GTT_RECONCILE_INTERVAL)


# ── Main WebSocket loop ───────────────────────────────────────────────────────
async def _ws_loop():
    try:
        import websockets
    except ImportError:
        raise RuntimeError("websockets not installed. Run: pip install websockets==12.0")

    ws_url = _get_ws_url()

    async with websockets.connect(ws_url) as ws:
        logger.info("✅ WebSocket connected")

        # Initial subscription
        keys_to_sub = list(_instrument_index.keys())
        if keys_to_sub:
            await ws.send(_sub_message(keys_to_sub))
            _subscribed.update(keys_to_sub)
            logger.info(f"📡 Subscribed to {len(keys_to_sub)} instrument(s): {keys_to_sub}")
        else:
            logger.info("📭 No instruments to subscribe yet — waiting for signals in DB")

        last_refresh = time.monotonic()

        async for raw in ws:
            # Periodic refresh: pick up new signals added since last check
            if time.monotonic() - last_refresh >= REFRESH_INTERVAL:
                _rebuild_cache()
                new_keys = list(_new_instrument_keys())
                if new_keys:
                    await ws.send(_sub_message(new_keys))
                    _subscribed.update(new_keys)
                    logger.info(f"📡 Subscribed to {len(new_keys)} new instrument(s): {new_keys}")
                dead_keys = list(_subscribed - set(_instrument_index.keys()))
                if dead_keys:
                    await ws.send(_unsub_message(dead_keys))
                    _subscribed.difference_update(dead_keys)
                    logger.info(f"📴 Unsubscribed {len(dead_keys)} dead instrument(s): {dead_keys}")
                last_refresh = time.monotonic()

            if not isinstance(raw, bytes):
                continue

            try:
                feed_resp = pb.FeedResponse()
                feed_resp.ParseFromString(raw)
                for instrument_key, feed in feed_resp.feeds.items():
                    ltp = feed.ltpc.ltp
                    if ltp > 0:
                        _check_thresholds(instrument_key, ltp)
            except Exception as e:
                logger.debug(f"⚠️ Could not parse feed frame: {e}")


async def _main():
    db.init_db()
    logger.info("=" * 70)
    logger.info(f"🚀 Price Monitor started at {_ist_now().isoformat()}")
    logger.info("=" * 70)

    if not UPSTOX_ACCESS_TOKEN:
        raise SystemExit("UPSTOX_ACCESS_TOKEN not set in .env")

    await _run_startup_backfill()

    while True:
        if not _is_market_hours():
            now = _ist_now()
            logger.info(f"🕐 Outside market hours ({now.strftime('%H:%M IST, %A')}) — sleeping 60s")
            await asyncio.sleep(60)
            continue

        _rebuild_cache()

        try:
            await asyncio.gather(
                _ws_loop(),
                _gtt_reconciler_loop(),
            )
        except KeyboardInterrupt:
            logger.info("🛑 Stopped by user.")
            break
        except Exception as e:
            logger.warning(f"⚠️ WebSocket error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(_main())
