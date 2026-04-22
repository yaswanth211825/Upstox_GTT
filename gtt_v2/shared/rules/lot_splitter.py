"""Lot splitting rules.

Profile-based splitting is DISABLED FOR NOW — will re-enable later.
All trades (SAFE and RISK) get a single GTT: 100% of lots at entry_low_adj.

Original PDF rules (preserved as comments for re-enablement):
  RANGING  + SAFE → 1 GTT: 100% at entry_low+buf
  RANGING  + RISK → 2 GTTs: 50% at entry_low+buf, 50% at entry_high+buf
  BUY_ABOVE+ SAFE → 2 GTTs: 50% at entry+buf, 50% near SL  (stoploss_adj + NEAR_SL_BUFFER_PTS)
  BUY_ABOVE+ RISK → 1 GTT: 100% at entry+buf
  AVERAGE  + SAFE → 2 GTTs: 30% at entry_low+buf, 70% at average_adj
  AVERAGE  + RISK → 2 GTTs: 50% at entry_low+buf, 50% at average_adj
"""
import os
from dataclasses import dataclass
from ..signal.types import AdjustedSignal


@dataclass
class LotOrder:
    lots: int
    entry_price: float


def _near_sl_buffer() -> float:
    """Points above stoploss_adj where the second SAFE BUY_ABOVE lot is placed.
    Configurable via NEAR_SL_BUFFER_PTS env var (default 5.0).
    """
    return float(os.getenv("NEAR_SL_BUFFER_PTS", "5.0"))


def split_lots(
    total: int, trade_type: str, profile: str, adj: AdjustedSignal
) -> list[LotOrder]:
    # ── DISABLED FOR NOW — profile-based lot splitting removed ──────────────
    # All profiles get a single order: 100% of lots at adj.entry_price.
    # entry_price = midpoint for RANGING, exact entry_low for BUY_ABOVE/AVERAGE.
    # To re-enable profile logic uncomment the block below and remove this return.
    return [LotOrder(lots=total, entry_price=adj.entry_price)]

    # ── ORIGINAL PROFILE LOGIC (disabled — will re-enable later) ────────────
    # profile = profile.upper()
    #
    # if trade_type == "RANGING":
    #     if profile == "SAFE":
    #         # RANGING SAFE: all lots at midpoint (no buffer on entry)
    #         return [LotOrder(lots=total, entry_price=adj.entry_price)]
    #     else:
    #         # RISK: 50% at entry_low, 50% at entry_high (no buffer on either)
    #         half = max(1, total // 2)
    #         second = total - half
    #         second_price = adj.entry_high_adj or adj.entry_price
    #         orders = [LotOrder(lots=half, entry_price=adj.entry_low_adj)]
    #         if second > 0:
    #             orders.append(LotOrder(lots=second, entry_price=second_price))
    #         return orders
    #
    # if trade_type == "AVERAGE":
    #     ratio = 0.3 if profile == "SAFE" else 0.5
    #     entry_lots = max(1, round(total * ratio))
    #     second_lots = max(1, total - entry_lots)
    #     avg_price = adj.average_adj if adj.average_adj else adj.entry_price
    #     return [
    #         LotOrder(lots=entry_lots, entry_price=adj.entry_price),
    #         LotOrder(lots=second_lots, entry_price=avg_price),
    #     ]
    #
    # # BUY_ABOVE
    # if profile == "SAFE":
    #     half = max(1, total // 2)
    #     second = total - half
    #     # "near SL" = stoploss_adj + NEAR_SL_BUFFER_PTS (configurable via env)
    #     near_sl_price = round(adj.stoploss_adj + _near_sl_buffer(), 2)
    #     orders = [LotOrder(lots=half, entry_price=adj.entry_price)]
    #     if second > 0:
    #         orders.append(LotOrder(lots=second, entry_price=near_sl_price))
    #     return orders
    #
    # # RISK — single order
    # return [LotOrder(lots=total, entry_price=adj.entry_price)]
