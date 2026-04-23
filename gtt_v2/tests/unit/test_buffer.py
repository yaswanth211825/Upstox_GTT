"""Unit tests for buffer.py — current Trading Floor buffer rules."""
import pytest
from shared.rules.buffer import (
    get_stoploss_buffer,
    get_target_buffer,
    adjust_stoploss,
    adjust_target,
    apply_buffer,
)
from shared.signal.types import RawSignal


@pytest.mark.parametrize("price, expected", [
    (100, 3.0),
    (150, 3.0),
    (199, 3.0),
    (200, 4.0),
    (250, 4.0),
    (299, 4.0),
    (300, 5.0),
    (350, 5.0),
    (399, 5.0),
    (400, 5.0),
    (700, 5.0),
])
def test_get_target_buffer_tiers(price, expected):
    assert get_target_buffer(price) == expected


def test_get_stoploss_buffer_is_fixed():
    assert get_stoploss_buffer() == 3.0


def test_adjust_stoploss_buy():
    assert adjust_stoploss(245, "BUY") == 242.0


def test_adjust_stoploss_sell():
    assert adjust_stoploss(245, "SELL") == 248.0


def test_adjust_target_buy():
    assert adjust_target(300, "BUY", 250) == 296.0
    assert adjust_target(300, "BUY", 350) == 295.0


def test_adjust_target_sell():
    assert adjust_target(300, "SELL", 250) == 304.0
    assert adjust_target(300, "SELL", 350) == 305.0


def _make_signal(**kwargs) -> RawSignal:
    defaults = {
        "action": "BUY",
        "underlying": "SENSEX",
        "strike": 78000,
        "option_type": "PE",
        "entry_low": 250.0,
        "entry_high": 260.0,
        "stoploss": 245.0,
        "targets": [300.0, 376.0, 456.0, 700.0],
        "quantity_lots": 1,
        "redis_message_id": "test-1",
    }
    defaults.update(kwargs)
    return RawSignal(**defaults)


def test_apply_buffer_ranging_buy():
    sig = _make_signal()
    adj = apply_buffer(sig)
    assert adj.trade_type == "RANGING"
    assert adj.entry_low_adj == 250.0
    assert adj.entry_high_adj == 260.0
    assert adj.entry_price == 255.0
    assert adj.stoploss_adj == 242.0
    assert adj.targets_adj == [296.0, 372.0, 452.0, 696.0]


def test_apply_buffer_different_target_tier():
    sig = _make_signal(entry_low=350.0, entry_high=360.0, stoploss=345.0, targets=[400.0, 460.0])
    adj = apply_buffer(sig)
    assert adj.entry_price == 355.0
    assert adj.stoploss_adj == 342.0
    assert adj.targets_adj == [395.0, 455.0]

