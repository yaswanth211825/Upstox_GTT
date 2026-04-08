# Upstox GTT Strategy

Standalone Redis → Upstox GTT (Good Till Triggered) order strategy.

Reads signals from a Redis stream and places multi-leg GTT orders directly to Upstox API v3. No dependency on OpenAlgo or any other framework.

---

## How It Works

```
Redis Stream (raw_trade_signals)
        ↓
  Parse signal fields
        ↓
  Resolve instrument token (from Upstox instrument cache)
        ↓
  Place GTT MULTIPLE order to Upstox API v3
  [ENTRY + TARGET + STOPLOSS in one shot]
        ↓
  Upstox handles all triggers server-side
```

GTT orders survive script restarts. Once placed, Upstox will trigger entry/exit automatically.

---

## Setup

### 1. Install dependencies

```bash
cd /Users/yash_2111825/Projects/UpstoxGTT

# Create a venv (optional but recommended)
python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure `.env`

Edit `.env` and set your Upstox access token:

```bash
UPSTOX_ACCESS_TOKEN=eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOi...
```

All other defaults work out of the box for local Redis.

### 3. Run

```bash
python3 gtt_strategy.py
```

---

## Redis Signal Format

Push messages to the `raw_trade_signals` stream:

```json
{
  "action": "BUY",
  "instrument": "SENSEX",
  "strike": "85200",
  "option_type": "PE",
  "entry_low": "400",
  "stoploss": "380",
  "targets": "480/530/650",
  "expiry": "18th DECEMBER",
  "product": "I",
  "quantity": "1"
}
```

Strict production template is also supported through the `message` field:

```json
{
  "message": "ENTRY|SENSEX|72700PE|230|240|200|270|2026-04-02|1"
}
```

Template fields:

```text
ENTRY|UNDERLYING|OPTION|ENTRY_LOW|ENTRY_HIGH|STOPLOSS|TARGET1|EXPIRY|LOTS
```

Example:

```text
ENTRY|SENSEX|72700PE|230|240|200|270|2026-04-02|1
```

**Test with redis-cli:**

```bash
redis-cli XADD raw_trade_signals "*" \
  action "BUY" \
  instrument "SENSEX" \
  strike "85200" \
  option_type "PE" \
  entry_low "400" \
  stoploss "380" \
  targets "480/530/650" \
  expiry "18th DECEMBER" \
  product "I" \
  quantity "1"
```

Or using the strict template:

```bash
redis-cli XADD raw_trade_signals "*" \
  message "ENTRY|SENSEX|72700PE|230|240|200|270|2026-04-02|1"
```

---

## GTT Order Structure (sent to Upstox)

```json
{
  "type": "MULTIPLE",
  "quantity": 20,
  "product": "I",
  "instrument_token": "BSE_FO|12345",
  "transaction_type": "BUY",
  "rules": [
    { "strategy": "ENTRY",    "trigger_type": "ABOVE",     "trigger_price": 400 },
    { "strategy": "TARGET",   "trigger_type": "IMMEDIATE", "trigger_price": 480 },
    { "strategy": "STOPLOSS", "trigger_type": "IMMEDIATE", "trigger_price": 380 }
  ]
}
```

- `ENTRY ABOVE` for BUY, `ENTRY BELOW` for SELL
- `TARGET` uses the first/minimum target price supplied
- Quantity is automatically converted from lots → shares
- Env lot-size overrides like `LOT_SIZE_SENSEX_PE=20` take priority over instrument metadata

---

## Supported Expiry Date Formats

| Input | Parsed |
|-------|--------|
| `18th DECEMBER` | 2025-12-18 |
| `06NOV25` | 2025-11-06 |
| `06 NOV 2025` | 2025-11-06 |
| `26 DEC 25` | 2025-12-26 |
| `2026-04-02` | 2026-04-02 |

---

## Monitoring

```bash
# View processed signal IDs
redis-cli ZRANGE processed_gtt_signals 0 -1 WITHSCORES

# View metadata for a specific signal
redis-cli HGETALL "gtt_metadata:1765690712123-0"

# Check last processed ID (for resume)
redis-cli GET processed_gtt:last_id

# Live logs
tail -f logs/gtt_upstox.log
```

---

## Error Reference

| Error | Fix |
|-------|-----|
| `UPSTOX_ACCESS_TOKEN not set` | Add token to `.env` |
| `Instrument not found in Upstox` | Check strike / expiry / option_type |
| `HTTP 401` | Token expired — refresh via Upstox OAuth |
| `HTTP 400: Invalid instrument_token` | Restart script to reload instrument cache |
| `Failed to connect to Redis` | Ensure Redis is running: `redis-server` |

---

## Notes

- GTT orders are valid for **1 year** if condition is not met
- Once ENTRY executes, TARGET and STOPLOSS become active for 365 days
- Product codes: `I` = Intraday, `D` = Delivery, `MTF` = Margin Trading
- Instrument cache is loaded once at startup from Upstox (~30,000 instruments)

---

## Upstox API Reference

- Place GTT: https://upstox.com/developer/api-documentation/place-gtt-order
- Developer Community: https://community.upstox.com/c/developer-api/15
