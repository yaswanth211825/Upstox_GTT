from datetime import datetime, timedelta, timezone

from services.price_monitor.main import old_expiry_logic, should_expire_trade
from shared.config import settings


def _mk_trade(signal_id: int, **overrides):
    base = {
        "id": signal_id,
        "action": "BUY",
        "status": "PENDING",
        "entry_low": 230.0,
        "entry_high": 240.0,
        "t1": 270.0,
        "entry_filled": False,
        "entry_range_touched": False,
        "signal_timestamp": datetime(2026, 4, 22, 9, 30, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


def test_new_logic_does_not_expire_without_entry_touch(monkeypatch):
    monkeypatch.setattr(settings, "use_entry_touch_logic", True)

    trade = _mk_trade(1)
    tick_time = trade["signal_timestamp"] + timedelta(seconds=5)

    # Jumps above T1 directly, but entry range never touched.
    assert should_expire_trade(trade, ltp=275.0, timestamp=tick_time) is False
    assert trade["entry_range_touched"] is False


def test_new_logic_expires_after_entry_touch_then_t1_cross(monkeypatch):
    monkeypatch.setattr(settings, "use_entry_touch_logic", True)

    trade = _mk_trade(2)
    t0 = trade["signal_timestamp"] + timedelta(seconds=1)

    # Tick touches entry range first.
    assert should_expire_trade(trade, ltp=235.0, timestamp=t0) is False
    assert trade["entry_range_touched"] is True

    # Later tick crosses above T1 while still not filled.
    assert should_expire_trade(trade, ltp=271.0, timestamp=t0 + timedelta(seconds=1)) is True


def test_ignores_ticks_before_signal_timestamp(monkeypatch):
    monkeypatch.setattr(settings, "use_entry_touch_logic", True)

    trade = _mk_trade(3)
    pre_signal_tick = trade["signal_timestamp"] - timedelta(seconds=1)

    assert should_expire_trade(trade, ltp=235.0, timestamp=pre_signal_tick) is False
    assert trade["entry_range_touched"] is False


def test_feature_flag_off_uses_old_logic_exactly(monkeypatch):
    monkeypatch.setattr(settings, "use_entry_touch_logic", False)

    trade = _mk_trade(4, entry_range_touched=False)
    tick_time = trade["signal_timestamp"] + timedelta(seconds=10)

    assert should_expire_trade(trade, ltp=271.0, timestamp=tick_time) == old_expiry_logic(trade, 271.0)


def test_multiple_trades_keep_state_isolated(monkeypatch):
    monkeypatch.setattr(settings, "use_entry_touch_logic", True)

    t = datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc)
    trade_a = _mk_trade(101, signal_timestamp=t)
    trade_b = _mk_trade(202, signal_timestamp=t)

    # Touch only trade A.
    assert should_expire_trade(trade_a, ltp=236.0, timestamp=t + timedelta(seconds=1)) is False
    assert should_expire_trade(trade_b, ltp=250.0, timestamp=t + timedelta(seconds=1)) is False

    assert trade_a["entry_range_touched"] is True
    assert trade_b["entry_range_touched"] is False
