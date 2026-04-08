# Upstox GTT Stack

Unified Telegram -> Redis -> Upstox GTT execution stack.

This repo now contains:
- Telegram AI listener
- Redis stream consumer
- Upstox GTT placer
- Upstox event-driven order tracker

Run everything from one command:

```bash
cd /Users/yash_2111825/Projects/UpstoxGTT
python3 app.py
```

If you want the venv python explicitly:

```bash
.venv/bin/python app.py
```

## Runtime Flow

```text
Telegram Channel
    ↓
telegram_ai_listener.py
    ↓
Redis stream (raw_trade_signals)
    ↓
gtt_strategy.py
    ↓
Upstox GTT order placement
    ↓
upstox_order_tracker.py
    ↓
SQLite DB + logs
```

## Main Files

- `app.py` : single-entry supervisor that starts all long-running services
- `telegram_ai_listener.py` : listens to Telegram and publishes parsed signals to Redis
- `ParseerWithAI/ai_signal_parser.py` : Gemini/OpenAI-based parser
- `gtt_strategy.py` : consumes Redis signals and places GTT orders
- `upstox_order_tracker.py` : listens to Upstox portfolio updates and records real execution state
- `db.py` : SQLite schema and persistence helpers

## Setup

```bash
cd /Users/yash_2111825/Projects/UpstoxGTT
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` with:
- Telegram credentials
- AI provider/API key
- Upstox access token
- Redis host/port

The Telegram session file used by the listener is `session_name.session` at repo root.

## Running

Primary entrypoint:

```bash
python3 app.py
```

Shell wrapper:

```bash
./run.sh
```

## Logs

Logs are written under `logs/`:
- `telegram_ai_listener.log`
- `gtt_upstox.log`
- `upstox_order_tracker.log`

## Notes

- `upstox_order_tracker.py` is the source of truth for entry/target/stoploss execution tracking.
- The old LTP-based `price_monitor.py` is still present only as fallback/reference and is no longer started by default.
- SQLite data is stored in `gtt_signals.db`.
