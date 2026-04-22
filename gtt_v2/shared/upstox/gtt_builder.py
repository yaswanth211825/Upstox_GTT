"""Build Upstox v3 GTT MULTIPLE payload."""


def build_gtt_payload(
    instrument_token: str,
    action: str,        # BUY | SELL
    product: str,       # I | D
    entry_price: float, # midpoint for RANGING, exact entry_low for BUY_ABOVE/AVERAGE
    quantity: int,      # total units = DEFAULT_QUANTITY (lots) * lot_size
    stoploss_adj: float,
    target_adj: float,
) -> dict:
    """3-leg MULTIPLE GTT: ENTRY → TARGET + STOPLOSS activate after entry fills."""
    entry_trigger = "ABOVE" if action == "BUY" else "BELOW"
    return {
        "type": "MULTIPLE",
        "quantity": quantity,
        "product": product,
        "instrument_token": instrument_token,
        "transaction_type": action,
        "rules": [
            {"strategy": "ENTRY",    "trigger_type": entry_trigger, "trigger_price": entry_price},
            {"strategy": "TARGET",   "trigger_type": "IMMEDIATE",   "trigger_price": target_adj},
            {"strategy": "STOPLOSS", "trigger_type": "IMMEDIATE",   "trigger_price": stoploss_adj},
        ],
    }


def resolve_lot_size(underlying: str, instrument_data: dict) -> int:
    """Prefer env var LOT_SIZE_{UNDERLYING}, fall back to instrument metadata."""
    import os
    for key in (f"LOT_SIZE_{underlying.upper()}", f"LOT_SIZE_{underlying.upper()}_CE"):
        val = os.getenv(key)
        if val:
            try:
                n = int(val)
                if n > 0:
                    return n
            except ValueError:
                pass
    try:
        n = int(instrument_data.get("lot_size", 1))
        return n if n > 0 else 1
    except (TypeError, ValueError):
        return 1
