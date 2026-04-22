from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from .deps import get_pool, require_auth
from shared.upstox.client import UpstoxClient
from shared.config import settings
from shared.redis.pipeline_log import get_events as _get_pipeline_events

router = APIRouter(prefix="/accounts", tags=["accounts"], dependencies=[Depends(require_auth)])


@router.get("/pnl")
async def get_pnl_summary(pool=Depends(get_pool)):
    """30-day realized P&L history from DB."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM daily_pnl ORDER BY trading_date DESC LIMIT 30"
        )
    return [dict(r) for r in rows]


@router.get("/positions")
async def get_positions(pool=Depends(get_pool)):
    """Live positions with unrealised P&L from Upstox /v2/portfolio/short-term-positions."""
    upstox = UpstoxClient(settings.upstox_access_token, settings.upstox_base_url)
    try:
        positions = await upstox.get_positions()
    finally:
        await upstox.aclose()

    active_signal_ids_by_instrument = {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, instrument_key
            FROM signals
            WHERE status = 'ACTIVE'
              AND instrument_key IS NOT NULL
            ORDER BY signal_at DESC, id DESC
            """
        )

    for row in rows:
        instrument_key = row["instrument_key"]
        if instrument_key and instrument_key not in active_signal_ids_by_instrument:
            active_signal_ids_by_instrument[instrument_key] = row["id"]

    enriched_positions = []
    for position in positions or []:
        instrument_key = position.get("instrument_key") or position.get("instrument_token")
        enriched = dict(position)
        enriched["signal_id"] = active_signal_ids_by_instrument.get(instrument_key)
        enriched_positions.append(enriched)

    return enriched_positions


@router.get("/live-pnl")
async def get_live_pnl(pool=Depends(get_pool)):
    """Real-time combined P&L for the dashboard.

    unrealized_pnl  — live floating P&L from open positions (Upstox /v2/portfolio/short-term-positions)
    realized_pnl    — sum of closed-trade P&L recorded in DB today
    total_pnl       — unrealized + realized

    Source note:
      • Upstox positions API uses LTP × qty to compute unrealised — refreshed on every API call.
      • Realized P&L is computed from actual weighted-average fill prices via /v2/order/trades.
    """
    # --- unrealized: live from Upstox positions API ---
    upstox = UpstoxClient(settings.upstox_access_token, settings.upstox_base_url)
    unrealized_pnl: float | None = None
    positions_error: str | None = None
    try:
        positions = await upstox.get_positions()
        # Upstox position object fields: unrealised_pnl (preferred) or pnl
        unrealized_pnl = sum(
            float(p.get("unrealised_pnl") or p.get("pnl") or 0)
            for p in (positions or [])
        )
    except Exception as e:
        positions_error = str(e)
    finally:
        await upstox.aclose()

    # --- realized: today's closed trades from DB ---
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(pnl), 0) AS realized
            FROM signals
            WHERE pnl IS NOT NULL
              AND signal_at >= CURRENT_DATE AT TIME ZONE 'Asia/Kolkata'
            """
        )
    realized_pnl = float(row["realized"])

    total_pnl = (unrealized_pnl or 0.0) + realized_pnl

    return {
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": realized_pnl,
        "total_pnl": total_pnl,
        "source": {
            "unrealized": "upstox_positions_api /v2/portfolio/short-term-positions",
            "realized": "db_signals_table (weighted avg fill price)",
        },
        "unrealized_error": positions_error,  # None if OK, error string if Upstox call failed
    }


class PositionExitRequest(BaseModel):
    instrument_key: str
    quantity: int
    transaction_type: str  # BUY or SELL (the EXISTING position side; exit will be the opposite)


@router.post("/positions/exit")
async def exit_position(req: PositionExitRequest):
    """Place an immediate market order to close an open position by instrument key.

    Determines the exit side automatically (opposite of transaction_type).
    Does not require a signal_id — works for any open position including manual ones.
    """
    if not req.instrument_key:
        raise HTTPException(400, "instrument_key is required")
    if req.quantity <= 0:
        raise HTTPException(400, "quantity must be > 0")
    txn = req.transaction_type.upper()
    if txn not in ("BUY", "SELL"):
        raise HTTPException(400, "transaction_type must be BUY or SELL")

    exit_txn = "SELL" if txn == "BUY" else "BUY"

    upstox = UpstoxClient(settings.upstox_access_token, settings.upstox_base_url)
    try:
        order_result = await upstox.place_order(
            instrument_token=req.instrument_key,
            transaction_type=exit_txn,
            quantity=req.quantity,
            order_type="MARKET",
            product="D",
            validity="IOC",
        )
    finally:
        await upstox.aclose()

    return {
        "status": "exited",
        "instrument_key": req.instrument_key,
        "exit_transaction_type": exit_txn,
        "quantity": req.quantity,
        "order_result": order_result,
    }


@router.get("/pipeline-log")
async def get_pipeline_log(request: Request, count: int = 60):
    """Return the last N pipeline events from the Redis ring buffer (newest first)."""
    redis = request.app.state.redis
    return await _get_pipeline_events(redis, count)


@router.get("/summary")
async def get_summary(pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'ACTIVE')       AS active_signals,
                COUNT(*) FILTER (WHERE status = 'PENDING')      AS pending_signals,
                COUNT(*) FILTER (WHERE status = 'TARGET1_HIT')  AS targets_hit,
                COUNT(*) FILTER (WHERE status = 'STOPLOSS_HIT') AS stoploss_hit,
                COALESCE(SUM(pnl) FILTER (WHERE pnl IS NOT NULL), 0) AS total_pnl
            FROM signals
            WHERE signal_at >= CURRENT_DATE AT TIME ZONE 'Asia/Kolkata'
            """
        )
    result = dict(row)
    result["trader_profile"] = settings.trader_profile.upper()
    return result
