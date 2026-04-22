"""Market timing gate — blocks signals outside allowed trading hours."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

MARKET_OPEN    = time(9, 15)
SAFE_MIN_TIME  = time(9, 30)   # SAFE profile can't trade in first 15 min
ENTRY_CLOSE    = time(15, 30)  # No new entries after 3:30 PM


class MarketTimingGate:
    def check(self, profile: str) -> tuple[bool, str]:
        now_ist = datetime.now(IST)
        if now_ist.weekday() >= 5:
            return False, "weekend"

        t = now_ist.time()
        if t < MARKET_OPEN:
            return False, "before_market_open"
        if t < SAFE_MIN_TIME and profile.upper() == "SAFE":
            return False, "safe_before_9_30"
        if t >= ENTRY_CLOSE:
            return False, "after_entry_close"
        return True, ""
