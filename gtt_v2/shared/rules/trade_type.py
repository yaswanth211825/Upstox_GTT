"""Detect trade type from signal characteristics."""
from ..signal.types import RawSignal


def detect_trade_type(signal: RawSignal) -> str:
    """
    AVERAGE    — average level provided (below entry_low, above SL)
    RANGING    — entry_low and entry_high both provided
    BUY_ABOVE  — single entry point
    """
    if signal.trade_type == "AVERAGE":
        return "AVERAGE"
    if signal.average:
        return "AVERAGE"
    if signal.entry_high and signal.entry_high != signal.entry_low:
        return "RANGING"
    return "BUY_ABOVE"
