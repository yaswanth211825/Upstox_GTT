"""
Mock Upstox client that records GTT placements and simulates triggers.
No real HTTP calls made — everything stays in memory + Redis.
"""
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PlacedGTT:
    gtt_id: str
    instrument_token: str
    action: str
    product: str
    quantity: int
    entry_price: float
    entry_trigger: str    # ABOVE | BELOW
    target_price: float
    stoploss_price: float
    status: str = "pending"   # pending | entry_filled | target_hit | sl_hit | cancelled
    fill_price: Optional[float] = None
    exit_price: Optional[float] = None


class MockUpstoxClient:
    """Fake Upstox client — records all GTT calls, allows simulated triggers."""

    def __init__(self):
        self.gtts: dict[str, PlacedGTT] = {}
        self.cancelled: list[str] = []
        self._fail_next_gtt: bool = False

    def set_fail_next(self, fail: bool = True):
        """Make the next place_gtt call fail (tests circuit breaker / error handling)."""
        self._fail_next_gtt = fail

    async def place_gtt(self, payload: dict) -> dict:
        if self._fail_next_gtt:
            self._fail_next_gtt = False
            raise RuntimeError("Simulated Upstox API failure")

        rules = {r["strategy"]: r for r in payload["rules"]}
        gtt_id = f"sim-{uuid.uuid4().hex[:8]}"
        entry_rule = rules["ENTRY"]
        gtt = PlacedGTT(
            gtt_id=gtt_id,
            instrument_token=payload["instrument_token"],
            action=payload["transaction_type"],
            product=payload["product"],
            quantity=payload["quantity"],
            entry_price=entry_rule["trigger_price"],
            entry_trigger=entry_rule["trigger_type"],
            target_price=rules["TARGET"]["trigger_price"],
            stoploss_price=rules["STOPLOSS"]["trigger_price"],
        )
        self.gtts[gtt_id] = gtt
        return {"status": "success", "data": {"gtt_order_ids": [gtt_id]}}

    async def cancel_gtt(self, gtt_order_id: str) -> dict:
        if gtt_order_id in self.gtts:
            self.gtts[gtt_order_id].status = "cancelled"
        self.cancelled.append(gtt_order_id)
        return {"status": "success"}

    async def get_ltp(self, instrument_key: str) -> Optional[float]:
        return None

    async def aclose(self):
        pass

    # ── Simulation helpers ────────────────────────────────────────────────────

    def simulate_price_hit_entry(self, gtt_id: str, fill_price: float) -> Optional[PlacedGTT]:
        """Simulate: price crossed entry → order fills at fill_price."""
        gtt = self.gtts.get(gtt_id)
        if not gtt or gtt.status != "pending":
            return None
        gtt.status = "entry_filled"
        gtt.fill_price = fill_price
        return gtt

    def simulate_price_hit_target(self, gtt_id: str) -> Optional[PlacedGTT]:
        gtt = self.gtts.get(gtt_id)
        if not gtt or gtt.status != "entry_filled":
            return None
        gtt.status = "target_hit"
        gtt.exit_price = gtt.target_price
        return gtt

    def simulate_price_hit_stoploss(self, gtt_id: str) -> Optional[PlacedGTT]:
        gtt = self.gtts.get(gtt_id)
        if not gtt or gtt.status != "entry_filled":
            return None
        gtt.status = "sl_hit"
        gtt.exit_price = gtt.stoploss_price
        return gtt

    def get_all_pending(self) -> list[PlacedGTT]:
        return [g for g in self.gtts.values() if g.status == "pending"]

    def get_filled(self) -> list[PlacedGTT]:
        return [g for g in self.gtts.values() if g.status == "entry_filled"]
