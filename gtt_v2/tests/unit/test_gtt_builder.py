"""Unit tests for GTT payload builder."""
from shared.upstox.gtt_builder import build_gtt_payload
from shared.rules.lot_splitter import LotOrder


def test_build_gtt_payload_buy():
    lot = LotOrder(lots=2, entry_price=205.0)
    payload = build_gtt_payload(
        instrument_token="NSE_FO|12345",
        action="BUY",
        product="I",
        lot_order=lot,
        lot_size=75,
        stoploss_adj=187.5,
        target_adj=244.0,
    )
    assert payload["type"] == "MULTIPLE"
    assert payload["quantity"] == 150   # 2 lots × 75
    assert payload["transaction_type"] == "BUY"
    assert payload["instrument_token"] == "NSE_FO|12345"
    rules = {r["strategy"]: r for r in payload["rules"]}
    assert rules["ENTRY"]["trigger_type"] == "ABOVE"
    assert rules["ENTRY"]["trigger_price"] == 205.0
    assert rules["TARGET"]["trigger_price"] == 244.0
    assert rules["STOPLOSS"]["trigger_price"] == 187.5


def test_build_gtt_payload_sell():
    lot = LotOrder(lots=1, entry_price=210.0)
    payload = build_gtt_payload(
        instrument_token="NSE_FO|99999",
        action="SELL",
        product="I",
        lot_order=lot,
        lot_size=50,
        stoploss_adj=230.0,
        target_adj=180.0,
    )
    rules = {r["strategy"]: r for r in payload["rules"]}
    assert rules["ENTRY"]["trigger_type"] == "BELOW"
    assert payload["quantity"] == 50


def test_payload_has_correct_keys():
    lot = LotOrder(lots=1, entry_price=100.0)
    payload = build_gtt_payload("KEY", "BUY", "I", lot, 15, 90.0, 115.0)
    for key in ("type", "quantity", "product", "instrument_token", "transaction_type", "rules"):
        assert key in payload
    assert len(payload["rules"]) == 3
