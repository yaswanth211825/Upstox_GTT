"""Unit tests for trade type detection."""
from shared.rules.trade_type import detect_trade_type
from shared.signal.types import RawSignal


def _sig(entry_low, entry_high=None, trade_type=None):
    return RawSignal(
        action="BUY", underlying="NIFTY", entry_low=entry_low,
        entry_high=entry_high, stoploss=entry_low - 10, targets=[entry_low + 50],
        quantity_lots=1, redis_message_id="t", trade_type=trade_type,
    )


def test_buy_above():
    assert detect_trade_type(_sig(200)) == "BUY_ABOVE"


def test_ranging_when_entry_high_set():
    assert detect_trade_type(_sig(200, entry_high=210)) == "RANGING"


def test_average_explicit():
    assert detect_trade_type(_sig(200, trade_type="AVERAGE")) == "AVERAGE"


def test_not_ranging_when_entry_high_equals_entry_low():
    assert detect_trade_type(_sig(200, entry_high=200)) == "BUY_ABOVE"
