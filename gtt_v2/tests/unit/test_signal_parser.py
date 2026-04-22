"""Unit tests for signal parser — port of existing test_whitebox.py W1-W2 groups."""
import pytest
from shared.signal.parser import parse_message_to_signal, parse_strict_template
from shared.signal.expiry import parse_expiry_date
from datetime import date


class TestExpiryParser:
    def test_iso_format(self):
        assert parse_expiry_date("2025-12-26") == date(2025, 12, 26)

    def test_short_month(self):
        assert parse_expiry_date("26 DEC 25") == date(2025, 12, 26)

    def test_full_month(self):
        assert parse_expiry_date("18th DECEMBER") is not None

    def test_ordinal_suffix_removed(self):
        assert parse_expiry_date("6th NOVEMBER") is not None

    def test_no_year_uses_current(self):
        result = parse_expiry_date("26 DEC")
        assert result is not None

    def test_empty_returns_none(self):
        assert parse_expiry_date("") is None
        assert parse_expiry_date(None) is None

    def test_garbage_returns_none(self):
        assert parse_expiry_date("NOT A DATE") is None

    def test_compact_format(self):
        assert parse_expiry_date("26DEC25") == date(2025, 12, 26)


class TestStrictTemplate:
    def test_valid_template(self):
        result = parse_strict_template("ENTRY|SENSEX|72700PE|230|240|200|270|2026-04-02|1")
        assert result is not None
        assert result["action"] == "BUY"
        assert result["underlying"] == "SENSEX"
        assert result["strike"] == 72700
        assert result["option_type"] == "PE"
        assert result["entry_low"] == 230.0
        assert result["entry_high"] == 240.0
        assert result["stoploss"] == 200.0
        assert result["targets"] == [270.0]
        assert result["quantity_lots"] == 1

    def test_buy_verb(self):
        result = parse_strict_template("BUY|NIFTY|24000CE|200|210|185|250|2025-06-26|2")
        assert result["action"] == "BUY"

    def test_sell_verb(self):
        result = parse_strict_template("SELL|NIFTY|24000CE|210|200|225|180|2025-06-26|1")
        assert result["action"] == "SELL"

    def test_not_enough_parts(self):
        assert parse_strict_template("ENTRY|SENSEX") is None

    def test_invalid_verb(self):
        assert parse_strict_template("RANDOM|NIFTY|24000CE|200|210|185|250|2025-06-26|1") is None


class TestParseMessageToSignal:
    def test_structured_dict(self):
        msg = {
            "action": "BUY",
            "instrument": "NIFTY",
            "strike": "24000",
            "option_type": "CE",
            "entry_low": "200",
            "stoploss": "185",
            "targets": "250/300/400",
            "expiry": "26 DEC 25",
            "quantity": "1",
        }
        sig = parse_message_to_signal(msg, "test-id-1")
        assert sig is not None
        assert sig.action == "BUY"
        assert sig.underlying == "NIFTY"
        assert sig.entry_low == 200.0
        assert sig.stoploss == 185.0
        assert sig.targets == [250.0, 300.0, 400.0]

    def test_buy_sell_validation(self):
        # BUY: entry must be > SL
        msg = {
            "action": "BUY", "instrument": "NIFTY", "strike": "24000",
            "option_type": "CE", "entry_low": "180", "stoploss": "185",  # INVALID
            "targets": "250", "expiry": "26 DEC 25",
        }
        sig = parse_message_to_signal(msg)
        assert sig is None

    def test_targets_list_input(self):
        msg = {
            "action": "BUY", "instrument": "SENSEX", "entry_low": "300",
            "stoploss": "280", "targets": [350, 400, 450], "quantity": "1",
        }
        sig = parse_message_to_signal(msg)
        assert sig is not None
        assert sig.targets == [350.0, 400.0, 450.0]

    def test_missing_entry_returns_none(self):
        sig = parse_message_to_signal({"action": "BUY", "instrument": "NIFTY"})
        assert sig is None

    def test_non_dict_returns_none(self):
        assert parse_message_to_signal("not a dict") is None  # type: ignore

    def test_template_in_message_field(self):
        msg = {"message": "ENTRY|SENSEX|72700PE|230|240|200|270|2026-04-02|1"}
        sig = parse_message_to_signal(msg, "msg-id-2")
        assert sig is not None
        assert sig.underlying == "SENSEX"
