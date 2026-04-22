"""Signal parser — ported from gtt_strategy.parse_message_to_signal."""
import re
from typing import Any, Optional
from .types import RawSignal
from .expiry import parse_expiry_date
from ..config import settings

_OPTION_RE = re.compile(r"^(?P<strike>\d+)(?P<option_type>CE|PE)$", re.IGNORECASE)
_TEMPLATE_DELIMITER = "|"


def parse_strict_template(text: str) -> Optional[dict]:
    """
    Parse pipe-delimited strict template:
    ENTRY|SENSEX|72700PE|230|240|200|270|2026-04-02|1
    Fields: verb | underlying | strikeOPT | entry_low | entry_high | stoploss | target | expiry | quantity
    """
    if not text:
        return None
    parts = [p.strip() for p in str(text).split(_TEMPLATE_DELIMITER)]
    if len(parts) < 8:
        return None

    verb = parts[0].upper()
    if verb not in ("ENTRY", "SIGNAL", "BUY", "SELL"):
        return None

    m = _OPTION_RE.match(parts[2].upper())
    if not m:
        return None

    action = "BUY" if verb in ("ENTRY", "SIGNAL") else verb
    qty = int(parts[8]) if len(parts) > 8 and parts[8] else settings.default_quantity
    if qty <= 0:
        raise ValueError("quantity must be > 0")

    return {
        "action": action,
        "underlying": parts[1].upper(),
        "strike": int(m.group("strike")),
        "option_type": m.group("option_type").upper(),
        "entry_low": float(parts[3]),
        "entry_high": float(parts[4]) if parts[4] else None,
        "stoploss": float(parts[5]),
        "targets": [float(parts[6])],
        "expiry": parts[7],
        "product": "I",
        "quantity_lots": qty,
    }


def parse_message_to_signal(
    raw: dict[str, Any], redis_message_id: str = ""
) -> Optional[RawSignal]:
    """
    Parse a Redis stream message dict into a validated RawSignal.
    Returns None if the message cannot be parsed or fails validation.
    """
    if not isinstance(raw, dict):
        return None

    try:
        # Try strict template first (from "message" or "signal" field)
        template_text = raw.get("message") or raw.get("signal_template") or raw.get("signal")
        if template_text:
            parsed = parse_strict_template(str(template_text))
            if parsed:
                parsed["redis_message_id"] = redis_message_id
                parsed["expiry"] = parse_expiry_date(str(parsed.get("expiry", "")))
                return RawSignal(**parsed)

        # Structured fields
        action = str(raw.get("action", "")).upper()
        if not action:
            msg = str(raw.get("message", "")).upper()
            action = "BUY" if msg.startswith("BUY") else "SELL" if msg.startswith("SELL") else ""

        underlying = str(raw.get("instrument", raw.get("underlying", ""))).upper()
        strike_raw = raw.get("strike")
        strike = int(strike_raw) if strike_raw else None
        option_type = str(raw.get("option_type", "")).upper() or None

        entry_low_raw = raw.get("entry_low")
        entry_high_raw = raw.get("entry_high")
        average_raw = raw.get("average")
        stoploss_raw = raw.get("stoploss")
        entry_low = float(entry_low_raw) if entry_low_raw else None
        entry_high = float(entry_high_raw) if entry_high_raw else None
        average = float(average_raw) if average_raw else None
        stoploss = float(stoploss_raw) if stoploss_raw else None

        targets_raw = raw.get("targets")
        if targets_raw:
            if isinstance(targets_raw, str):
                targets = [float(t.strip()) for t in targets_raw.split("/") if t.strip()]
            elif isinstance(targets_raw, list):
                targets = [float(t) for t in targets_raw]
            else:
                targets = []
        else:
            targets = []

        expiry_str = raw.get("expiry", "")
        expiry = parse_expiry_date(str(expiry_str)) if expiry_str else None
        product = str(raw.get("product", "I")).upper()

        qty_raw = raw.get("quantity")
        qty = int(qty_raw) if qty_raw else settings.default_quantity
        if qty <= 0:
            raise ValueError("quantity must be > 0")

        safe_only = str(raw.get("safe_only", "false")).lower() in ("1", "true", "yes")
        risk_only = str(raw.get("risk_only", "false")).lower() in ("1", "true", "yes")

        if entry_low is None or stoploss is None:
            return None

        return RawSignal(
            action=action,
            underlying=underlying,
            strike=strike,
            option_type=option_type,
            expiry=expiry,
            product=product,
            entry_low=entry_low,
            entry_high=entry_high,
            average=average,
            stoploss=stoploss,
            targets=targets,
            quantity_lots=qty,
            safe_only=safe_only,
            risk_only=risk_only,
            redis_message_id=redis_message_id,
        )

    except Exception:
        return None
