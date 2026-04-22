-- 003_entry_range_touched.sql
-- Persist entry_range_touched state so price_monitor restarts don't reset the gate.

ALTER TABLE signals
    ADD COLUMN IF NOT EXISTS entry_range_touched BOOLEAN NOT NULL DEFAULT FALSE;
