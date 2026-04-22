-- 001_initial.sql: core schema

CREATE TABLE IF NOT EXISTS accounts (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    profile     TEXT NOT NULL CHECK (profile IN ('SAFE','RISK')),
    capital     NUMERIC(12,2) NOT NULL DEFAULT 100000,
    active      BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO accounts (name, profile, capital) VALUES ('default', 'SAFE', 100000)
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS signals (
    id                  BIGSERIAL PRIMARY KEY,
    redis_message_id    TEXT NOT NULL UNIQUE,
    signal_hash         TEXT UNIQUE,
    action              TEXT NOT NULL CHECK (action IN ('BUY','SELL')),
    underlying          TEXT NOT NULL,
    strike              INTEGER,
    option_type         TEXT CHECK (option_type IN ('CE','PE')),
    expiry              DATE,
    product             TEXT NOT NULL DEFAULT 'I',
    quantity_lots       INTEGER NOT NULL,
    instrument_key      TEXT,

    -- raw prices (before buffer)
    entry_low_raw       NUMERIC(10,2) NOT NULL,
    entry_high_raw      NUMERIC(10,2),
    stoploss_raw        NUMERIC(10,2) NOT NULL,
    targets_raw         NUMERIC(10,2)[],

    -- adjusted prices (after buffer)
    entry_low_adj       NUMERIC(10,2),
    entry_high_adj      NUMERIC(10,2),
    stoploss_adj        NUMERIC(10,2),
    targets_adj         NUMERIC(10,2)[],

    -- classification
    trade_type          TEXT CHECK (trade_type IN ('BUY_ABOVE','RANGING','AVERAGE')),
    safe_only           BOOLEAN NOT NULL DEFAULT false,
    risk_only           BOOLEAN NOT NULL DEFAULT false,

    -- lifecycle FSM
    status              TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN (
                            'PENDING','ACTIVE','TARGET1_HIT','TARGET2_HIT','TARGET3_HIT',
                            'STOPLOSS_HIT','EXPIRED','CANCELLED','FAILED','BLOCKED')),
    block_reason        TEXT,
    gtt_order_ids       TEXT[],
    entry_price         NUMERIC(10,2),
    exit_price          NUMERIC(10,2),
    pnl                 NUMERIC(12,2),
    ltp_at_placement    NUMERIC(10,2),

    -- timestamps
    signal_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    gtt_placed_at       TIMESTAMPTZ,
    activated_at        TIMESTAMPTZ,
    resolved_at         TIMESTAMPTZ,
    last_event_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_status ON signals (status);
CREATE INDEX IF NOT EXISTS idx_signals_instrument_key ON signals (instrument_key);
CREATE INDEX IF NOT EXISTS idx_signals_signal_at ON signals (signal_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_underlying ON signals (underlying, expiry);

CREATE TABLE IF NOT EXISTS order_events (
    id          BIGSERIAL PRIMARY KEY,
    signal_id   BIGINT NOT NULL REFERENCES signals(id),
    event_type  TEXT NOT NULL,
    event_data  JSONB NOT NULL DEFAULT '{}',
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_order_events_signal_id ON order_events (signal_id);

CREATE TABLE IF NOT EXISTS gtt_rules (
    id               BIGSERIAL PRIMARY KEY,
    signal_id        BIGINT NOT NULL REFERENCES signals(id),
    gtt_order_id     TEXT NOT NULL,
    strategy         TEXT NOT NULL CHECK (strategy IN ('ENTRY','TARGET','STOPLOSS')),
    trigger_type     TEXT,
    trigger_price    NUMERIC(10,2),
    transaction_type TEXT,
    status           TEXT,
    order_id         TEXT,
    message          TEXT,
    payload_json     JSONB,
    last_event_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (gtt_order_id, strategy)
);

CREATE INDEX IF NOT EXISTS idx_gtt_rules_signal_id ON gtt_rules (signal_id);
CREATE INDEX IF NOT EXISTS idx_gtt_rules_gtt_order_id ON gtt_rules (gtt_order_id);

CREATE TABLE IF NOT EXISTS order_updates (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT REFERENCES signals(id),
    order_id        TEXT NOT NULL,
    status          TEXT,
    message         TEXT,
    average_price   NUMERIC(10,2),
    quantity        INTEGER,
    payload_json    JSONB,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trade_executions (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT REFERENCES signals(id),
    order_id        TEXT NOT NULL,
    trade_id        TEXT,
    quantity        INTEGER,
    price           NUMERIC(10,2),
    side            TEXT CHECK (side IN ('BUY','SELL')),
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS price_events (
    id              BIGSERIAL PRIMARY KEY,
    signal_id       BIGINT REFERENCES signals(id),
    instrument_key  TEXT NOT NULL,
    ltp             NUMERIC(10,2) NOT NULL,
    event_type      TEXT NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    id              BIGSERIAL PRIMARY KEY,
    trading_date    DATE NOT NULL UNIQUE,
    realized_pnl    NUMERIC(12,2) NOT NULL DEFAULT 0,
    trade_count     INTEGER NOT NULL DEFAULT 0,
    pnl_limit_hit   BOOLEAN NOT NULL DEFAULT false,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
