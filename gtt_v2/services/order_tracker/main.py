"""Order Tracker — Portfolio WebSocket → GTT/order lifecycle → P&L → PostgreSQL."""
import asyncio
import json
import structlog

from shared.config import settings, configure_logging
from shared.db.postgres import create_pool, run_migrations
from shared.db.signals import update_signal_status, update_signal_pnl
from shared.db.gtt_rules import (
    upsert_gtt_rule,
    get_gtt_rule_by_order_id,
    get_gtt_rule_by_gtt_order_id,
    get_gtt_rule_by_child_order_id,
    get_gtt_rules_by_gtt_order_id,
)
from shared.db.order_updates import (
    insert_order_update, insert_trade_execution, get_trades_for_order, weighted_avg_price
)
from shared.db.writer import AsyncDBWriter
from shared.redis.client import create_redis
from shared.rules.pnl_guard import DailyPnLGuard
from shared.upstox.client import UpstoxClient
from shared.upstox.portfolio_ws import PortfolioWebSocket
from shared.heartbeat import heartbeat_loop

log = structlog.get_logger()

# GTT status → signal status mapping
# Upstox MULTIPLE GTT lifecycle:
#   "pending"   — GTT accepted, waiting for entry trigger price to be hit
#   "triggered" — entry leg fired, now in a live position (target/SL are active)
#   "cancelled" — GTT cancelled
#   "expired"   — GTT expired without triggering
_GTT_STATUS_MAP = {
    "pending":     "PENDING",
    "triggered":   "ACTIVE",
    "cancelled":   "CANCELLED",
    "expired":     "EXPIRED",
}

_ORDER_TERMINAL_STATUSES = {"complete", "cancelled", "rejected"}


def _derive_strategy(
    order_txn_type: str,
    signal_action: str,
    fill_price: float | None,
    gtt_rules: list[dict],
) -> str:
    """Determine which leg (ENTRY/TARGET/STOPLOSS) an order fill belongs to.

    Logic:
    - Same transaction direction as the signal → ENTRY leg.
    - Opposite direction → exit leg (TARGET or STOPLOSS).
      Distinguish by comparing fill_price to the stored trigger_price of each exit leg.
    """
    order_txn = (order_txn_type or "").upper()
    action = (signal_action or "BUY").upper()

    if order_txn == action:
        return "ENTRY"

    # Exit leg — pick TARGET vs STOPLOSS by proximity of fill_price to stored trigger_price
    if fill_price is not None:
        target_row = next((r for r in gtt_rules if r.get("strategy") == "TARGET"), None)
        stoploss_row = next((r for r in gtt_rules if r.get("strategy") == "STOPLOSS"), None)
        if target_row and stoploss_row:
            tp = float(target_row.get("trigger_price") or 0)
            sp = float(stoploss_row.get("trigger_price") or 0)
            dist_target = abs(fill_price - tp) if tp else float("inf")
            dist_sl = abs(fill_price - sp) if sp else float("inf")
            return "TARGET" if dist_target <= dist_sl else "STOPLOSS"
        if target_row:
            return "TARGET"
        if stoploss_row:
            return "STOPLOSS"

    # Fallback: can't determine exit leg type — default to TARGET to avoid missing SL_HIT
    return "TARGET"


class OrderTracker:
    def __init__(self, upstox, pool, redis, db_writer, pnl_guard):
        self._upstox = upstox
        self._pool = pool
        self._redis = redis
        self._db_writer = db_writer
        self._pnl_guard = pnl_guard

    async def handle(self, msg: dict) -> None:
        feeds = msg.get("feeds") or {}
        for _key, feed in feeds.items():
            await self._dispatch(feed)

    async def _dispatch(self, feed: dict) -> None:
        # Portfolio WS returns order_update or gtt updates
        if "orderUpdate" in feed:
            await self._handle_order_update(feed["orderUpdate"])
        elif "gttUpdate" in feed:
            await self._handle_gtt_update(feed["gttUpdate"])

    async def _handle_gtt_update(self, data: dict) -> None:
        gtt_id = data.get("gttOrderId") or data.get("id")
        status = str(data.get("status", "")).lower()
        if not gtt_id:
            return

        log.info("gtt_update", gtt_id=gtt_id, status=status)

        async with self._pool.acquire() as conn:
            rules = await get_gtt_rules_by_gtt_order_id(conn, str(gtt_id))
            if not rules:
                log.warning("gtt_rules_not_found", gtt_id=gtt_id)
                return

            signal_id = rules[0]["signal_id"]
            # Update status on every leg row for this GTT
            for rule in rules:
                await upsert_gtt_rule(conn, {
                    "signal_id": signal_id,
                    "gtt_order_id": str(gtt_id),
                    "strategy": rule["strategy"],
                    "status": status,
                    "payload_json": json.dumps(data),
                })

            # Only map terminal GTT statuses (cancelled/expired) from gttUpdate.
            # ACTIVE/TARGET1_HIT/STOPLOSS_HIT are driven exclusively by _process_filled_order.
            terminal_map = {"cancelled": "CANCELLED", "expired": "EXPIRED"}
            mapped_status = terminal_map.get(status)
            if mapped_status:
                await update_signal_status(conn, signal_id, mapped_status)

    async def _handle_order_update(self, data: dict) -> None:
        order_id = data.get("orderId") or data.get("order_id")
        gtt_id = data.get("gttOrderId") or data.get("gtt_order_id") or data.get("id")
        status = str(data.get("status", "")).lower()
        avg_price = data.get("averagePrice") or data.get("average_price")
        quantity = data.get("quantity")
        if not order_id:
            return

        log.info("order_update", order_id=order_id, status=status)

        async with self._pool.acquire() as conn:
            rule = await get_gtt_rule_by_child_order_id(conn, str(order_id))
            if not rule and gtt_id:
                # Determine which leg this order belongs to before linking
                rules = await get_gtt_rules_by_gtt_order_id(conn, str(gtt_id))
                if rules:
                    signal_action = await conn.fetchval(
                        "SELECT action FROM signals WHERE id = $1", rules[0]["signal_id"]
                    )
                    fill_price = float(avg_price) if avg_price else None
                    strategy = _derive_strategy(
                        data.get("transactionType") or data.get("transaction_type", ""),
                        signal_action or "BUY",
                        fill_price,
                        rules,
                    )
                    rule = next((r for r in rules if r.get("strategy") == strategy), rules[0])
                    # Link this child order ID to the correct leg row
                    await upsert_gtt_rule(conn, {
                        "signal_id": rule["signal_id"],
                        "gtt_order_id": rule["gtt_order_id"],
                        "strategy": rule["strategy"],
                        "status": rule.get("status"),
                        "order_id": str(order_id),
                        "payload_json": json.dumps(data),
                    })

            await insert_order_update(conn, {
                "signal_id": rule["signal_id"] if rule else None,
                "order_id": str(order_id),
                "status": status,
                "average_price": float(avg_price) if avg_price else None,
                "quantity": int(quantity) if quantity else None,
                "payload_json": json.dumps(data),
            })

        if status not in _ORDER_TERMINAL_STATUSES:
            return

        if status == "complete":
            await self._process_filled_order(order_id, data)

    async def _process_filled_order(self, order_id: str, data: dict) -> None:
        try:
            trades_resp = await self._upstox.get_order_trades(order_id)
            trades = trades_resp.get("data", [])
        except Exception as e:
            log.error("order_trades_fetch_failed", order_id=order_id, error=str(e))
            return

        pnl = 0.0
        async with self._pool.acquire() as conn:
            for trade in trades:
                await insert_trade_execution(conn, {
                    "order_id": str(order_id),
                    "trade_id": trade.get("trade_id"),
                    "quantity": trade.get("quantity"),
                    "price": trade.get("average_price") or trade.get("price"),
                    "side": data.get("transaction_type", "BUY"),
                    # signal_id linked below once rule is resolved
                })

            all_trades = await get_trades_for_order(conn, str(order_id))
            fill_price = weighted_avg_price(all_trades)
            if fill_price is None:
                return

            # Resolve which leg row this fill belongs to
            rule = await get_gtt_rule_by_child_order_id(conn, str(order_id))
            if not rule:
                gtt_id = data.get("gttOrderId") or data.get("gtt_order_id") or data.get("id")
                if gtt_id:
                    rules = await get_gtt_rules_by_gtt_order_id(conn, str(gtt_id))
                    if rules:
                        signal_action = await conn.fetchval(
                            "SELECT action FROM signals WHERE id = $1", rules[0]["signal_id"]
                        )
                        strategy = _derive_strategy(
                            data.get("transactionType") or data.get("transaction_type", ""),
                            signal_action or "BUY",
                            fill_price,
                            rules,
                        )
                        rule = next((r for r in rules if r.get("strategy") == strategy), rules[0])
                        await upsert_gtt_rule(conn, {
                            "signal_id": rule["signal_id"],
                            "gtt_order_id": rule["gtt_order_id"],
                            "strategy": rule["strategy"],
                            "status": rule.get("status"),
                            "order_id": str(order_id),
                            "payload_json": json.dumps(data),
                        })
            if not rule:
                log.warning("gtt_rule_not_found_for_order", order_id=order_id)
                return

            signal_id = rule["signal_id"]
            strategy = rule.get("strategy", "")

            # Back-fill signal_id onto the trade_execution rows we just inserted
            await conn.execute(
                "UPDATE trade_executions SET signal_id = $1 WHERE order_id = $2 AND signal_id IS NULL",
                signal_id, str(order_id),
            )

            signal_row = await conn.fetchrow(
                """
                SELECT action, entry_price, stoploss_adj, targets_adj, status
                FROM signals
                WHERE id = $1
                """,
                signal_id,
            )
            signal_action = (signal_row["action"] if signal_row else "BUY") or "BUY"
            entry_price_ref = float(signal_row["entry_price"]) if signal_row and signal_row["entry_price"] else None
            stoploss_adj = float(signal_row["stoploss_adj"]) if signal_row and signal_row["stoploss_adj"] else None
            targets_adj = signal_row["targets_adj"] if signal_row and signal_row["targets_adj"] else []
            target1_adj = float(targets_adj[0]) if targets_adj else None

            is_target_hit = False
            is_stoploss_hit = False
            if signal_action == "BUY":
                is_target_hit = target1_adj is not None and fill_price >= (target1_adj - 0.75)
                is_stoploss_hit = stoploss_adj is not None and fill_price <= (stoploss_adj + 0.75)
            else:
                is_target_hit = target1_adj is not None and fill_price <= (target1_adj + 0.75)
                is_stoploss_hit = stoploss_adj is not None and fill_price >= (stoploss_adj - 0.75)

            if strategy == "ENTRY" and not is_target_hit and not is_stoploss_hit:
                # Store the entry fill price so P&L can be computed when exit fires
                await conn.execute(
                    "UPDATE signals SET entry_price = $1, last_event_at = now() WHERE id = $2",
                    fill_price, signal_id,
                )
                await update_signal_status(conn, signal_id, "ACTIVE")
                log.info("entry_fill_recorded", order_id=order_id, entry_price=fill_price, signal_id=signal_id)
                return

            # EXIT leg (TARGET or STOPLOSS): compute realized P&L
            entry_row = await conn.fetchrow(
                "SELECT entry_price, action FROM signals WHERE id = $1", signal_id
            )
            entry_price = float(entry_row["entry_price"]) if entry_row and entry_row["entry_price"] else entry_price_ref
            if entry_price is None:
                log.warning("no_entry_price_for_pnl", signal_id=signal_id, order_id=order_id)
                entry_price = fill_price  # fallback — P&L will be 0 but won't crash

            qty = sum(t.get("quantity", 0) for t in all_trades)
            action = (entry_row["action"] if entry_row else "BUY") or "BUY"
            pnl = (fill_price - entry_price) * qty if action == "BUY" else (entry_price - fill_price) * qty

            if is_target_hit or strategy == "TARGET":
                new_status = "TARGET1_HIT"
            elif is_stoploss_hit or strategy == "STOPLOSS":
                new_status = "STOPLOSS_HIT"
            else:
                new_status = "ACTIVE"

            await update_signal_pnl(conn, signal_id, entry_price, fill_price, pnl)
            await update_signal_status(conn, signal_id, new_status)
            log.info("order_pnl_computed", order_id=order_id, pnl=pnl, entry=entry_price, exit=fill_price)

        if pnl != 0:
            await self._pnl_guard.record(self._redis, self._pool, pnl)

    async def startup_backfill(self) -> None:
        """Sync GTT state from Upstox on startup to handle events missed while offline."""
        try:
            resp = await self._upstox.get_all_gtts()
            data = resp.get("data", [])
            gtts = data if isinstance(data, list) else data.get("gtt_order_list", [])
            for gtt in gtts:
                gtt_id = gtt.get("id")
                status = str(gtt.get("status", "")).lower()
                if not gtt_id:
                    continue
                async with self._pool.acquire() as conn:
                    rules = await get_gtt_rules_by_gtt_order_id(conn, str(gtt_id))
                    for rule in rules:
                        await upsert_gtt_rule(conn, {
                            "signal_id": rule["signal_id"],
                            "gtt_order_id": str(gtt_id),
                            "strategy": rule["strategy"],
                            "status": status,
                            "payload_json": json.dumps(gtt),
                        })
            log.info("startup_backfill_done", gtt_count=len(gtts))
        except Exception as e:
            log.warning("startup_backfill_failed", error=str(e))


async def main() -> None:
    configure_logging("order-tracker")
    log.info("order_tracker_starting")

    pool = await create_pool(settings.postgres_dsn)
    await run_migrations(pool)
    redis = await create_redis(settings.redis_host, settings.redis_port)

    upstox = UpstoxClient(settings.upstox_access_token, settings.upstox_base_url)
    db_writer = AsyncDBWriter(pool)
    pnl_guard = DailyPnLGuard()
    tracker = OrderTracker(upstox, pool, redis, db_writer, pnl_guard)

    await tracker.startup_backfill()

    ws = PortfolioWebSocket(
        url_factory=upstox.get_portfolio_stream_url,
        on_message=tracker.handle,
    )

    writer_task = asyncio.create_task(db_writer.run())
    asyncio.create_task(heartbeat_loop())
    try:
        await ws.run()
    finally:
        await db_writer.stop()
        writer_task.cancel()
        await upstox.aclose()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
