"""Missed entry guard — skip GTT placement when the move is already done."""
import redis.asyncio as aioredis
import structlog
from ..signal.types import AdjustedSignal

log = structlog.get_logger()


async def check_missed_entry(
    adj: AdjustedSignal, redis: aioredis.Redis
) -> tuple[bool, str]:
    """
    Returns (can_place, reason).
    Blocks if:
    - BUY: LTP already >= targets_adj[0]  (trade already hit target)
    - BUY: LTP already <= stoploss_adj    (would enter into a loss immediately)
    If LTP not available, allows (can't verify, don't block).
    """
    if not adj.instrument_key:
        return True, ""

    raw_ltp = await redis.get(f"ltp:{adj.instrument_key}")
    if raw_ltp is None:
        return True, ""

    ltp = float(raw_ltp)
    action = adj.raw.action

    if action == "BUY":
        if adj.targets_adj and ltp >= adj.targets_adj[0]:
            reason = f"ltp={ltp} already_above_target={adj.targets_adj[0]}"
            log.warning("missed_entry_target_already_hit", reason=reason)
            return False, reason
        if ltp <= adj.stoploss_adj:
            reason = f"ltp={ltp} already_below_stoploss={adj.stoploss_adj}"
            log.warning("missed_entry_below_stoploss", reason=reason)
            return False, reason
    elif action == "SELL":
        if adj.targets_adj and ltp <= adj.targets_adj[0]:
            reason = f"ltp={ltp} already_below_target={adj.targets_adj[0]}"
            log.warning("missed_entry_target_already_hit", reason=reason)
            return False, reason
        if ltp >= adj.stoploss_adj:
            reason = f"ltp={ltp} already_above_stoploss={adj.stoploss_adj}"
            return False, reason

    return True, ""
