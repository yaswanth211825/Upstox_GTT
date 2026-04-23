from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from .deps import get_pool, require_auth
from shared.upstox.client import UpstoxClient
from shared.config import settings
from shared.redis.pipeline_log import get_events as _get_pipeline_events

router = APIRouter(prefix="/accounts", tags=["accounts"], dependencies=[Depends(require_auth)])


async def _fetch_upstox_live_pnl() -> tuple[float | None, float | None, float | None, str | None]:
    """Fetch day P&L from Upstox positions as broker/account-level context."""
    upstox = UpstoxClient(settings.upstox_access_token, settings.upstox_base_url)
    total_pnl: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    positions_error: str | None = None

    try:
        positions = await upstox.get_positions()
        total_pnl = sum(float(p.get("pnl") or 0) for p in (positions or []))
        unrealized_pnl = sum(
            float(p.get("unrealised_pnl") or p.get("unrealised") or 0)
            for p in (positions or [])
            if abs(int(p.get("quantity") or 0)) > 0
        )
        realized_pnl = total_pnl - unrealized_pnl
    except Exception as e:
        positions_error = str(e)
    finally:
        await upstox.aclose()

    return total_pnl, unrealized_pnl, realized_pnl, positions_error


async def _fetch_strategy_live_pnl(pool, redis) -> tuple[float, float, float, dict, str | None]:
    """Compute strategy P&L from app-owned signals plus live LTPs.

    This stays signal-scoped, so it does not get distorted when Upstox aggregates
    multiple trades on the same instrument into a single day position row.
    """
    async with pool.acquire() as conn:
        realized_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(pnl), 0) AS realized_pnl
            FROM signals
            WHERE pnl IS NOT NULL
              AND (signal_at AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date
            """
        )
        active_rows = await conn.fetch(
            """
            SELECT
                s.id,
                s.instrument_key,
                s.action,
                s.entry_price,
                COALESCE(
                    (
                        SELECT SUM(te.quantity)
                        FROM gtt_rules gr
                        JOIN trade_executions te ON te.order_id = gr.order_id
                        WHERE gr.signal_id = s.id
                          AND gr.strategy = 'ENTRY'
                    ),
                    0
                ) AS executed_qty
            FROM signals s
            WHERE s.status = 'ACTIVE'
              AND s.entry_price IS NOT NULL
              AND s.instrument_key IS NOT NULL
              AND (s.signal_at AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date
            ORDER BY s.id
            """
        )

    realized_pnl = float(realized_row["realized_pnl"] or 0)
    instrument_keys = sorted({str(row["instrument_key"]) for row in active_rows if row["instrument_key"]})
    ltp_by_instrument: dict[str, float] = {}
    ltp_error: str | None = None

    if instrument_keys and redis:
        cached = await redis.mget(*[f"ltp:{key}" for key in instrument_keys])
        for key, raw in zip(instrument_keys, cached):
            if raw is None:
                continue
            try:
                ltp_by_instrument[key] = float(raw)
            except (TypeError, ValueError):
                continue

    missing_keys = [key for key in instrument_keys if key not in ltp_by_instrument]
    if missing_keys:
        upstox = UpstoxClient(settings.upstox_access_token, settings.upstox_base_url)
        try:
            ltp_by_instrument.update(await upstox.get_ltp_multi(missing_keys))
        except Exception as e:
            ltp_error = str(e)
        finally:
            await upstox.aclose()

    unrealized_pnl = 0.0
    signals_priced = 0
    missing_signal_ids: list[int] = []
    for row in active_rows:
        instrument_key = str(row["instrument_key"])
        ltp = ltp_by_instrument.get(instrument_key)
        entry_price = float(row["entry_price"] or 0)
        executed_qty = int(row["executed_qty"] or 0)
        if ltp is None or executed_qty <= 0:
            missing_signal_ids.append(int(row["id"]))
            continue
        if str(row["action"]).upper() == "SELL":
            unrealized_pnl += (entry_price - ltp) * executed_qty
        else:
            unrealized_pnl += (ltp - entry_price) * executed_qty
        signals_priced += 1

    unrealized_pnl = round(unrealized_pnl, 2)
    total_pnl = round(realized_pnl + unrealized_pnl, 2)
    details = {
        "active_signal_count": len(active_rows),
        "signals_priced": signals_priced,
        "missing_signal_ids": missing_signal_ids,
        "ltp_keys": instrument_keys,
    }
    return total_pnl, unrealized_pnl, realized_pnl, details, ltp_error


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
async def get_live_pnl(request: Request, pool=Depends(get_pool)):
    """Real-time strategy P&L for the dashboard.

    - Realized P&L comes from signal-level fills recorded by the app.
    - Unrealized P&L comes from active signal entry prices vs live LTP.
    - Live LTP is sourced from the Upstox market data feed cache in Redis,
      with REST LTP used only as a fallback for missing keys.
    - Broker/account-level P&L from Upstox positions is returned alongside it
      as context, but not used as the primary dashboard number because Upstox
      aggregates by instrument rather than by app signal.
    """
    redis = request.app.state.redis
    total_pnl, unrealized_pnl, realized_pnl, details, ltp_error = await _fetch_strategy_live_pnl(pool, redis)
    broker_total, broker_unrealized, broker_realized, broker_error = await _fetch_upstox_live_pnl()

    return {
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": realized_pnl,
        "total_pnl": total_pnl,
        "source": {
            "total": "signals + live_ltp_cache",
            "unrealized": "upstox_market_data_feed (redis cache) with REST fallback",
            "realized": "signals table realized fills for today",
        },
        "details": details,
        "unrealized_error": ltp_error,
        "broker_pnl": {
            "total_pnl": broker_total,
            "unrealized_pnl": broker_unrealized,
            "realized_pnl": broker_realized,
            "error": broker_error,
            "source": "upstox_positions_api /v2/portfolio/short-term-positions",
        },
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
async def get_summary(request: Request, pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'ACTIVE')       AS active_signals,
                COUNT(*) FILTER (WHERE status = 'PENDING')      AS pending_signals,
                COUNT(*) FILTER (WHERE status = 'TARGET1_HIT')  AS targets_hit,
                COUNT(*) FILTER (WHERE status = 'STOPLOSS_HIT') AS stoploss_hit
            FROM signals
            WHERE (signal_at AT TIME ZONE 'Asia/Kolkata')::date = (now() AT TIME ZONE 'Asia/Kolkata')::date
            """
        )
    result = dict(row)
    redis = request.app.state.redis
    total_pnl, _unrealized_pnl, realized_pnl, details, ltp_error = await _fetch_strategy_live_pnl(pool, redis)
    broker_total, broker_unrealized, broker_realized, broker_error = await _fetch_upstox_live_pnl()
    result["pnl_source"] = "signals + live_ltp_cache"
    result["total_pnl"] = total_pnl
    result["realized_pnl"] = realized_pnl
    result["pnl_error"] = ltp_error
    result["pnl_details"] = details
    result["broker_total_pnl"] = broker_total
    result["broker_realized_pnl"] = broker_realized
    result["broker_unrealized_pnl"] = broker_unrealized
    result["broker_pnl_error"] = broker_error
    result["trader_profile"] = settings.trader_profile.upper()
    return result
