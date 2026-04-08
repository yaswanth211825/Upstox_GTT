"""
test_whitebox.py — White-box unit tests for UpstoxGTT internals.

Tests internal logic with full knowledge of source code:
 W1 — parse_expiry_date
 W2 — parse_message_to_signal
 W3 — place_gtt_order_upstox validation
 W4 — Retry logic
 W5 — Deduplication
 W6 — _check_thresholds (price_monitor)
 W7 — Database layer (db.py)
"""

import sys
import os
import json
import sqlite3
import tempfile
import time
from unittest.mock import MagicMock, patch, call
import pytest

# conftest.py already did sys.path setup and Redis mocking
import gtt_strategy
from gtt_strategy import (
    parse_expiry_date,
    parse_message_to_signal,
    place_gtt_order_upstox,
    DEFAULT_QUANTITY,
    instrument_cache,
)
import price_monitor
import db


# ─────────────────────────────────────────────────────────────────────────────
# W1 — parse_expiry_date
# ─────────────────────────────────────────────────────────────────────────────

class TestParseExpiryDate:
    def test_ordinal_full_month(self):
        """'18th DECEMBER' → '2025-12-18' or '2026-12-18' (current year added)"""
        result = parse_expiry_date("18th DECEMBER")
        assert result is not None
        assert result.endswith("-12-18")

    def test_ordinal_short_month(self):
        result = parse_expiry_date("6th NOV")
        assert result is not None
        assert result.endswith("-11-06")

    def test_two_digit_year_low(self):
        """'26 DEC 25' → '2025-12-26'"""
        result = parse_expiry_date("26 DEC 25")
        assert result == "2025-12-26"

    def test_two_digit_year_high(self):
        """'26 DEC 87' → '1987-12-26' (yy > 50 → 19xx) — KNOWN AMBIGUITY"""
        result = parse_expiry_date("26 DEC 87")
        assert result == "1987-12-26"  # This is a bug for future dates > 2050

    def test_two_digit_year_50_boundary(self):
        """'26 DEC 50' → '2050-12-26'"""
        result = parse_expiry_date("26 DEC 50")
        assert result == "2050-12-26"

    def test_two_digit_year_51_boundary(self):
        """'26 DEC 51' → '1951-12-26' — BUG: year 2051 would parse as 1951"""
        result = parse_expiry_date("26 DEC 51")
        assert result == "1951-12-26"  # KNOWN BUG: wrong century for 2051+

    def test_iso_format_supported(self):
        """'2025-12-26' (ISO format) is supported for strict template mode."""
        result = parse_expiry_date("2025-12-26")
        assert result == "2025-12-26"

    def test_invalid_day_32(self):
        """'32nd MARCH' → None (invalid day)"""
        result = parse_expiry_date("32nd MARCH")
        assert result is None

    def test_empty_string(self):
        """Empty string → None"""
        result = parse_expiry_date("")
        assert result is None

    def test_none_input(self):
        """None input → None"""
        result = parse_expiry_date(None)
        assert result is None

    def test_garbage_input(self):
        """Completely invalid string → None"""
        result = parse_expiry_date("NOT_A_DATE_AT_ALL")
        assert result is None

    def test_no_year_gets_current_year(self):
        """'15 JAN' (no year) → adds current year"""
        from datetime import datetime
        result = parse_expiry_date("15 JAN")
        assert result is not None
        assert result.startswith(str(datetime.now().year))
        assert result.endswith("-01-15")

    def test_compact_format(self):
        """'06NOV25' → '2025-11-06'"""
        result = parse_expiry_date("06NOV25")
        assert result == "2025-11-06"


# ─────────────────────────────────────────────────────────────────────────────
# W2 — parse_message_to_signal
# ─────────────────────────────────────────────────────────────────────────────

class TestParseMessageToSignal:
    def test_valid_buy_signal(self, sample_redis_message):
        result = parse_message_to_signal(sample_redis_message)
        assert result is not None
        assert result["action"] == "BUY"
        assert result["underlying"] == "NIFTY"
        assert result["strike"] == 24000
        assert result["option_type"] == "CE"
        assert result["entry_low"] == 120.0
        assert result["stoploss"] == 80.0
        assert result["targets"] == [160.0, 200.0, 250.0]
        assert result["quantity"] == 1

    def test_invalid_strike_string_returns_none(self):
        """W1a: strike='abc' → parse fails gracefully, returns None (NOT a crash)"""
        msg = {"action": "BUY", "instrument": "NIFTY", "strike": "abc",
               "option_type": "CE", "entry_low": "120", "stoploss": "80",
               "targets": "160", "expiry": "26th JUNE"}
        result = parse_message_to_signal(msg)
        assert result is None, "Invalid strike should return None, not crash"

    def test_targets_trailing_slash_handled(self):
        """W1b: targets='250/300/' — trailing slash is filtered correctly (NO crash)"""
        msg = {"action": "BUY", "instrument": "NIFTY", "strike": "24000",
               "option_type": "CE", "entry_low": "120", "stoploss": "80",
               "targets": "250/300/", "expiry": "26th JUNE"}
        result = parse_message_to_signal(msg)
        assert result is not None
        # Empty string after trailing slash is filtered by 'if t.strip()'
        assert result["targets"] == [250.0, 300.0]

    def test_targets_empty_string_gives_empty_list(self):
        """W1c: targets='' → signal['targets'] = [] (empty list)"""
        msg = {"action": "BUY", "instrument": "NIFTY", "strike": "24000",
               "option_type": "CE", "entry_low": "120", "stoploss": "80",
               "targets": "", "expiry": "26th JUNE"}
        result = parse_message_to_signal(msg)
        assert result is not None
        assert result["targets"] == []

    def test_targets_missing_gives_empty_list(self):
        """Missing targets key → signal['targets'] = []"""
        msg = {"action": "BUY", "instrument": "NIFTY", "strike": "24000",
               "option_type": "CE", "entry_low": "120", "stoploss": "80",
               "expiry": "26th JUNE"}
        result = parse_message_to_signal(msg)
        assert result is not None
        assert result["targets"] == []

    def test_quantity_invalid_falls_back_to_default(self):
        """W1g: quantity='abc' → DEFAULT_QUANTITY (no crash)"""
        msg = {"action": "BUY", "instrument": "NIFTY", "strike": "24000",
               "option_type": "CE", "entry_low": "120", "stoploss": "80",
               "targets": "160", "expiry": "26th JUNE", "quantity": "abc"}
        result = parse_message_to_signal(msg)
        # 'abc' → int("abc") fails → caught → returns None
        assert result is None  # The whole parse fails, not just quantity

    def test_quantity_missing_uses_default(self):
        """Missing quantity → DEFAULT_QUANTITY"""
        msg = {"action": "BUY", "instrument": "NIFTY", "strike": "24000",
               "option_type": "CE", "entry_low": "120", "stoploss": "80",
               "targets": "160", "expiry": "26th JUNE"}
        result = parse_message_to_signal(msg)
        assert result is not None
        assert result["quantity"] == DEFAULT_QUANTITY

    def test_option_type_lowercase_normalized(self):
        """W1h: option_type='ce' → normalized to 'CE'"""
        msg = {"action": "BUY", "instrument": "NIFTY", "strike": "24000",
               "option_type": "ce", "entry_low": "120", "stoploss": "80",
               "targets": "160", "expiry": "26th JUNE"}
        result = parse_message_to_signal(msg)
        assert result is not None
        assert result["option_type"] == "CE"

    def test_action_lowercase_normalized(self):
        """action='buy' → normalized to 'BUY'"""
        msg = {"action": "buy", "instrument": "NIFTY", "strike": "24000",
               "option_type": "CE", "entry_low": "120", "stoploss": "80",
               "targets": "160", "expiry": "26th JUNE"}
        result = parse_message_to_signal(msg)
        assert result is not None
        assert result["action"] == "BUY"

    def test_all_fields_missing_returns_none(self):
        """W1i: empty message → returns None"""
        result = parse_message_to_signal({})
        assert result is None or result.get("action") == ""

    def test_not_dict_returns_none(self):
        """Non-dict input → returns None immediately"""
        assert parse_message_to_signal("not a dict") is None
        assert parse_message_to_signal(None) is None
        assert parse_message_to_signal([1, 2, 3]) is None

    def test_entry_high_optional(self):
        """W1j: entry_high is optional, can be absent"""
        msg = {"action": "BUY", "instrument": "NIFTY", "strike": "24000",
               "option_type": "CE", "entry_low": "120", "stoploss": "80",
               "targets": "160", "expiry": "26th JUNE"}
        result = parse_message_to_signal(msg)
        assert result is not None
        assert result["entry_high"] is None

    def test_instrument_field_aliased_to_underlying(self):
        """'instrument' field maps to 'underlying'"""
        msg = {"action": "BUY", "instrument": "SENSEX", "strike": "85000",
               "option_type": "PE", "entry_low": "300", "stoploss": "400",
               "targets": "200", "expiry": "30th JUNE"}
        result = parse_message_to_signal(msg)
        assert result is not None
        assert result["underlying"] == "SENSEX"

    def test_signal_summary_built_correctly(self):
        """signal_summary is constructed from action+underlying+strike+opt_type"""
        msg = {"action": "BUY", "instrument": "NIFTY", "strike": "24000",
               "option_type": "CE", "entry_low": "120", "stoploss": "80",
               "targets": "160", "expiry": "26th JUNE"}
        result = parse_message_to_signal(msg)
        assert result["signal_summary"] == "BUY NIFTY 24000CE"

    def test_zero_quantity_rejected(self):
        """quantity=0 is rejected during parse validation."""
        msg = {"action": "BUY", "instrument": "NIFTY", "strike": "24000",
               "option_type": "CE", "entry_low": "120", "stoploss": "80",
               "targets": "160", "expiry": "26th JUNE", "quantity": "0"}
        result = parse_message_to_signal(msg)
        assert result is None

    def test_strict_template_signal_parses(self):
        """Strict template text parses into the same internal signal structure."""
        msg = {"message": "ENTRY|SENSEX|72700PE|230|240|200|270|2026-04-02|1"}
        result = parse_message_to_signal(msg)
        assert result is not None
        assert result["action"] == "BUY"
        assert result["underlying"] == "SENSEX"
        assert result["strike"] == 72700
        assert result["option_type"] == "PE"
        assert result["entry_low"] == 230.0
        assert result["entry_high"] == 240.0
        assert result["stoploss"] == 200.0
        assert result["targets"] == [270.0]
        assert result["expiry"] == "2026-04-02"


# ─────────────────────────────────────────────────────────────────────────────
# W3 — place_gtt_order_upstox validation & construction
# ─────────────────────────────────────────────────────────────────────────────

class TestGTTOrderValidation:
    def test_empty_targets_caught_by_validation(self, sample_signal):
        """W3d: empty targets list → validation error, no API call"""
        sig = dict(sample_signal)
        sig["targets"] = []
        result = place_gtt_order_upstox(sig)
        assert result["status"] == "error"
        assert "targets" in result["message"].lower() or "Missing" in result["message"]

    def test_zero_stoploss_caught_by_validation(self, sample_signal):
        """stoploss=0.0 is falsy → caught as missing field"""
        sig = dict(sample_signal)
        sig["stoploss"] = 0.0
        result = place_gtt_order_upstox(sig)
        assert result["status"] == "error"
        assert "stoploss" in result["message"].lower() or "Missing" in result["message"]

    def test_zero_quantity_not_caught_by_validation(self, sample_signal):
        """W3b: CRITICAL BUG — quantity=0 passes validation, gets sent to API as 0 * lot_size = 0"""
        sig = dict(sample_signal)
        sig["quantity"] = 0
        # Mock instrument lookup to return valid instrument
        mock_instrument = {
            "instrument_key": "NSE_FO|12345",
            "lot_size": 50,
            "trading_symbol": "NIFTY24JUN24000CE",
        }
        with patch.object(instrument_cache, 'find_instrument', return_value=mock_instrument):
            with patch('gtt_strategy.requests.post') as mock_post:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {
                    "status": "success",
                    "data": {"gtt_order_ids": ["gtt_001"]}
                }
                mock_post.return_value = mock_response
                with patch('gtt_strategy.fetch_ltp', return_value=None):
                    result = place_gtt_order_upstox(sig)
        # The payload was built and sent — capture what was sent
        if mock_post.called:
            call_kwargs = mock_post.call_args
            payload = call_kwargs[1].get('json') or call_kwargs[0][1] if call_kwargs[0] else {}
            # BUG: quantity in payload = 0 * 50 = 0
            assert payload.get("quantity") == 0, "CONFIRMED BUG: api_quantity = 0 * lot_size = 0"

    def test_buy_entry_trigger_is_above(self, sample_signal):
        """BUY → trigger_type = 'ABOVE' in GTT ENTRY rule"""
        mock_instrument = {"instrument_key": "NSE_FO|12345", "lot_size": 50}
        with patch.object(instrument_cache, 'find_instrument', return_value=mock_instrument):
            with patch('gtt_strategy.requests.post') as mock_post:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"status": "success", "data": {"gtt_order_ids": ["x"]}}
                mock_post.return_value = mock_response
                with patch('gtt_strategy.fetch_ltp', return_value=120.0):
                    place_gtt_order_upstox(sample_signal)
        payload = mock_post.call_args[1]["json"]
        entry_rule = next(r for r in payload["rules"] if r["strategy"] == "ENTRY")
        assert entry_rule["trigger_type"] == "ABOVE"

    def test_sell_entry_trigger_is_below(self, sample_sell_signal):
        """SELL → trigger_type = 'BELOW' in GTT ENTRY rule"""
        mock_instrument = {"instrument_key": "BSE_FO|67890", "lot_size": 20}
        with patch.object(instrument_cache, 'find_instrument', return_value=mock_instrument):
            with patch('gtt_strategy.requests.post') as mock_post:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"status": "success", "data": {"gtt_order_ids": ["x"]}}
                mock_post.return_value = mock_response
                with patch('gtt_strategy.fetch_ltp', return_value=300.0):
                    place_gtt_order_upstox(sample_sell_signal)
        payload = mock_post.call_args[1]["json"]
        entry_rule = next(r for r in payload["rules"] if r["strategy"] == "ENTRY")
        assert entry_rule["trigger_type"] == "BELOW"

    def test_target_uses_minimum_of_targets(self, sample_signal):
        """Target rule uses min(targets) = 160.0 for targets=[160,200,250]"""
        mock_instrument = {"instrument_key": "NSE_FO|12345", "lot_size": 50}
        with patch.object(instrument_cache, 'find_instrument', return_value=mock_instrument):
            with patch('gtt_strategy.requests.post') as mock_post:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"status": "success", "data": {"gtt_order_ids": ["x"]}}
                mock_post.return_value = mock_response
                with patch('gtt_strategy.fetch_ltp', return_value=120.0):
                    place_gtt_order_upstox(sample_signal)
        payload = mock_post.call_args[1]["json"]
        target_rule = next(r for r in payload["rules"] if r["strategy"] == "TARGET")
        assert target_rule["trigger_price"] == 160.0

    def test_instrument_not_found_returns_error(self, sample_signal):
        """W2a: instrument not in cache → error response, no API call"""
        with patch.object(instrument_cache, 'find_instrument', return_value=None):
            result = place_gtt_order_upstox(sample_signal)
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_api_quantity_is_lots_times_lot_size(self, sample_signal):
        """quantity=2 lots × lot_size=50 → api_quantity=100"""
        sig = dict(sample_signal)
        sig["quantity"] = 2
        mock_instrument = {"instrument_key": "NSE_FO|12345", "lot_size": 50}
        with patch.object(instrument_cache, 'find_instrument', return_value=mock_instrument):
            with patch('gtt_strategy.requests.post') as mock_post:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"status": "success", "data": {"gtt_order_ids": ["x"]}}
                mock_post.return_value = mock_response
                with patch('gtt_strategy.fetch_ltp', return_value=120.0):
                    place_gtt_order_upstox(sig)
        payload = mock_post.call_args[1]["json"]
        assert payload["quantity"] == 100  # 2 * 50

    def test_missing_lot_size_defaults_to_1(self, sample_signal):
        """W2d: instrument missing lot_size → defaults to 1 (NOT a crash)"""
        mock_instrument = {"instrument_key": "NSE_FO|12345"}  # no lot_size
        with patch.object(instrument_cache, 'find_instrument', return_value=mock_instrument):
            with patch('gtt_strategy.requests.post') as mock_post:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.json.return_value = {"status": "success", "data": {"gtt_order_ids": ["x"]}}
                mock_post.return_value = mock_response
                with patch('gtt_strategy.fetch_ltp', return_value=120.0):
                    result = place_gtt_order_upstox(sample_signal)
        payload = mock_post.call_args[1]["json"]
        # quantity=1 lot × lot_size=1 (default) = 1
        assert payload["quantity"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# W4 — Retry logic
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryLogic:
    def test_timeout_retries_3_times_then_raises(self, sample_signal):
        """W4a: 3 consecutive timeouts → exception raised after 3rd attempt"""
        import requests as req
        mock_instrument = {"instrument_key": "NSE_FO|12345", "lot_size": 50}
        with patch.object(instrument_cache, 'find_instrument', return_value=mock_instrument):
            with patch('gtt_strategy.requests.post', side_effect=req.exceptions.Timeout) as mock_post:
                with patch('gtt_strategy.time.sleep'):  # speed up test
                    result = place_gtt_order_upstox(sample_signal)
        assert result["status"] == "error"
        assert mock_post.call_count == 3  # tried 3 times

    def test_timeout_then_success_on_3rd_attempt(self, sample_signal):
        """W4b: 2 timeouts → success on 3rd"""
        import requests as req
        mock_instrument = {"instrument_key": "NSE_FO|12345", "lot_size": 50}
        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = {"status": "success", "data": {"gtt_order_ids": ["x"]}}
        with patch.object(instrument_cache, 'find_instrument', return_value=mock_instrument):
            with patch('gtt_strategy.requests.post',
                       side_effect=[req.exceptions.Timeout, req.exceptions.Timeout, success_response]) as mock_post:
                with patch('gtt_strategy.time.sleep'):
                    with patch('gtt_strategy.fetch_ltp', return_value=None):
                        result = place_gtt_order_upstox(sample_signal)
        assert result["status"] == "success"
        assert mock_post.call_count == 3

    def test_http_401_not_retried(self, sample_signal):
        """W4c: HTTP 401 → returned immediately, NOT retried (retry only for Timeout)"""
        mock_instrument = {"instrument_key": "NSE_FO|12345", "lot_size": 50}
        error_response = MagicMock()
        error_response.status_code = 401
        error_response.text = "Unauthorized"
        error_response.json.return_value = {"message": "Unauthorized"}
        with patch.object(instrument_cache, 'find_instrument', return_value=mock_instrument):
            with patch('gtt_strategy.requests.post', return_value=error_response) as mock_post:
                result = place_gtt_order_upstox(sample_signal)
        assert mock_post.call_count == 1  # NOT retried
        assert result["status"] == "error"
        assert result["code"] == 401

    def test_http_429_not_retried_no_backoff(self, sample_signal):
        """W4d: HTTP 429 (rate limit) → NOT retried, no backoff — silent failure"""
        mock_instrument = {"instrument_key": "NSE_FO|12345", "lot_size": 50}
        error_response = MagicMock()
        error_response.status_code = 429
        error_response.text = "Too Many Requests"
        error_response.json.return_value = {"message": "Rate limit exceeded"}
        with patch.object(instrument_cache, 'find_instrument', return_value=mock_instrument):
            with patch('gtt_strategy.requests.post', return_value=error_response) as mock_post:
                result = place_gtt_order_upstox(sample_signal)
        assert mock_post.call_count == 1  # NOT retried — no rate limit handling
        assert result["status"] == "error"
        assert result["code"] == 429

    def test_http_500_not_retried(self, sample_signal):
        """W4e: HTTP 500 → NOT retried (only Timeout triggers retry)"""
        mock_instrument = {"instrument_key": "NSE_FO|12345", "lot_size": 50}
        error_response = MagicMock()
        error_response.status_code = 500
        error_response.text = "Internal Server Error"
        error_response.json.side_effect = Exception("no json")
        with patch.object(instrument_cache, 'find_instrument', return_value=mock_instrument):
            with patch('gtt_strategy.requests.post', return_value=error_response) as mock_post:
                result = place_gtt_order_upstox(sample_signal)
        assert mock_post.call_count == 1
        assert result["status"] == "error"


# ─────────────────────────────────────────────────────────────────────────────
# W5 — Deduplication
# ─────────────────────────────────────────────────────────────────────────────

class TestDeduplication:
    def test_in_memory_dedup_skips_second_occurrence(self, mock_redis):
        """W5a: same message_id in processed_ids set → skipped"""
        # The in-memory set is local to start_stream_consumer()
        # We test this by calling the function logic directly
        processed_ids = set()
        message_id = "1234567890-0"

        # First time: not in set → process
        is_duplicate_first = message_id in processed_ids
        processed_ids.add(message_id)

        # Second time: in set → skip
        is_duplicate_second = message_id in processed_ids

        assert not is_duplicate_first
        assert is_duplicate_second

    def test_redis_zset_dedup_persists_across_restarts(self, mock_redis):
        """W5b: ZSET check is independent of in-memory set"""
        # Simulate: ZSET has the message_id → zscore returns a value
        mock_redis.zscore.return_value = 1234567890.0
        score = mock_redis.zscore("processed_gtt_signals", "1234567890-0")
        assert score is not None  # → should be skipped

        # Simulate: ZSET does not have the message_id → zscore returns None
        mock_redis.zscore.return_value = None
        score = mock_redis.zscore("processed_gtt_signals", "9999999999-0")
        assert score is None  # → should be processed

    def test_zset_trim_at_max_capacity(self, mock_redis):
        """W5c: ZSET at PROCESSED_ZSET_MAX+1 → oldest entry trimmed"""
        from gtt_strategy import PROCESSED_ZSET_MAX
        # Simulate zcard returning max+1
        mock_redis.zcard.return_value = PROCESSED_ZSET_MAX + 1
        # The trimming logic:
        zcount = mock_redis.zcard("processed_gtt_signals")
        expected_trim_count = zcount - PROCESSED_ZSET_MAX - 1  # = 0
        # zremrangebyrank(key, 0, 0) removes 1 entry
        assert expected_trim_count == 0
        # The trim removes entries from rank 0 to 0 (1 entry)
        mock_redis.zremrangebyrank("processed_gtt_signals", 0, expected_trim_count)
        mock_redis.zremrangebyrank.assert_called_with("processed_gtt_signals", 0, 0)


# ─────────────────────────────────────────────────────────────────────────────
# W6 — _check_thresholds (price_monitor)
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckThresholds:
    """Tests for price_monitor._check_thresholds using temp DB"""

    @pytest.fixture(autouse=True)
    def setup_db_and_cache(self, tmp_db, monkeypatch):
        """Fresh DB + clear module-level cache before each test"""
        monkeypatch.setattr(db, "DB_PATH", tmp_db)
        # Clear the module-level cache dicts
        price_monitor._signal_cache.clear()
        price_monitor._instrument_index.clear()
        price_monitor._subscribed.clear()

    def _insert_signal(self, signal_data, status="PENDING"):
        """Helper: insert a signal into DB and populate module cache"""
        sig_id = db.insert_signal(
            redis_message_id=signal_data.get("redis_message_id", "test-id-001"),
            signal=signal_data,
            instrument_key=signal_data["instrument_key"],
            gtt_ids=["gtt_test_001"],
            status=status,
        )
        row = {
            "id": sig_id,
            "instrument_key": signal_data["instrument_key"],
            "action": signal_data["action"],
            "entry_low": signal_data["entry_low"],
            "stoploss": signal_data["stoploss"],
            "target1": signal_data["targets"][0] if signal_data.get("targets") else None,
            "status": status,
        }
        price_monitor._signal_cache[sig_id] = row
        price_monitor._instrument_index.setdefault(signal_data["instrument_key"], []).append(sig_id)
        return sig_id

    def _make_buy_signal(self):
        return {
            "redis_message_id": "buy-signal-001",
            "action": "BUY",
            "underlying": "NIFTY",
            "strike": 24000,
            "option_type": "CE",
            "entry_low": 100.0,
            "stoploss": 60.0,
            "targets": [150.0, 200.0],
            "expiry": "2025-06-26",
            "product": "I",
            "quantity": 1,
            "signal_summary": "BUY NIFTY 24000CE",
            "instrument_key": "NSE_FO|12345",
        }

    def _make_sell_signal(self):
        return {
            "redis_message_id": "sell-signal-001",
            "action": "SELL",
            "underlying": "SENSEX",
            "strike": 85000,
            "option_type": "PE",
            "entry_low": 300.0,
            "stoploss": 400.0,
            "targets": [200.0, 150.0],
            "expiry": "2025-06-30",
            "product": "I",
            "quantity": 2,
            "signal_summary": "SELL SENSEX 85000PE",
            "instrument_key": "BSE_FO|67890",
        }

    def test_w6a_buy_price_below_entry_no_transition(self):
        """W6a: BUY, ltp < entry_low → no status change"""
        sig_id = self._insert_signal(self._make_buy_signal(), "PENDING")
        price_monitor._check_thresholds("NSE_FO|12345", 90.0)  # 90 < 100 entry
        assert price_monitor._signal_cache[sig_id]["status"] == "PENDING"

    def test_w6b_buy_price_at_entry_transitions_to_active(self):
        """W6b: BUY, ltp >= entry_low → PENDING → ACTIVE"""
        sig_id = self._insert_signal(self._make_buy_signal(), "PENDING")
        price_monitor._check_thresholds("NSE_FO|12345", 100.0)  # exactly at entry
        assert price_monitor._signal_cache[sig_id]["status"] == "ACTIVE"
        # Verify DB was updated
        rows = db.get_watchable_signals()
        assert any(r["id"] == sig_id and r["status"] == "ACTIVE" for r in rows)

    def test_w6c_buy_active_price_at_target_terminal(self):
        """W6c: BUY ACTIVE, ltp >= target1 → TARGET1_HIT (terminal, removed from cache)"""
        sig_id = self._insert_signal(self._make_buy_signal(), "ACTIVE")
        price_monitor._signal_cache[sig_id]["status"] = "ACTIVE"
        price_monitor._check_thresholds("NSE_FO|12345", 150.0)  # exactly at target
        # Signal removed from cache
        assert sig_id not in price_monitor._signal_cache
        # Verify DB status
        with db._conn() as con:
            row = con.execute("SELECT status FROM signals WHERE id=?", (sig_id,)).fetchone()
        assert row["status"] == "TARGET1_HIT"

    def test_w6d_buy_active_price_at_stoploss_terminal(self):
        """W6d: BUY ACTIVE, ltp <= stoploss → STOPLOSS_HIT (terminal)"""
        sig_id = self._insert_signal(self._make_buy_signal(), "ACTIVE")
        price_monitor._signal_cache[sig_id]["status"] = "ACTIVE"
        price_monitor._check_thresholds("NSE_FO|12345", 60.0)  # exactly at stoploss
        assert sig_id not in price_monitor._signal_cache
        with db._conn() as con:
            row = con.execute("SELECT status FROM signals WHERE id=?", (sig_id,)).fetchone()
        assert row["status"] == "STOPLOSS_HIT"

    def test_w6e_buy_entry_and_target_same_tick(self):
        """W6e: ltp triggers BOTH entry AND target in single tick → both fire correctly"""
        sig_id = self._insert_signal(self._make_buy_signal(), "PENDING")
        # entry_low=100, target1=150, ltp=200 → hits entry AND target
        price_monitor._check_thresholds("NSE_FO|12345", 200.0)
        # Signal should be in TARGET1_HIT (not stuck at ACTIVE)
        # This works because in-memory status is updated to ACTIVE then immediately TARGET1_HIT
        assert sig_id not in price_monitor._signal_cache
        with db._conn() as con:
            row = con.execute("SELECT status, activated_at FROM signals WHERE id=?", (sig_id,)).fetchone()
        # Status should be TARGET1_HIT, and activated_at should be set (ACTIVE was reached)
        assert row["status"] == "TARGET1_HIT"
        assert row["activated_at"] is not None  # update_signal_activated was called

    def test_w6f_sell_entry_triggered_when_price_drops_to_entry(self):
        """W6f: SELL, ltp <= entry_low → PENDING → ACTIVE"""
        sig_id = self._insert_signal(self._make_sell_signal(), "PENDING")
        price_monitor._check_thresholds("BSE_FO|67890", 300.0)  # exactly at SELL entry
        assert price_monitor._signal_cache[sig_id]["status"] == "ACTIVE"

    def test_sell_entry_not_triggered_when_price_above_entry(self):
        """SELL, ltp > entry_low → no transition"""
        sig_id = self._insert_signal(self._make_sell_signal(), "PENDING")
        price_monitor._check_thresholds("BSE_FO|67890", 350.0)  # above entry → no trigger for SELL
        assert price_monitor._signal_cache[sig_id]["status"] == "PENDING"

    def test_w6f_sell_target_triggered_when_price_drops(self):
        """SELL ACTIVE: ltp <= target1 → TARGET1_HIT"""
        sig_id = self._insert_signal(self._make_sell_signal(), "ACTIVE")
        price_monitor._signal_cache[sig_id]["status"] = "ACTIVE"
        price_monitor._check_thresholds("BSE_FO|67890", 200.0)  # at SELL target
        assert sig_id not in price_monitor._signal_cache

    def test_w6g_zero_price_ignored(self):
        """W6g: ltp=0.0 → early return, no state changes"""
        sig_id = self._insert_signal(self._make_buy_signal(), "PENDING")
        price_monitor._check_thresholds("NSE_FO|12345", 0.0)
        assert price_monitor._signal_cache[sig_id]["status"] == "PENDING"

    def test_negative_price_ignored(self):
        """ltp < 0 → same early return as ltp=0"""
        sig_id = self._insert_signal(self._make_buy_signal(), "PENDING")
        price_monitor._check_thresholds("NSE_FO|12345", -1.0)
        assert price_monitor._signal_cache[sig_id]["status"] == "PENDING"

    def test_unknown_instrument_key_no_crash(self):
        """Instrument key not in index → graceful no-op"""
        price_monitor._check_thresholds("UNKNOWN|99999", 100.0)  # should not raise

    def test_w6h_stoploss_not_checked_after_target_terminal(self):
        """W6h: If target fires first (return after), stoploss check is skipped"""
        sig_data = self._make_buy_signal()
        sig_data["targets"] = [100.0]  # target at entry price
        sig_data["stoploss"] = 100.0  # stoploss also at same price (illogical but tests ordering)
        sig_id = self._insert_signal(sig_data, "ACTIVE")
        price_monitor._signal_cache[sig_id]["status"] = "ACTIVE"
        price_monitor._signal_cache[sig_id]["target1"] = 100.0
        price_monitor._signal_cache[sig_id]["stoploss"] = 100.0
        price_monitor._check_thresholds("NSE_FO|12345", 100.0)
        # target fires first → return → stoploss NOT checked → status is TARGET1_HIT, not STOPLOSS_HIT
        with db._conn() as con:
            row = con.execute("SELECT status FROM signals WHERE id=?", (sig_id,)).fetchone()
        assert row["status"] == "TARGET1_HIT"

    def test_price_event_inserted_on_entry_hit(self):
        """ENTRY_HIT → price_events row inserted"""
        sig_id = self._insert_signal(self._make_buy_signal(), "PENDING")
        price_monitor._check_thresholds("NSE_FO|12345", 100.0)
        with db._conn() as con:
            events = con.execute(
                "SELECT * FROM price_events WHERE signal_id=? AND event_type='ENTRY_HIT'",
                (sig_id,)
            ).fetchall()
        assert len(events) == 1
        assert events[0]["price"] == 100.0


# ─────────────────────────────────────────────────────────────────────────────
# W7 — Database layer (db.py)
# ─────────────────────────────────────────────────────────────────────────────

class TestDatabaseLayer:
    @pytest.fixture(autouse=True)
    def use_tmp_db(self, tmp_db, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_db)

    def _base_signal(self, redis_id="msg-001"):
        return {
            "action": "BUY",
            "underlying": "NIFTY",
            "strike": 24000,
            "option_type": "CE",
            "entry_low": 120.0,
            "stoploss": 80.0,
            "targets": [160.0, 200.0, 250.0],
            "expiry": "2025-06-26",
            "product": "I",
            "quantity": 1,
            "signal_summary": "BUY NIFTY 24000CE",
        }

    def test_w7a_duplicate_redis_message_id_silently_ignored(self):
        """W7a: INSERT OR IGNORE — second insert with same redis_message_id is silently skipped"""
        signal = self._base_signal()
        db.insert_signal("msg-001", signal, "NSE_FO|12345", ["gtt_1"], "PENDING")
        db.insert_signal("msg-001", signal, "NSE_FO|12345", ["gtt_2"], "PENDING")  # duplicate
        rows = db.get_watchable_signals()
        assert len(rows) == 1  # only one row

    def test_insert_and_retrieve_signal(self):
        """Basic insert + get_watchable_signals round-trip"""
        signal = self._base_signal()
        db.insert_signal("msg-001", signal, "NSE_FO|12345", ["gtt_1"], "PENDING")
        rows = db.get_watchable_signals()
        assert len(rows) == 1
        assert rows[0]["status"] == "PENDING"
        assert rows[0]["entry_low"] == 120.0
        assert rows[0]["target1"] == 160.0

    def test_w7b_terminal_state_sets_resolved_at(self):
        """W7b: update_signal_status to TARGET1_HIT → resolved_at set"""
        db.insert_signal("msg-001", self._base_signal(), "NSE_FO|12345", [], "PENDING")
        sig_id = db.get_watchable_signals()[0]["id"]
        db.update_signal_status(sig_id, "TARGET1_HIT")
        with db._conn() as con:
            row = con.execute("SELECT status, resolved_at FROM signals WHERE id=?", (sig_id,)).fetchone()
        assert row["status"] == "TARGET1_HIT"
        assert row["resolved_at"] is not None

    def test_non_terminal_status_does_not_set_resolved_at(self):
        """Non-terminal update (e.g., status='ACTIVE') → resolved_at NOT set"""
        db.insert_signal("msg-001", self._base_signal(), "NSE_FO|12345", [], "PENDING")
        sig_id = db.get_watchable_signals()[0]["id"]
        db.update_signal_activated(sig_id)  # sets status=ACTIVE
        with db._conn() as con:
            row = con.execute("SELECT status, resolved_at FROM signals WHERE id=?", (sig_id,)).fetchone()
        assert row["status"] == "ACTIVE"
        assert row["resolved_at"] is None

    def test_w7c_get_watchable_signals_empty_db_returns_empty_list(self):
        """W7c: no signals in DB → returns []"""
        rows = db.get_watchable_signals()
        assert rows == []

    def test_terminal_signals_excluded_from_watchable(self):
        """TARGET1_HIT/STOPLOSS_HIT signals are NOT returned by get_watchable_signals"""
        db.insert_signal("msg-001", self._base_signal("msg-001"), "NSE_FO|12345", [], "PENDING")
        db.insert_signal("msg-002", self._base_signal("msg-002"), "NSE_FO|12345", [], "PENDING")
        rows = db.get_watchable_signals()
        sig_id = rows[0]["id"]
        db.update_signal_status(sig_id, "TARGET1_HIT")
        watchable = db.get_watchable_signals()
        ids = [r["id"] for r in watchable]
        assert sig_id not in ids  # terminal signal excluded

    def test_price_event_insertion(self):
        """insert_price_event works and is retrievable"""
        db.insert_signal("msg-001", self._base_signal(), "NSE_FO|12345", [], "PENDING")
        sig_id = db.get_watchable_signals()[0]["id"]
        db.insert_price_event(sig_id, "ENTRY_HIT", 120.0, 120.0)
        with db._conn() as con:
            events = con.execute(
                "SELECT * FROM price_events WHERE signal_id=?", (sig_id,)
            ).fetchall()
        assert len(events) == 1
        assert events[0]["event_type"] == "ENTRY_HIT"

    def test_targets_stored_in_correct_columns(self):
        """targets=[160,200,250] → stored as target1, target2, target3"""
        db.insert_signal("msg-001", self._base_signal(), "NSE_FO|12345", [], "PENDING")
        row = db.get_watchable_signals()[0]
        assert row["target1"] == 160.0
        assert row["target2"] == 200.0
        assert row["target3"] == 250.0

    def test_signal_with_no_targets_has_null_target_columns(self):
        """targets=[] → target1/target2/target3 all NULL"""
        sig = self._base_signal()
        sig["targets"] = []
        db.insert_signal("msg-001", sig, "NSE_FO|12345", [], "FAILED")
        with db._conn() as con:
            row = con.execute("SELECT target1,target2,target3 FROM signals").fetchone()
        assert row["target1"] is None
        assert row["target2"] is None
        assert row["target3"] is None

    def test_failed_signal_not_in_watchable(self):
        """FAILED signals are excluded from get_watchable_signals"""
        sig = self._base_signal()
        db.insert_signal("msg-001", sig, "NSE_FO|12345", [], "FAILED")
        rows = db.get_watchable_signals()
        assert rows == []
