"""Unit tests for lot_splitter.py."""
import pytest
from shared.rules.lot_splitter import split_lots, LotOrder
from shared.signal.types import RawSignal, AdjustedSignal


def _make_adj(entry_low=205, entry_high=215, sl=187, targets=None):
    raw = RawSignal(
        action="BUY", underlying="NIFTY", strike=24000, option_type="CE",
        entry_low=200, stoploss=185, targets=targets or [250],
        quantity_lots=1, redis_message_id="t",
    )
    return AdjustedSignal(
        raw=raw,
        entry_low_adj=entry_low,
        entry_high_adj=entry_high,
        stoploss_adj=sl,
        targets_adj=targets or [244],
        trade_type="RANGING",
    )


class TestRanging:
    def test_safe_30_70_split(self):
        adj = _make_adj()
        orders = split_lots(10, "RANGING", "SAFE", adj)
        assert len(orders) == 2
        assert orders[0].lots == 3    # 30% of 10
        assert orders[1].lots == 7    # 70% of 10
        assert orders[0].entry_price == 205
        assert orders[1].entry_price == 215

    def test_risk_50_50_split(self):
        adj = _make_adj()
        orders = split_lots(10, "RANGING", "RISK", adj)
        assert len(orders) == 2
        assert orders[0].lots == 5
        assert orders[1].lots == 5

    def test_single_lot_ranging(self):
        adj = _make_adj()
        orders = split_lots(1, "RANGING", "SAFE", adj)
        # With 1 lot, both get at least 1
        assert orders[0].lots >= 1
        assert orders[1].lots >= 1

    def test_average_same_as_ranging(self):
        adj = _make_adj()
        safe_ranging = split_lots(10, "RANGING", "SAFE", adj)
        safe_avg = split_lots(10, "AVERAGE", "SAFE", adj)
        assert safe_ranging[0].lots == safe_avg[0].lots


class TestBuyAbove:
    def test_safe_50_50_split(self):
        adj = _make_adj(entry_high=None)
        adj = adj.model_copy(update={"trade_type": "BUY_ABOVE", "entry_high_adj": None})
        orders = split_lots(4, "BUY_ABOVE", "SAFE", adj)
        assert len(orders) == 2
        assert orders[0].lots == 2
        assert orders[1].lots == 2
        assert orders[1].entry_price == adj.stoploss_adj + 5

    def test_risk_single_order(self):
        adj = _make_adj()
        adj = adj.model_copy(update={"trade_type": "BUY_ABOVE", "entry_high_adj": None})
        orders = split_lots(4, "BUY_ABOVE", "RISK", adj)
        assert len(orders) == 1
        assert orders[0].lots == 4
        assert orders[0].entry_price == adj.entry_low_adj

    def test_safe_odd_lots(self):
        adj = _make_adj()
        orders = split_lots(3, "BUY_ABOVE", "SAFE", adj)
        assert orders[0].lots + orders[1].lots == 3
