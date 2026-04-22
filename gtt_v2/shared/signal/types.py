from __future__ import annotations
from datetime import date
from typing import Optional
from pydantic import BaseModel, field_validator, model_validator


class RawSignal(BaseModel):
    """Signal as parsed from the Redis stream message — no buffer applied yet."""
    action: str                     # BUY | SELL
    underlying: str                 # NIFTY, BANKNIFTY, SENSEX
    strike: Optional[int] = None
    option_type: Optional[str] = None   # CE | PE
    expiry: Optional[date] = None
    product: str = "D"

    entry_low: float
    entry_high: Optional[float] = None  # set for RANGING trades
    average: Optional[float] = None    # set for AVERAGE trades (avg level below entry_low)
    stoploss: float
    targets: list[float] = []

    quantity_lots: int = 1
    safe_only: bool = False
    risk_only: bool = False
    trade_type: Optional[str] = None    # BUY_ABOVE | RANGING | AVERAGE

    redis_message_id: str = ""
    signal_hash: Optional[str] = None
    instrument_key: Optional[str] = None

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        v = v.upper()
        if v not in ("BUY", "SELL"):
            raise ValueError(f"action must be BUY or SELL, got {v!r}")
        return v

    @field_validator("option_type")
    @classmethod
    def validate_option_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.upper()
        if v not in ("CE", "PE"):
            raise ValueError(f"option_type must be CE or PE, got {v!r}")
        return v

    @model_validator(mode="after")
    def validate_prices(self) -> "RawSignal":
        if self.action == "BUY" and self.entry_low <= self.stoploss:
            raise ValueError(
                f"BUY signal: entry_low ({self.entry_low}) must be > stoploss ({self.stoploss})"
            )
        if self.action == "SELL" and self.entry_low >= self.stoploss:
            raise ValueError(
                f"SELL signal: entry_low ({self.entry_low}) must be < stoploss ({self.stoploss})"
            )
        if self.quantity_lots <= 0:
            raise ValueError("quantity_lots must be > 0")
        return self


class AdjustedSignal(BaseModel):
    """Signal after Trading Floor buffer rules applied — ready for GTT placement.

    Entry fields (NO buffer applied):
      entry_low_adj  — raw entry_low carried through unchanged
      entry_high_adj — raw entry_high carried through unchanged
      average_adj    — raw average carried through unchanged
      entry_price    — FINAL single entry used for GTT trigger:
                         RANGING   → midpoint = round((entry_low + entry_high) / 2, 2)
                         BUY_ABOVE → entry_low  (exact)
                         AVERAGE   → entry_low  (first lot)

    Buffer IS applied to SL and targets only.
    """
    raw: RawSignal

    entry_low_adj: float           # raw entry_low, no buffer
    entry_high_adj: Optional[float] = None   # raw entry_high, no buffer
    average_adj: Optional[float] = None      # raw average, no buffer
    entry_price: float             # final GTT entry trigger (midpoint for RANGING, else entry_low)
    stoploss_adj: float            # SL with buffer applied
    targets_adj: list[float] = []  # targets with buffer applied

    trade_type: str    # BUY_ABOVE | RANGING | AVERAGE
    instrument_key: str = ""
    ltp_at_check: Optional[float] = None
