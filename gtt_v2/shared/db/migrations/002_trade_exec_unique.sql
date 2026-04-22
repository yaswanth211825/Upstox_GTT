-- 002_trade_exec_unique.sql
-- Prevent duplicate trade_execution rows on WebSocket reconnect.
-- ON CONFLICT DO NOTHING in insert_trade_execution had no UNIQUE to fire against.
-- trade_id can be NULL for partial fills; use a partial unique index to handle that safely.

CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_execution_trade_id
    ON trade_executions (trade_id)
    WHERE trade_id IS NOT NULL;
