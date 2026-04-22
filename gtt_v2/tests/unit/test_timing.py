"""Unit tests for timing gate."""
from unittest.mock import patch
from datetime import datetime, time
from zoneinfo import ZoneInfo
import pytest
from shared.rules.timing import MarketTimingGate

IST = ZoneInfo("Asia/Kolkata")
gate = MarketTimingGate()


def _mock_time(t: time, weekday: int = 0):
    """Create a mock datetime at given time on given weekday (0=Monday, 5=Saturday)."""
    return datetime(2025, 3, 3 + weekday, t.hour, t.minute, tzinfo=IST)


@pytest.mark.parametrize("t,profile,expected_ok,expected_reason", [
    # Before market open (9:15)
    (time(8, 0),   "SAFE", False, "before_market_open"),
    (time(9, 14),  "SAFE", False, "before_market_open"),
    # Between 9:15 and 9:30 — SAFE blocked, RISK allowed
    (time(9, 15),  "SAFE", False, "safe_before_9_30"),
    (time(9, 20),  "SAFE", False, "safe_before_9_30"),
    (time(9, 29),  "SAFE", False, "safe_before_9_30"),
    (time(9, 15),  "RISK", True,  ""),
    (time(9, 25),  "RISK", True,  ""),
    # After 9:30 — both allowed
    (time(9, 30),  "SAFE", True,  ""),
    (time(12, 0),  "SAFE", True,  ""),
    (time(12, 0),  "RISK", True,  ""),
    # After entry close (15:30)
    (time(15, 30), "SAFE", False, "after_entry_close"),
    (time(15, 30), "RISK", False, "after_entry_close"),
    (time(16, 0),  "SAFE", False, "after_entry_close"),
])
def test_timing_gate(t, profile, expected_ok, expected_reason):
    mock_dt = _mock_time(t, weekday=0)   # Monday
    with patch("shared.rules.timing.datetime") as mock_datetime:
        mock_datetime.now.return_value = mock_dt
        ok, reason = gate.check(profile)
    assert ok == expected_ok
    assert reason == expected_reason


def test_weekend_blocked():
    mock_dt = _mock_time(time(10, 0), weekday=5)  # Saturday
    with patch("shared.rules.timing.datetime") as mock_datetime:
        mock_datetime.now.return_value = mock_dt
        ok, reason = gate.check("SAFE")
    assert ok is False
    assert reason == "weekend"
