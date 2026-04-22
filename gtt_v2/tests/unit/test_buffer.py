"""Unit tests for buffer.py — Trading Floor buffer rules."""
import pytest
from shared.rules.buffer import get_buffer, adjust_entry, adjust_stoploss, adjust_target, apply_buffer
from shared.signal.types import RawSignal
from datetime import date


@pytest.mark.parametrize("price, expected", [
    (100,  3.0),
    (150,  3.0),
    (199,  3.0),
    (200,  5.0),
    (250,  5.0),
    (299,  5.0),
    (300,  7.0),
    (350,  7.0),
    (399,  7.0),
    (400, 10.0),
    (500, 10.0),
    (999, 10.0),
])
def test_get_buffer_tiers(price, expected):
    assert get_buffer(price) == expected


def test_adjust_entry_buy():
    # BUY: add buffer (price + pts)
    assert adjust_entry(200, "BUY") == 205.0   # 200-300 tier = 5 pts
    assert adjust_entry(100, "BUY") == 103.0   # 100-200 tier = 3 pts
    assert adjust_entry(300, "BUY") == 307.0   # 300-400 tier = 7 pts
    assert adjust_entry(400, "BUY") == 410.0   # 400+ tier = 10 pts


def test_adjust_entry_sell():
    # SELL: subtract buffer (price - pts)
    assert adjust_entry(200, "SELL") == 195.0
    assert adjust_entry(300, "SELL") == 293.0


def test_adjust_stoploss_buy(monkeypatch):
    monkeypatch.setenv("BUFFER_SL_PTS", "2.5")
    # BUY SL: add SL buffer (exit before reaching actual SL from above)
    assert adjust_stoploss(185, "BUY") == 187.5


def test_adjust_stoploss_sell(monkeypatch):
    monkeypatch.setenv("BUFFER_SL_PTS", "2.5")
    assert adjust_stoploss(215, "SELL") == 212.5


def test_adjust_target_buy(monkeypatch):
    monkeypatch.setenv("BUFFER_TARGET_PTS", "6.0")
    # BUY target: subtract target buffer (exit before actual target)
    assert adjust_target(250, "BUY") == 244.0


def test_adjust_target_sell(monkeypatch):
    monkeypatch.setenv("BUFFER_TARGET_PTS", "6.0")
    assert adjust_target(250, "SELL") == 256.0


def _make_signal(**kwargs) -> RawSignal:
    defaults = {
        "action": "BUY",
        "underlying": "NIFTY",
        "strike": 24000,
        "option_type": "CE",
        "entry_low": 200.0,
        "stoploss": 185.0,
        "targets": [250.0, 300.0],
        "quantity_lots": 1,
        "redis_message_id": "test-1",
    }
    defaults.update(kwargs)
    return RawSignal(**defaults)


def test_apply_buffer_buy_above(monkeypatch):
    monkeypatch.setenv("BUFFER_SL_PTS", "2.5")
    monkeypatch.setenv("BUFFER_TARGET_PTS", "6.0")
    sig = _make_signal()
    adj = apply_buffer(sig)
    assert adj.trade_type == "BUY_ABOVE"
    assert adj.entry_low_adj == 205.0    # 200 + 5
    assert adj.stoploss_adj == 187.5     # 185 + 2.5
    assert adj.targets_adj[0] == 244.0  # 250 - 6


def test_apply_buffer_ranging(monkeypatch):
    monkeypatch.setenv("BUFFER_SL_PTS", "2.5")
    monkeypatch.setenv("BUFFER_TARGET_PTS", "6.0")
    sig = _make_signal(entry_low=200.0, entry_high=210.0)
    adj = apply_buffer(sig)
    assert adj.trade_type == "RANGING"
    assert adj.entry_low_adj == 205.0
    assert adj.entry_high_adj == 215.0


def test_apply_buffer_does_not_mutate_raw():
    sig = _make_signal()
    adj = apply_buffer(sig)
    assert adj.raw.entry_low == 200.0   # raw unchanged
    assert adj.entry_low_adj != 200.0
