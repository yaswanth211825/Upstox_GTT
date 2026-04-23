"""Trading Floor buffer rules.

Entry price rules (NO buffer ever applied to entry):
  BUY_ABOVE → exact entry_low
  RANGING   → midpoint = round((entry_low + entry_high) / 2, 2)
  AVERAGE   → entry_low

Stoploss buffer is a fixed 3 points for every leg.
Target buffer is decided by the signal price range:
  100–200  -> 3 pts
  200–300  -> 4 pts
  300–400  -> 5 pts
  400+     -> 5 pts

That target buffer is subtracted from every BUY target (or added for SELL).
Edit the constants in this file whenever you want to change this behavior.
"""
from ..signal.types import RawSignal, AdjustedSignal

STOPLOSS_BUFFER_PTS = 3.0
TARGET_BUFFER_TABLE: list[tuple[float, float, float]] = [
    (100.0, 200.0, 3.0),
    (200.0, 300.0, 4.0),
    (300.0, 400.0, 5.0),
    (400.0, float("inf"), 5.0),
]


def get_stoploss_buffer() -> float:
    return STOPLOSS_BUFFER_PTS


def get_target_buffer(entry_price: float) -> float:
    for low, high, pts in TARGET_BUFFER_TABLE:
        if low <= entry_price < high:
            return pts
    return TARGET_BUFFER_TABLE[-1][2]


# ── Price adjustment helpers ─────────────────────────────────────────────────

def adjust_stoploss(sl: float, action: str) -> float:
    """Apply the fixed stoploss buffer.

    BUY  → stoploss_adj = stoploss - buffer_pts  (limit-sell below stated SL)
    SELL → stoploss_adj = stoploss + buffer_pts  (limit-buy above stated SL)
    """
    buf = get_stoploss_buffer()
    return round(sl - buf if action == "BUY" else sl + buf, 2)


def adjust_target(target: float, action: str, entry_price: float) -> float:
    """Apply the target buffer based on the signal price range.

    BUY  → target_adj = target - buffer_pts  (exit before price fully reaches target)
    SELL → target_adj = target + buffer_pts
    """
    buf = get_target_buffer(entry_price)
    return round(target - buf if action == "BUY" else target + buf, 2)


def _compute_entry_price(entry_low: float, entry_high: float | None, trade_type: str) -> float:
    """Return the single GTT entry trigger price — no buffer applied.

    RANGING → midpoint = round((entry_low + entry_high) / 2, 2)
    Others  → entry_low (exact)
    """
    if trade_type == "RANGING" and entry_high is not None and entry_high != entry_low:
        return round((entry_low + entry_high) / 2, 2)
    return entry_low


# ── Main function ─────────────────────────────────────────────────────────────

def apply_buffer(signal: RawSignal) -> AdjustedSignal:
    action = signal.action

    # Detect trade type
    if signal.trade_type == "AVERAGE" or signal.average:
        trade_type = "AVERAGE"
    elif signal.entry_high and signal.entry_high != signal.entry_low:
        trade_type = "RANGING"
    else:
        trade_type = "BUY_ABOVE"

    # Entry: NO buffer
    entry_low_adj  = signal.entry_low
    entry_high_adj = signal.entry_high
    average_adj    = signal.average
    entry_price    = _compute_entry_price(signal.entry_low, signal.entry_high, trade_type)

    # SL + targets: fixed buffers defined in this file only
    stoploss_adj = adjust_stoploss(signal.stoploss, action)
    targets_adj  = [adjust_target(t, action, signal.entry_low) for t in signal.targets]

    return AdjustedSignal(
        raw=signal,
        entry_low_adj=entry_low_adj,
        entry_high_adj=entry_high_adj,
        average_adj=average_adj,
        entry_price=entry_price,
        stoploss_adj=stoploss_adj,
        targets_adj=targets_adj,
        trade_type=trade_type,
        instrument_key=signal.instrument_key or "",
    )
