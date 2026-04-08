"""
Redis Streams -> Upstox GTT Order Strategy
Reads signals from Redis and places GTT (Good Till Triggered) orders directly to Upstox API v3.

Expected Redis stream message format (with structured fields):
{
  "action": "BUY",                 # BUY / SELL
  "instrument": "SENSEX",          # NIFTY, SENSEX, BANKNIFTY, etc
  "strike": "85200",               # string strike
  "option_type": "PE",             # PE or CE
  "entry_low": "400",              # entry trigger price
  "stoploss": "380",               # stoploss price
  "targets": "480/530/650",        # target prices (will use minimum)
  "expiry": "18th DECEMBER",       # expiry date
  "product": "I",                  # I (Intraday) / D (Delivery) / MTF
  "quantity": "1"                  # quantity
}
"""

import os
import json
import logging
import time
import re
import requests
import gzip
from datetime import datetime
from dotenv import load_dotenv
from typing import Any, Dict, Optional, List

import db

# Load .env from same directory as this script
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

PRINT_DEBUG = True

# Logger setup
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(_log_dir, exist_ok=True)

logger = logging.getLogger("gtt_upstox")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler_file = logging.FileHandler(os.path.join(_log_dir, "gtt_upstox.log"))
    _formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    _handler.setFormatter(_formatter)
    _handler_file.setFormatter(_formatter)
    logger.addHandler(_handler)
    logger.addHandler(_handler_file)
logger.setLevel(logging.DEBUG if PRINT_DEBUG else logging.INFO)

# Redis Configuration
REDIS_HOST = os.getenv("REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_STREAM = os.getenv("REDIS_STREAM_NAME", "raw_trade_signals")
REDIS_START_FROM = os.getenv("REDIS_START_FROM", "resume")

# Dedupe keys
PROCESSED_ZSET = os.getenv("PROCESSED_GTT_ZSET", "processed_gtt_signals")
LAST_ID_KEY = os.getenv("LAST_GTT_ID_KEY", "processed_gtt:last_id")
PROCESSED_ZSET_MAX = int(os.getenv("PROCESSED_ZSET_MAX", "10000"))

# Upstox Configuration
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")
UPSTOX_BASE_URL = os.getenv("UPSTOX_BASE_URL", "https://api.upstox.com")
DEFAULT_QUANTITY = int(os.getenv("DEFAULT_QUANTITY", "4"))
STRICT_TEMPLATE_ENABLED = os.getenv("STRICT_TEMPLATE_ENABLED", "false").lower() in ("1", "true", "yes", "on")
SIGNAL_TEMPLATE_DELIMITER = os.getenv("SIGNAL_TEMPLATE_DELIMITER", "|")

logger.info("🔁 GTT Strategy Bot (Direct Upstox API v3) is running.")

# Import Redis
try:
    import redis
except Exception as e:
    raise RuntimeError("Please install redis-py (pip install redis)") from e

# Redis client
redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
try:
    redis_client.ping()
    if PRINT_DEBUG:
        logger.info(f"✅ Connected to Redis {REDIS_HOST}:{REDIS_PORT}, stream={REDIS_STREAM}")
except Exception as e:
    raise SystemExit(f"Failed to connect to Redis: {e}")

# Validate Upstox token
if not UPSTOX_ACCESS_TOKEN:
    raise SystemExit("UPSTOX_ACCESS_TOKEN not set in environment/.env")
else:
    logger.info(f"✅ Upstox Access Token configured")


# ============================================================================
# INSTRUMENT CACHE - Load instruments from Upstox once at startup
# ============================================================================

class InstrumentCache:
    """Cache instruments from Upstox to avoid repeated downloads"""
    def __init__(self):
        self.instruments = {}
        self.loaded = False
        self.last_reload = None

    def load_instruments(self):
        """Download and cache instruments from Upstox"""
        try:
            logger.info("📥 Loading instruments from Upstox...")

            # Download complete instruments list (NSE + BSE)
            url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
            resp = requests.get(url, timeout=30)

            if resp.status_code != 200:
                logger.error(f"❌ Failed to download instruments: {resp.status_code}")
                return False

            # Decompress gzip
            instruments_data = json.loads(gzip.decompress(resp.content))

            # Build searchable cache: index by "NSE_FO|KEY" format
            for item in instruments_data:
                key = item.get("instrument_key")
                if key:
                    self.instruments[key] = item

            self.loaded = True
            self.last_reload = datetime.now()
            logger.info(f"✅ Loaded {len(self.instruments)} instruments")
            return True

        except Exception as e:
            logger.exception(f"❌ Failed to load instruments:")
            return False

    def find_instrument(self, underlying: str, strike: int, opt_type: str, expiry_str: str) -> Optional[Dict]:
        """Find instrument by underlying, strike, option type, and expiry"""
        if not self.loaded:
            if not self.load_instruments():
                return None

        # Parse expiry to standard format (YYYY-MM-DD)
        expiry_date = parse_expiry_date(expiry_str)
        if not expiry_date:
            logger.warning(f"⚠️ Could not parse expiry: {expiry_str}")
            return None

        # Convert YYYY-MM-DD to timestamp for comparison
        try:
            target_dt = datetime.strptime(expiry_date, "%Y-%m-%d")
            # Upstox stores expiry as Unix timestamp in milliseconds (end of trading day)
            target_ts_day = int(target_dt.timestamp() * 1000)
        except:
            logger.warning(f"⚠️ Could not convert expiry to timestamp: {expiry_date}")
            return None

        # Search for matching instrument
        for key, item in self.instruments.items():
            # Filter by exchange (NSE_FO for indices, BSE_FO for sensex/bankex)
            exchange = item.get("segment", "")
            if exchange not in ("NSE_FO", "BSE_FO"):
                continue

            # Check if it's an option (new instruments use instrument_type directly)
            inst_type = item.get("instrument_type", "")
            if inst_type not in ("CE", "PE"):  # Direct CE/PE check for new format
                continue

            # Match underlying_symbol (more reliable than trading_symbol)
            underlying_symbol = str(item.get("underlying_symbol", "")).upper()
            if underlying.upper() != underlying_symbol:
                continue

            # Check strike
            if item.get("strike_price") != strike:
                continue

            # Check option type
            if item.get("instrument_type", "").upper() != opt_type.upper():
                continue

            # Check expiry - compare dates (Upstox stores as timestamp)
            item_expiry_ts = item.get("expiry")
            if item_expiry_ts:
                item_expiry_date = datetime.fromtimestamp(item_expiry_ts / 1000).strftime("%Y-%m-%d")
                if item_expiry_date == expiry_date:
                    logger.info(f"✅ Found instrument: {key} ({item.get('trading_symbol')})")
                    return item

        logger.warning(f"⚠️ Could not find instrument: {underlying} {strike}{opt_type} {expiry_str}")
        return None


def parse_expiry_date(expiry_str: str) -> Optional[str]:
    """
    Parse expiry string to YYYY-MM-DD format (Upstox expects this)
    Accepts: "6th NOVEMBER", "06 NOV 2025", "06NOV25", "18th DECEMBER", "26 DEC 25", etc.
    """
    if not expiry_str:
        return None

    expiry_str = str(expiry_str).strip().upper()

    try:
        dt = datetime.strptime(expiry_str, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        pass

    # Try parsing English date format
    try:
        # Remove ordinal suffixes (st, nd, rd, th)
        cleaned = re.sub(r'(ST|ND|RD|TH)\b', '', expiry_str)

        # Handle 2-digit year (e.g., "25" -> "2025", "26" -> "2026")
        if re.search(r'\d{2}$', cleaned) and not re.search(r'\d{4}', cleaned):
            # Extract 2-digit year and convert to 4-digit
            match = re.search(r'(\d{2})$', cleaned)
            if match:
                yy = int(match.group(1))
                yyyy = 2000 + yy if yy <= 50 else 1900 + yy  # Assume 20xx for 00-50, 19xx for 51-99
                cleaned = re.sub(r'\d{2}$', str(yyyy), cleaned)

        # If no year, add current or next year
        if not re.search(r'\d{4}', cleaned):
            cleaned = f"{cleaned} {datetime.now().year}"

        # Try parsing with full month name
        try:
            dt = datetime.strptime(cleaned, "%d %B %Y")
        except:
            # Try short month name
            try:
                dt = datetime.strptime(cleaned, "%d %b %Y")
            except:
                # Try without spaces
                try:
                    dt = datetime.strptime(cleaned.replace(" ", ""), "%d%b%Y")
                except:
                    # Try with various formats
                    dt = datetime.strptime(cleaned.replace(" ", ""), "%d%b%y")

        return dt.strftime("%Y-%m-%d")

    except Exception as e:
        if PRINT_DEBUG:
            logger.debug(f"Could not parse expiry '{expiry_str}': {e}")
        return None


instrument_cache = InstrumentCache()


# ============================================================================
# SIGNAL PARSING
# ============================================================================

_OPTION_RE = re.compile(r"^(?P<strike>\d+)(?P<option_type>CE|PE)$", re.IGNORECASE)


def parse_strict_template(message_text: str) -> Optional[Dict[str, Any]]:
    """
    Parse the strict production template:
    ENTRY|SENSEX|72700PE|230|240|200|270|2026-04-02|1
    """
    if not message_text:
        return None

    parts = [part.strip() for part in str(message_text).split(SIGNAL_TEMPLATE_DELIMITER)]
    if len(parts) < 8:
        return None

    verb = parts[0].upper()
    if verb not in ("ENTRY", "SIGNAL", "BUY", "SELL"):
        return None

    option_match = _OPTION_RE.match(parts[2].upper())
    if not option_match:
        return None

    action = "BUY" if verb in ("ENTRY", "SIGNAL") else verb
    quantity = int(parts[8]) if len(parts) > 8 and parts[8] else DEFAULT_QUANTITY
    if quantity <= 0:
        raise ValueError("quantity must be greater than 0")

    return {
        "action": action,
        "underlying": parts[1].upper(),
        "strike": int(option_match.group("strike")),
        "option_type": option_match.group("option_type").upper(),
        "entry_low": float(parts[3]),
        "entry_high": float(parts[4]),
        "stoploss": float(parts[5]),
        "targets": [float(parts[6])],
        "expiry": parts[7],
        "product": "I",
        "quantity": quantity,
        "signal_summary": f"{action} {parts[1].upper()} {option_match.group('strike')}{option_match.group('option_type').upper()}",
    }


def resolve_env_lot_size(signal: Dict[str, Any], instrument: Dict[str, Any]) -> int:
    """Resolve lot size from env first, then fall back to Upstox instrument metadata."""
    underlying = str(signal.get("underlying", "")).upper()
    option_type = str(signal.get("option_type", "")).upper()

    for env_key in (f"LOT_SIZE_{underlying}_{option_type}", f"LOT_SIZE_{underlying}"):
        env_val = os.getenv(env_key)
        if not env_val:
            continue
        try:
            parsed = int(env_val)
            if parsed > 0:
                logger.info(f"📦 Using env lot size {env_key}={parsed}")
                return parsed
        except ValueError:
            logger.warning(f"⚠️ Invalid lot size in {env_key}: {env_val}")

    try:
        instrument_lot = int(instrument.get("lot_size", 1))
        if instrument_lot > 0:
            return instrument_lot
    except Exception:
        pass

    return 1

def parse_message_to_signal(raw_message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse Redis message with structured fields"""
    if not isinstance(raw_message, dict):
        return None

    try:
        template_text = raw_message.get("message") or raw_message.get("signal_template") or raw_message.get("signal")
        if template_text:
            parsed_template = parse_strict_template(template_text)
            if parsed_template:
                return parsed_template
            if STRICT_TEMPLATE_ENABLED:
                logger.warning("⚠️ Strict template enabled and message did not match expected template")
                return None

        signal = {}
        action = raw_message.get("action", "").upper()
        if not action:
            # Fallback: extract BUY/SELL from the 'message' text field
            msg_text = raw_message.get("message", "").upper()
            if msg_text.startswith("BUY"):
                action = "BUY"
            elif msg_text.startswith("SELL"):
                action = "SELL"
        signal["action"] = action
        signal["underlying"] = raw_message.get("instrument", raw_message.get("underlying", "")).upper()

        # Parse strike as int
        strike = raw_message.get("strike")
        signal["strike"] = int(strike) if strike else None

        signal["option_type"] = raw_message.get("option_type", "").upper()

        # Parse entry_low as float
        entry_low = raw_message.get("entry_low")
        signal["entry_low"] = float(entry_low) if entry_low else None

        # Parse entry_high
        entry_high = raw_message.get("entry_high")
        signal["entry_high"] = float(entry_high) if entry_high else None

        # Parse stoploss as float
        stoploss = raw_message.get("stoploss")
        signal["stoploss"] = float(stoploss) if stoploss else None

        # Parse targets: "480/530/650" → [480.0, 530.0, 650.0]
        targets = raw_message.get("targets")
        if targets:
            if isinstance(targets, str):
                target_list = [float(t.strip()) for t in targets.split('/') if t.strip()]
                signal["targets"] = target_list
            elif isinstance(targets, list):
                signal["targets"] = [float(t) for t in targets]
        else:
            signal["targets"] = []

        signal["expiry"] = raw_message.get("expiry", "")
        signal["product"] = raw_message.get("product", "I").upper()

        # Parse quantity (fall back to DEFAULT_QUANTITY from .env)
        qty = raw_message.get("quantity")
        signal["quantity"] = int(qty) if qty else DEFAULT_QUANTITY
        if signal["quantity"] <= 0:
            raise ValueError("quantity must be greater than 0")

        signal["signal_summary"] = f"{signal.get('action')} {signal.get('underlying')} {signal.get('strike')}{signal.get('option_type')}"

        return signal

    except Exception as e:
        logger.warning(f"⚠️ Error parsing signal: {e}")
        return None


# ============================================================================
# GTT ORDER PLACEMENT
# ============================================================================

def place_gtt_order_upstox(signal: Dict[str, Any]) -> Dict[str, Any]:
    """
    Place GTT order directly to Upstox API v3.

    Args:
        signal: Parsed signal with action, underlying, strike, option_type, entry_low, stoploss, targets, expiry

    Returns:
        Response from Upstox API
    """
    try:
        # Get required fields
        action = signal.get("action", "").upper()
        underlying = signal.get("underlying", "").upper()
        strike = signal.get("strike")
        opt_type = signal.get("option_type", "").upper()
        entry_low = signal.get("entry_low")
        stoploss = signal.get("stoploss")
        targets = signal.get("targets", [])
        expiry_str = signal.get("expiry", "")
        quantity = signal.get("quantity", 1)
        product = signal.get("product", "I").upper()

        # Validate required fields
        if not all([action, underlying, strike, opt_type, entry_low, stoploss, targets, expiry_str]):
            missing = []
            if not action: missing.append("action")
            if not underlying: missing.append("underlying")
            if not strike: missing.append("strike")
            if not opt_type: missing.append("option_type")
            if not entry_low: missing.append("entry_low")
            if not stoploss: missing.append("stoploss")
            if not targets: missing.append("targets")
            if not expiry_str: missing.append("expiry")
            logger.error(f"❌ Missing fields: {missing}")
            return {"status": "error", "message": f"Missing fields: {missing}"}

        # Get minimum target (first target price)
        target = min(float(t) for t in targets)
        entry_low = float(entry_low)
        stoploss = float(stoploss)

        logger.info(f"🔍 Placing GTT Order to Upstox:")
        logger.info(f"   Action: {action} | Underlying: {underlying} | Strike: {strike} | Type: {opt_type}")
        logger.info(f"   Entry: {entry_low} | SL: {stoploss} | Target: {target}")
        logger.info(f"   Expiry: {expiry_str} | Qty: {quantity} | Product: {product}")

        # Find instrument in cache
        instrument = instrument_cache.find_instrument(underlying, strike, opt_type, expiry_str)
        if not instrument:
            logger.error(f"❌ Could not find instrument in Upstox")
            return {"status": "error", "message": "Instrument not found in Upstox"}

        instrument_token = instrument.get("instrument_key")
        lot_size = resolve_env_lot_size(signal, instrument)
        logger.info(f"   📦 Instrument Token: {instrument_token}")
        logger.info(f"   📦 Lot Size: {lot_size}")

        # Calculate actual quantity to send to API
        # User specifies quantity in lots, Upstox API expects total shares
        # Example: SENSEX lot_size=20, user qty=1 -> API qty=20
        api_quantity = int(quantity) * int(lot_size)
        if api_quantity <= 0:
            logger.error("❌ Invalid computed quantity: must be greater than 0")
            return {"status": "error", "message": "Invalid computed quantity"}
        logger.info(f"   📦 Quantity: {quantity} lot(s) × {lot_size} = {api_quantity} shares")

        # Determine trigger type based on action
        # For BUY: entry triggers when price goes ABOVE entry_low
        # For SELL: entry triggers when price goes BELOW entry_low
        if action == "BUY":
            entry_trigger = "ABOVE"
        else:
            entry_trigger = "BELOW"

        # Build GTT order payload
        url = f"{UPSTOX_BASE_URL}/v3/order/gtt/place"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}",
        }

        # Multi-leg GTT with ENTRY, TARGET, and STOPLOSS
        payload = {
            "type": "MULTIPLE",
            "quantity": api_quantity,
            "product": product,
            "rules": [
                {
                    "strategy": "ENTRY",
                    "trigger_type": entry_trigger,
                    "trigger_price": entry_low
                },
                {
                    "strategy": "TARGET",
                    "trigger_type": "IMMEDIATE",
                    "trigger_price": target
                },
                {
                    "strategy": "STOPLOSS",
                    "trigger_type": "IMMEDIATE",
                    "trigger_price": stoploss
                }
            ],
            "instrument_token": instrument_token,
            "transaction_type": action
        }

        if PRINT_DEBUG:
            logger.debug(f"📤 Upstox Payload:\n{json.dumps(payload, indent=2)}")

        # Send to Upstox with retry logic
        logger.info(f"📡 Sending GTT order to Upstox API...")
        max_retries = 3
        resp = None
        for attempt in range(max_retries):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
                break  # Success
            except requests.exceptions.Timeout:
                if attempt < max_retries - 1:
                    logger.warning(f"⚠️  Timeout on attempt {attempt + 1}, retrying...")
                    time.sleep(2)
                else:
                    raise

        if PRINT_DEBUG and resp:
            logger.info(f"   Response Code: {resp.status_code}")
            try:
                logger.debug(f"   Response: {json.dumps(resp.json(), indent=2)}")
            except:
                logger.debug(f"   Response: {resp.text}")

        # Parse response
        if resp.status_code == 200:
            result = resp.json()
            if result.get("status") == "success":
                gtt_ids = result.get("data", {}).get("gtt_order_ids", [])
                logger.info(f"✅ GTT Order placed successfully! IDs: {gtt_ids}")
                result["instrument_token"] = instrument_token
                result["request_payload"] = payload
                ltp = fetch_ltp(instrument_token)
                result["ltp_at_placement"] = ltp
                if ltp:
                    logger.info(f"   💹 LTP at placement: {ltp}")
                return result
            else:
                logger.error(f"❌ GTT Order failed: {result.get('message', 'Unknown error')}")
                return result
        else:
            error_msg = resp.text
            try:
                error_json = resp.json()
                error_msg = error_json.get("message", error_msg)
            except:
                pass

            logger.error(f"❌ HTTP {resp.status_code}: {error_msg}")
            return {
                "status": "error",
                "message": error_msg,
                "code": resp.status_code
            }

    except Exception as e:
        logger.exception(f"❌ Exception placing GTT order:")
        return {
            "status": "error",
            "message": str(e)
        }


def fetch_ltp(instrument_token: str) -> Optional[float]:
    """Fetch current LTP for an instrument from Upstox Market Quote API."""
    try:
        url = f"{UPSTOX_BASE_URL}/v2/market-quote/ltp"
        headers = {"Accept": "application/json", "Authorization": f"Bearer {UPSTOX_ACCESS_TOKEN}"}
        resp = requests.get(url, headers=headers, params={"instrument_key": instrument_token}, timeout=5)
        if resp.status_code == 200:
            data = resp.json().get("data", {})
            for val in data.values():
                ltp = float(val.get("last_price", 0))
                return ltp if ltp > 0 else None
    except Exception as e:
        logger.debug(f"Could not fetch LTP for {instrument_token}: {e}")
    return None


def store_signal_metadata(
    message_id: str,
    signal: Dict,
    status: str,
    gtt_ids: Optional[List[str]] = None,
    instrument_key: str = "",
    ltp_at_placement: Optional[float] = None,
    gtt_request_payload: Optional[Dict[str, Any]] = None,
):
    """Store metadata about processed GTT signal in Redis and SQLite DB"""
    try:
        key = f"gtt_metadata:{message_id}"
        metadata = {
            "message_id": message_id,
            "processed_at": datetime.now().isoformat(),
            "signal_summary": signal.get("signal_summary") or f"{signal.get('action')} {signal.get('underlying')} {signal.get('strike')}{signal.get('option_type')}",
            "status": status,
            "gtt_ids": ",".join(gtt_ids) if gtt_ids else "",
            "entry_low": str(signal.get('entry_low', '')),
            "stoploss": str(signal.get('stoploss', '')),
            "targets": str(signal.get('targets', [])),
        }
        redis_client.hset(key, mapping=metadata)
        redis_client.expire(key, 7 * 24 * 3600)  # 7 days

        if PRINT_DEBUG:
            logger.debug(f"📝 Stored metadata for {message_id}")
    except Exception as e:
        logger.warning(f"⚠️ Failed to store Redis metadata: {e}")

    # Write to SQLite DB (only for real signals, not parse errors)
    if signal and signal.get("entry_low"):
        try:
            db_status = "PENDING" if status == "success" else "FAILED"
            signal_id = db.insert_signal(
                redis_message_id=message_id,
                signal=signal,
                instrument_key=instrument_key,
                gtt_ids=gtt_ids or [],
                status=db_status,
                notes=None if status == "success" else status,
                ltp_at_placement=ltp_at_placement,
            )
            if signal_id and gtt_ids:
                for gtt_id in gtt_ids:
                    db.set_signal_gtt_order(signal_id, gtt_id)
                    if gtt_request_payload:
                        for rule in gtt_request_payload.get("rules", []):
                            seeded_rule = dict(rule)
                            seeded_rule.setdefault("status", "PENDING")
                            seeded_rule.setdefault("message", "")
                            seeded_rule.setdefault("order_id", None)
                            seeded_rule.setdefault("transaction_type", gtt_request_payload.get("transaction_type"))
                            db.upsert_gtt_rule(signal_id, gtt_id, seeded_rule)
                db.update_tracker_fields(signal_id, tracker_status="GTT_PLACED")
            if PRINT_DEBUG:
                logger.debug(f"📝 Stored DB record for {message_id} ({db_status})")
        except Exception as e:
            logger.warning(f"⚠️ Failed to store DB record: {e}")


# ============================================================================
# MAIN LOOP
# ============================================================================

def start_stream_consumer():
    logger.info("=" * 80)
    logger.info(f"🚀 GTT Strategy (Upstox API v3) started at {datetime.now().isoformat()}")
    logger.info("=" * 80)

    # Initialise SQLite DB (creates tables if not exists)
    db.init_db()
    logger.info("✅ SQLite DB initialised")

    # Load instruments once at startup
    if not instrument_cache.load_instruments():
        logger.warning("⚠️ Failed to load instruments. Some signals may fail to process.")

    # Determine starting ID
    start_policy = (REDIS_START_FROM or "").strip().lower()
    if start_policy in ("$", "tail", "new"):
        last_id = "$"
        logger.info("📍 Starting position: Only NEW messages")
    elif start_policy in ("0", "head"):
        last_id = "0-0"
        logger.info("📍 Starting position: From BEGINNING of stream")
    else:  # resume
        last_id = redis_client.get(LAST_ID_KEY) or "$"
        logger.info(f"📍 Starting position: RESUME from {last_id}")

    processed_ids = set()

    while True:
        try:
            entries = redis_client.xread({REDIS_STREAM: last_id}, count=10, block=5000)

            if not entries:
                continue

            for stream_name, messages in entries:
                for message_id, message_kv in messages:
                    logger.info("=" * 80)
                    logger.info(f"📨 Processing Signal ID: {message_id}")

                    # Dedupe check
                    if message_id in processed_ids:
                        logger.info(f"⏭️  Already processed - Skipping")
                        last_id = message_id
                        continue

                    try:
                        if redis_client.zscore(PROCESSED_ZSET, message_id) is not None:
                            logger.info(f"⏭️  Found in processed set - Skipping")
                            last_id = message_id
                            continue
                    except Exception:
                        pass

                    processed_ids.add(message_id)

                    # Parse signal
                    signal = parse_message_to_signal(message_kv)
                    if not signal:
                        logger.error(f"❌ Could not parse signal")
                        store_signal_metadata(message_id, {}, "parse_error")
                        last_id = message_id
                        continue

                    logger.info(f"📊 Signal: {signal.get('action')} {signal.get('underlying')} {signal.get('strike')}{signal.get('option_type')}")
                    logger.info(f"   Entry: {signal.get('entry_low')} | SL: {signal.get('stoploss')} | Targets: {signal.get('targets')}")

                    # Place GTT order
                    logger.info("🔄 Placing GTT order via Upstox API v3...")
                    result = place_gtt_order_upstox(signal)

                    # Store result
                    gtt_ids = None
                    if result.get("status") == "success":
                        gtt_ids = result.get("data", {}).get("gtt_order_ids")
                        store_signal_metadata(
                            message_id,
                            signal,
                            "success",
                            gtt_ids,
                            instrument_key=result.get("instrument_token", ""),
                            ltp_at_placement=result.get("ltp_at_placement"),
                            gtt_request_payload=result.get("request_payload"),
                        )
                        logger.info(f"✅ GTT Order placed - IDs: {gtt_ids}")
                    else:
                        store_signal_metadata(message_id, signal, "failed")
                        logger.error(f"❌ GTT Order failed: {result.get('message')}")

                    # Update tracking
                    last_id = message_id
                    try:
                        now_ts = int(time.time())
                        redis_client.zadd(PROCESSED_ZSET, {message_id: now_ts})
                        redis_client.set(LAST_ID_KEY, message_id)

                        # Trim ZSET
                        zcount = redis_client.zcard(PROCESSED_ZSET)
                        if isinstance(zcount, int) and zcount > PROCESSED_ZSET_MAX:
                            redis_client.zremrangebyrank(PROCESSED_ZSET, 0, zcount - PROCESSED_ZSET_MAX - 1)
                    except Exception:
                        pass

                    logger.info("=" * 80)

        except KeyboardInterrupt:
            logger.info("🛑 Stopping consumer.")
            break
        except Exception as e:
            logger.exception("❌ Main loop error:")
            time.sleep(2)


if __name__ == "__main__":
    start_stream_consumer()
