"""Signal management endpoints."""
import json
import asyncpg
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional

from shared.db.signals import get_signal, update_signal_status
from shared.db.gtt_rules import get_gtt_rules_for_signal
from .deps import require_auth

router = APIRouter(prefix="/signals", tags=["signals"], dependencies=[Depends(require_auth)])


async def _get_pool():
    from .deps import get_pool
    return await get_pool()


@router.get("")
async def list_signals(
    status: Optional[str] = None,
    limit: int = 50,
    pool=Depends(_get_pool),
):
    query = "SELECT * FROM signals"
    params = []
    if status:
        query += " WHERE status = $1"
        params.append(status)
    query += " ORDER BY signal_at DESC LIMIT $" + str(len(params) + 1)
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [dict(r) for r in rows]


@router.get("/{signal_id}")
async def get_signal_detail(signal_id: int, pool=Depends(_get_pool)):
    async with pool.acquire() as conn:
        signal = await get_signal(conn, signal_id)
        if not signal:
            raise HTTPException(404, "Signal not found")
        rules = await get_gtt_rules_for_signal(conn, signal_id)
    return {"signal": signal, "gtt_rules": rules}


@router.post("/{signal_id}/cancel")
async def cancel_signal(signal_id: int, pool=Depends(_get_pool)):
    from shared.upstox.client import UpstoxClient
    from shared.config import settings

    async with pool.acquire() as conn:
        signal = await get_signal(conn, signal_id)
        if not signal:
            raise HTTPException(404, "Signal not found")
        if signal["status"] not in ("PENDING", "ACTIVE"):
            raise HTTPException(400, f"Signal status {signal['status']!r} is not cancellable")

    upstox = UpstoxClient(settings.upstox_access_token, settings.upstox_base_url)
    errors = []
    for gtt_id in (signal.get("gtt_order_ids") or []):
        try:
            await upstox.cancel_gtt(gtt_id)
        except Exception as e:
            errors.append({"gtt_id": gtt_id, "error": str(e)})
    await upstox.aclose()

    async with pool.acquire() as conn:
        await update_signal_status(conn, signal_id, "CANCELLED", "manual_cancel")

    return {"status": "cancelled", "signal_id": signal_id, "errors": errors}


@router.post("/{signal_id}/exit")
async def exit_signal(signal_id: int, pool=Depends(_get_pool)):
    """Cancel GTT and place a market exit order to close the active position."""
    from shared.upstox.client import UpstoxClient
    from shared.config import settings

    async with pool.acquire() as conn:
        signal = await get_signal(conn, signal_id)
        if not signal:
            raise HTTPException(404, "Signal not found")
        if signal["status"] != "ACTIVE":
            raise HTTPException(400, f"Signal status {signal['status']!r} is not exitable")

    upstox = UpstoxClient(settings.upstox_access_token, settings.upstox_base_url)
    errors = []
    order_result = None

    lot_size_map = {
        "NIFTY": settings.lot_size_nifty,
        "BANKNIFTY": settings.lot_size_banknifty,
        "SENSEX": settings.lot_size_sensex,
    }
    lot_size = lot_size_map.get((signal.get("underlying") or "").upper(), 1)
    fallback_qty = settings.default_quantity * lot_size

    try:
        positions = await upstox.get_positions()
    except Exception as e:
        positions = []
        errors.append({"positions_lookup": str(e)})

    qty = None
    for position in positions or []:
        position_key = position.get("instrument_key") or position.get("instrument_token")
        if position_key == signal.get("instrument_key"):
            qty = abs(int(position.get("quantity") or position.get("net_quantity") or 0))
            break

    if not qty:
        qty = fallback_qty

    if qty <= 0:
        await upstox.aclose()
        raise HTTPException(400, "Unable to resolve a valid exit quantity")

    exit_txn = "SELL" if signal["action"] == "BUY" else "BUY"

    try:
        order_result = await upstox.place_order(
            instrument_token=signal["instrument_key"],
            transaction_type=exit_txn,
            quantity=qty,
            order_type="MARKET",
            product="D",
            validity="IOC",
        )
    except Exception as e:
        await upstox.aclose()
        raise HTTPException(502, f"Market exit failed: {e}") from e

    for gtt_id in (signal.get("gtt_order_ids") or []):
        try:
            await upstox.cancel_gtt(gtt_id)
        except Exception as e:
            errors.append({"gtt_id": gtt_id, "error": str(e)})

    await upstox.aclose()

    async with pool.acquire() as conn:
        await update_signal_status(conn, signal_id, "CANCELLED", "manual_exit")

    return {"status": "exited", "signal_id": signal_id, "order_result": order_result, "errors": errors}


class ManualSignalRequest(BaseModel):
    action: str
    underlying: str
    strike: Optional[int] = None
    option_type: Optional[str] = None
    entry_low: float
    entry_high: Optional[float] = None
    stoploss: float
    targets: list[float]
    expiry: Optional[str] = None
    quantity_lots: int = 1
    product: str = "D"


@router.post("/manual")
async def post_manual_signal(req: ManualSignalRequest, pool=Depends(_get_pool)):
    """Post a signal manually — goes through the full Redis Stream pipeline."""
    import time
    from shared.redis.client import get_redis, stream_add
    redis = await get_redis()
    payload = {
        "action": req.action,
        "instrument": req.underlying,
        "strike": str(req.strike or ""),
        "option_type": req.option_type or "",
        "entry_low": str(req.entry_low),
        "entry_high": str(req.entry_high or ""),
        "stoploss": str(req.stoploss),
        "targets": "/".join(str(t) for t in req.targets),
        "expiry": req.expiry or "",
        "quantity": str(req.quantity_lots),
        "product": req.product,
        "timestamp": str(int(time.time())),
        "source": "manual_admin",
    }
    msg_id = await stream_add(redis, "raw_trade_signals", payload)
    return {"status": "published", "stream_id": msg_id}
