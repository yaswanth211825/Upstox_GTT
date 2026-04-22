"""Trading Floor buffer rules.

Buffer is applied ONLY to Stop Loss and Targets — NOT to entry price.
Buffer amount is determined by the option premium price (signal entry_low) using a
configurable price-range table stored in the BUFFER_TABLE env var.

─── CONFIGURING BUFFER_TABLE ───────────────────────────────────────────────────
Set in .env as comma-separated  low:high:pts  tiers (price ranges are exclusive on high):

  BUFFER_TABLE=100:200:3,200:300:5,300:400:7,400:inf:10

Default (if not set):
  100–200  → 3 pts
  200–300  → 5 pts
  300–400  → 7 pts
  400+     → 10 pts

Entry price used for lookup = signal.entry_low (option premium from signal).
────────────────────────────────────────────────────────────────────────────────

Entry price rules (NO buffer ever applied to entry):
  BUY_ABOVE → exact entry_low
  RANGING   → midpoint = round((entry_low + entry_high) / 2, 2)
  AVERAGE   → entry_low

SL  → stoploss_adj = stoploss ∓ buffer_pts   (BUY: minus,  SELL: plus)
T1+ → target_adj  = target   ∓ buffer_pts   (BUY: minus,  SELL: plus)
"""
import os
from ..signal.types import RawSignal, AdjustedSignal

# ── Buffer table loading ──────────────────────────────────────────────────────

_DEFAULT_TABLE: list[tuple[float, float, float]] = [
    (100.0, 200.0,  3.0),
    (200.0, 300.0,  5.0),
    (300.0, 400.0,  7.0),
    (400.0, float("inf"), 10.0),
]


def _load_buffer_table() -> list[tuple[float, float, float]]:
    """Parse BUFFER_TABLE env var into (low, high, pts) tuples.

    Format: "low:high:pts,low:high:pts,..."
    Use "inf" for the upper bound of the last tier.
    Example: BUFFER_TABLE=100:200:3,200:300:5,300:400:7,400:inf:10
    """
    raw = os.getenv("BUFFER_TABLE", "").strip()
    if not raw:
        return _DEFAULT_TABLE
    try:
        table = []
        for tier in raw.split(","):
            parts = tier.strip().split(":")
            low  = float(parts[0])
            high = float("inf") if parts[1].lower() == "inf" else float(parts[1])
            pts  = float(parts[2])
            table.append((low, high, pts))
        return table
    except Exception:
        # Malformed env var — fall back to default and warn
        import sys
        print(
            f"[BUFFER] WARNING: BUFFER_TABLE env var is malformed ({raw!r}). "
            "Using default table. Format: low:high:pts,low:high:pts,...",
            file=sys.stderr,
        )
        return _DEFAULT_TABLE


_BUFFER_TABLE_CACHE: list[tuple[float, float, float]] | None = None


def _get_buffer_table() -> list[tuple[float, float, float]]:
    global _BUFFER_TABLE_CACHE
    if _BUFFER_TABLE_CACHE is None:
        _BUFFER_TABLE_CACHE = _load_buffer_table()
    return _BUFFER_TABLE_CACHE


def get_buffer(entry_price: float) -> float:
    """Return buffer points for the given option premium price (entry_low of signal).

    Walks the configured price-range table and returns the matching pts value.
    Falls back to the last tier's pts if price exceeds all ranges.
    """
    table = _get_buffer_table()
    for low, high, pts in table:
        if low <= entry_price < high:
            return pts
    # price above all defined tiers — use last tier's pts
    return table[-1][2] if table else 10.0


# ── Price adjustment helpers ─────────────────────────────────────────────────

def adjust_stoploss(sl: float, action: str, entry_price: float) -> float:
    """Apply buffer to stoploss based on entry_price tier.

    BUY  → stoploss_adj = stoploss - buffer_pts  (limit-sell below stated SL)
    SELL → stoploss_adj = stoploss + buffer_pts  (limit-buy above stated SL)
    """
    buf = get_buffer(entry_price)
    return round(sl - buf if action == "BUY" else sl + buf, 2)


def adjust_target(target: float, action: str, entry_price: float) -> float:
    """Apply buffer to target based on entry_price tier.

    BUY  → target_adj = target - buffer_pts  (exit before price fully reaches target)
    SELL → target_adj = target + buffer_pts
    """
    buf = get_buffer(entry_price)
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

    # SL + targets: buffer based on entry_low price tier
    stoploss_adj = adjust_stoploss(signal.stoploss, action, signal.entry_low)
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
