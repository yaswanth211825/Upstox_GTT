"""
Fake market clock — lets simulation override datetime.now(IST) so we can
pretend it's 9:32 AM on a Monday, or 15:31 (after close), etc.
"""
from datetime import datetime, time
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from unittest.mock import patch

IST = ZoneInfo("Asia/Kolkata")

# Realistic market days in 2025 (Monday)
_BASE_DATE = "2025-12-22"
_WEEKEND_DATE = "2025-12-20"   # Saturday

MARKET_TIMES = {
    "before_open":    time(9, 0),
    "pre_9_30":       time(9, 20),   # after open, before 9:30
    "open_safe":      time(9, 32),   # safe traders can trade
    "mid_session":    time(11, 0),
    "after_close":    time(15, 31),
    "weekend":        time(11, 0),   # Saturday — same time, different base date
}


def fake_now(t: time, base_date: str = _BASE_DATE) -> datetime:
    d = datetime.strptime(base_date, "%Y-%m-%d")
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=IST)


@contextmanager
def at_time(market_time: str):
    """Context manager: patches datetime.now in timing module to simulate market time."""
    import shared.rules.timing as timing_mod
    t = MARKET_TIMES[market_time]
    base = _WEEKEND_DATE if market_time == "weekend" else _BASE_DATE
    mock_dt = fake_now(t, base)
    with patch.object(timing_mod, "datetime") as m:
        m.now.return_value = mock_dt
        yield mock_dt
