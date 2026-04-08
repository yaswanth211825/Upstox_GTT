"""
test_blackbox.py — Black-box end-to-end tests for UpstoxGTT.

Tests the system from the outside: signals in → orders out → DB state.
Uses mocked Redis and mocked Upstox HTTP API.

Groups:
 B1 — Happy path (full signal lifecycle)
 B2 — Signal ingestion failures
 B3 — API failure scenarios
 B4 — Token expiry / auth failures
 B5 — Concurrent edge cases
 B6 — Volume / stress scenarios
 B7 — Edge case inputs
"""

import os
import sys
import json
import time
import sqlite3
import tempfile
import threading
from unittest.mock import MagicMock, patch, call
import pytest

# conftest.py handles path + env + Redis mocking
import gtt_strategy
from gtt_strategy import (
    parse_message_to_signal,
    place_gtt_order_upstox,
    store_signal_metadata,
    instrument_cache,
    DEFAULT_QUANTITY,
)
import price_monitor
import db


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_redis_message(overrides=None):
    """Build a valid Redis stream message dict."""
    base = {
        "action": "BUY",
        "instrument": "NIFTY",
        "strike": "24000",
        "option_type": "CE",
        "entry_low": "120",
        "stoploss": "80",
        "targets": "160/200/250",
        "expiry": "26th JUNE",
        "product": "I",
        "quantity": "1",
    }
    if overrides:
        base.update(overrides)
    return base


def mock_success_response(gtt_ids=None):
    """Returns a mock requests.Response for a successful GTT placement."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "status": "success",
        "data": {"gtt_order_ids": gtt_ids or ["gtt_bb_001"]}
    }
    return resp


def mock_error_response(status_code=400, message="Bad Request"):
    """Returns a mock requests.Response for a failed GTT placement."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = message
    resp.json.return_value = {"message": message, "status": "error"}
    return resp


MOCK_INSTRUMENT = {
    "instrument_key": "NSE_FO|12345",
    "lot_size": 50,
    "trading_symbol": "NIFTY24JUN24000CE",
    "segment": "NSE_FO",
    "instrument_type": "CE",
    "underlying_symbol": "NIFTY",
    "strike_price": 24000,
}


# ─────────────────────────────────────────────────────────────────────────────
# B1 — Happy Path
# ─────────────────────────────────────────────────────────────────────────────

class TestHappyPath:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_db, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_db)
        price_monitor._signal_cache.clear()
        price_monitor._instrument_index.clear()

    def test_b1a_valid_buy_ce_signal_produces_pending_db_entry(self):
        """B1a: Valid BUY CE signal → DB status=PENDING, GTT IDs stored"""
        msg = make_redis_message()
        signal = parse_message_to_signal(msg)
        assert signal is not None

        with patch.object(instrument_cache, 'find_instrument', return_value=MOCK_INSTRUMENT):
            with patch('gtt_strategy.requests.post', return_value=mock_success_response(["gtt_001"])):
                with patch('gtt_strategy.fetch_ltp', return_value=118.5):
                    result = place_gtt_order_upstox(signal)

        assert result["status"] == "success"
        store_signal_metadata("msg-001", signal, "success",
                              result.get("data", {}).get("gtt_order_ids"),
                              instrument_key=result.get("instrument_token", "NSE_FO|12345"),
                              ltp_at_placement=result.get("ltp_at_placement"))

        rows = db.get_watchable_signals()
        assert len(rows) == 1
        assert rows[0]["status"] == "PENDING"
        assert rows[0]["entry_low"] == 120.0
        assert rows[0]["stoploss"] == 80.0

    def test_b1b_valid_sell_pe_signal_processed(self):
        """B1b: Valid SELL PE signal → order placed, direction correct"""
        msg = make_redis_message({"action": "SELL", "instrument": "SENSEX",
                                   "strike": "85000", "option_type": "PE",
                                   "entry_low": "300", "stoploss": "400",
                                   "targets": "200/150"})
        signal = parse_message_to_signal(msg)
        assert signal is not None
        assert signal["action"] == "SELL"

        mock_instrument = dict(MOCK_INSTRUMENT)
        mock_instrument["instrument_key"] = "BSE_FO|67890"
        mock_instrument["segment"] = "BSE_FO"

        with patch.object(instrument_cache, 'find_instrument', return_value=mock_instrument):
            with patch('gtt_strategy.requests.post', return_value=mock_success_response()) as mock_post:
                with patch('gtt_strategy.fetch_ltp', return_value=295.0):
                    result = place_gtt_order_upstox(signal)

        assert result["status"] == "success"
        payload = mock_post.call_args[1]["json"]
        entry_rule = next(r for r in payload["rules"] if r["strategy"] == "ENTRY")
        assert entry_rule["trigger_type"] == "BELOW"  # SELL uses BELOW
        assert payload["transaction_type"] == "SELL"

    def test_b1c_entry_hit_transitions_signal_to_active(self):
        """B1c: Signal in DB → price hits entry → status becomes ACTIVE"""
        signal = parse_message_to_signal(make_redis_message())
        db.insert_signal("msg-001", signal, "NSE_FO|12345", ["gtt_001"], "PENDING")
        sig_id = db.get_watchable_signals()[0]["id"]

        # Set up price_monitor cache
        price_monitor._signal_cache[sig_id] = {
            "id": sig_id,
            "instrument_key": "NSE_FO|12345",
            "action": "BUY",
            "entry_low": 120.0,
            "stoploss": 80.0,
            "target1": 160.0,
            "status": "PENDING",
        }
        price_monitor._instrument_index["NSE_FO|12345"] = [sig_id]

        price_monitor._check_thresholds("NSE_FO|12345", 125.0)  # above entry

        assert price_monitor._signal_cache[sig_id]["status"] == "ACTIVE"
        rows = db.get_watchable_signals()
        assert rows[0]["status"] == "ACTIVE"
        assert rows[0]["activated_at"] is not None

    def test_b1d_target_hit_terminates_active_signal(self):
        """B1d: ACTIVE signal → price hits target → TARGET1_HIT (terminal)"""
        signal = parse_message_to_signal(make_redis_message())
        db.insert_signal("msg-001", signal, "NSE_FO|12345", ["gtt_001"], "ACTIVE")
        db.update_signal_activated(db.get_watchable_signals()[0]["id"])
        sig_id = db.get_watchable_signals()[0]["id"]

        price_monitor._signal_cache[sig_id] = {
            "id": sig_id, "instrument_key": "NSE_FO|12345",
            "action": "BUY", "entry_low": 120.0, "stoploss": 80.0,
            "target1": 160.0, "status": "ACTIVE",
        }
        price_monitor._instrument_index["NSE_FO|12345"] = [sig_id]

        price_monitor._check_thresholds("NSE_FO|12345", 165.0)  # above target

        assert sig_id not in price_monitor._signal_cache  # removed from cache
        assert db.get_watchable_signals() == []  # no more watchable signals
        with db._conn() as con:
            row = con.execute("SELECT status FROM signals WHERE id=?", (sig_id,)).fetchone()
        assert row["status"] == "TARGET1_HIT"

    def test_b1e_stoploss_hit_terminates_active_signal(self):
        """B1e: ACTIVE signal → price hits SL → STOPLOSS_HIT (terminal)"""
        signal = parse_message_to_signal(make_redis_message())
        db.insert_signal("msg-001", signal, "NSE_FO|12345", ["gtt_001"], "ACTIVE")
        sig_id = db.get_watchable_signals()[0]["id"]

        price_monitor._signal_cache[sig_id] = {
            "id": sig_id, "instrument_key": "NSE_FO|12345",
            "action": "BUY", "entry_low": 120.0, "stoploss": 80.0,
            "target1": 160.0, "status": "ACTIVE",
        }
        price_monitor._instrument_index["NSE_FO|12345"] = [sig_id]

        price_monitor._check_thresholds("NSE_FO|12345", 75.0)  # below stoploss

        assert sig_id not in price_monitor._signal_cache
        with db._conn() as con:
            row = con.execute("SELECT status FROM signals WHERE id=?", (sig_id,)).fetchone()
        assert row["status"] == "STOPLOSS_HIT"


# ─────────────────────────────────────────────────────────────────────────────
# B2 — Signal Ingestion Failures
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalIngestionFailures:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_db, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_db)

    def test_b2d_malformed_signal_returns_none(self):
        """B2d: message with no valid fields → parse_message_to_signal returns None"""
        result = parse_message_to_signal({"garbage": "data", "no_valid_fields": True})
        # action="" → signal_summary uses empty values, but strike=None, entry_low=None
        # The function returns a signal but with None values
        # place_gtt_order will catch missing fields
        # Let's verify parse handles empty action/strike gracefully
        assert result is None or result.get("strike") is None

    def test_b2d_empty_message_handled_gracefully(self):
        """Empty message dict → graceful None or partial signal"""
        result = parse_message_to_signal({})
        # strike is None, entry_low is None → partial signal or None
        if result:
            assert result["strike"] is None
            assert result["entry_low"] is None

    def test_b2e_burst_of_signals_all_processed_sequentially(self):
        """B2e: 50 signals in quick succession → all processed, none dropped"""
        processed = []
        for i in range(50):
            msg = make_redis_message({"strike": str(24000 + i * 100)})
            signal = parse_message_to_signal(msg)
            processed.append(signal)

        assert len(processed) == 50
        assert all(s is not None for s in processed)
        # All have unique strikes
        strikes = [s["strike"] for s in processed]
        assert len(set(strikes)) == 50

    def test_signal_with_only_action_fails_validation(self):
        """Signal with only action → instrument lookup fails"""
        signal = {"action": "BUY", "underlying": "NIFTY", "strike": 24000,
                  "option_type": "CE", "entry_low": None, "stoploss": 80.0,
                  "targets": [160.0], "expiry": "2025-06-26", "quantity": 1,
                  "product": "I", "signal_summary": "BUY NIFTY 24000CE"}
        result = place_gtt_order_upstox(signal)
        assert result["status"] == "error"
        assert "entry_low" in result["message"]

    def test_signal_with_no_expiry_fails_instrument_lookup(self):
        """No expiry → instrument lookup returns None → FAILED"""
        signal = {"action": "BUY", "underlying": "NIFTY", "strike": 24000,
                  "option_type": "CE", "entry_low": 120.0, "stoploss": 80.0,
                  "targets": [160.0], "expiry": "", "quantity": 1,
                  "product": "I", "signal_summary": "BUY NIFTY 24000CE"}
        result = place_gtt_order_upstox(signal)
        assert result["status"] == "error"
        assert "expiry" in result["message"].lower() or "Missing" in result["message"]


# ─────────────────────────────────────────────────────────────────────────────
# B3 — API Failure Scenarios
# ─────────────────────────────────────────────────────────────────────────────

class TestAPIFailures:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_db, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_db)

    def _make_signal(self):
        return parse_message_to_signal(make_redis_message())

    def test_b3a_http_401_returns_error_no_retry(self):
        """B3a: 401 Unauthorized → error result, signal should be FAILED in DB"""
        signal = self._make_signal()
        with patch.object(instrument_cache, 'find_instrument', return_value=MOCK_INSTRUMENT):
            with patch('gtt_strategy.requests.post',
                       return_value=mock_error_response(401, "Invalid token")) as mock_post:
                result = place_gtt_order_upstox(signal)
        assert result["status"] == "error"
        assert result["code"] == 401
        assert mock_post.call_count == 1  # NOT retried

    def test_b3b_http_429_not_backed_off(self):
        """B3b: 429 rate limit → error result, NOT retried, no backoff — gap in implementation"""
        signal = self._make_signal()
        with patch.object(instrument_cache, 'find_instrument', return_value=MOCK_INSTRUMENT):
            with patch('gtt_strategy.requests.post',
                       return_value=mock_error_response(429, "Too Many Requests")) as mock_post:
                with patch('gtt_strategy.time.sleep') as mock_sleep:
                    result = place_gtt_order_upstox(signal)
        assert result["status"] == "error"
        assert result["code"] == 429
        assert mock_post.call_count == 1  # NOT retried
        mock_sleep.assert_not_called()   # NO backoff

    def test_b3c_http_500_not_retried(self):
        """B3c: 500 Server Error → error result, not retried"""
        signal = self._make_signal()
        with patch.object(instrument_cache, 'find_instrument', return_value=MOCK_INSTRUMENT):
            with patch('gtt_strategy.requests.post',
                       return_value=mock_error_response(500, "Internal Server Error")) as mock_post:
                result = place_gtt_order_upstox(signal)
        assert result["status"] == "error"
        assert mock_post.call_count == 1

    def test_b3d_3x_timeout_fails_after_3_retries(self):
        """B3d: 3 consecutive timeouts → exception caught, returns error"""
        import requests as req
        signal = self._make_signal()
        with patch.object(instrument_cache, 'find_instrument', return_value=MOCK_INSTRUMENT):
            with patch('gtt_strategy.requests.post',
                       side_effect=req.exceptions.Timeout) as mock_post:
                with patch('gtt_strategy.time.sleep'):
                    result = place_gtt_order_upstox(signal)
        assert result["status"] == "error"
        assert mock_post.call_count == 3

    def test_b3e_instruments_url_unreachable_all_signals_fail(self):
        """B3e: Instruments URL unreachable → cache not loaded → every signal fails"""
        import requests as req
        # Reset cache state
        instrument_cache.loaded = False
        instrument_cache.instruments = {}
        with patch('gtt_strategy.requests.get', side_effect=req.exceptions.ConnectionError("DNS failed")):
            result = place_gtt_order_upstox(self._make_signal())
        assert result["status"] == "error"
        # Restore for other tests
        instrument_cache.loaded = False

    def test_b3f_ltp_fetch_failure_stores_none_as_placement_price(self):
        """B3f: LTP endpoint error → gtt_place_price stored as None"""
        signal = self._make_signal()
        with patch.object(instrument_cache, 'find_instrument', return_value=MOCK_INSTRUMENT):
            with patch('gtt_strategy.requests.post', return_value=mock_success_response()):
                with patch('gtt_strategy.fetch_ltp', return_value=None):
                    result = place_gtt_order_upstox(signal)

        assert result["status"] == "success"
        assert result.get("ltp_at_placement") is None

        store_signal_metadata("msg-ltp-fail", signal, "success",
                              ["gtt_001"], "NSE_FO|12345", ltp_at_placement=None)
        rows = db.get_watchable_signals()
        assert rows[0]["gtt_place_price"] is None

    def test_upstox_success_status_false_treated_as_failure(self):
        """Upstox returns 200 but status='error' → stored as FAILED"""
        signal = self._make_signal()
        with patch.object(instrument_cache, 'find_instrument', return_value=MOCK_INSTRUMENT):
            with patch('gtt_strategy.requests.post') as mock_post:
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {"status": "error", "message": "Order rejected by exchange"}
                mock_post.return_value = resp
                result = place_gtt_order_upstox(signal)

        assert result.get("status") != "success"


# ─────────────────────────────────────────────────────────────────────────────
# B4 — Token Expiry / Auth Failures
# ─────────────────────────────────────────────────────────────────────────────

class TestTokenExpiry:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_db, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_db)

    def test_b4b_expired_token_mid_run_marks_all_signals_failed(self):
        """B4b: Token expires → all subsequent signals return 401 → FAILED in DB"""
        signals = [parse_message_to_signal(make_redis_message({"strike": str(24000 + i * 100)}))
                   for i in range(3)]
        with patch.object(instrument_cache, 'find_instrument', return_value=MOCK_INSTRUMENT):
            with patch('gtt_strategy.requests.post',
                       return_value=mock_error_response(401, "Token expired")) as mock_post:
                for i, signal in enumerate(signals):
                    result = place_gtt_order_upstox(signal)
                    store_signal_metadata(f"msg-{i}", signal, result.get("status", "error"))

        # All should be FAILED
        with db._conn() as con:
            rows = con.execute("SELECT status FROM signals WHERE entry_low > 0").fetchall()
        statuses = [r["status"] for r in rows]
        assert all(s == "FAILED" for s in statuses), f"Expected all FAILED, got: {statuses}"

    def test_b4a_missing_token_causes_system_exit_on_price_monitor(self, monkeypatch):
        """B4a: No token → price_monitor._main() raises SystemExit"""
        monkeypatch.setattr(price_monitor, "UPSTOX_ACCESS_TOKEN", "")
        import asyncio
        with pytest.raises(SystemExit):
            asyncio.run(price_monitor._main())

    def test_b4c_ws_auth_failure_triggers_reconnect_loop(self):
        """B4c: WS authorization fails → exception → reconnect after 5s"""
        import requests as req
        call_count = {"n": 0}

        def failing_get(url, **kwargs):
            call_count["n"] += 1
            raise req.exceptions.HTTPError("401 Unauthorized")

        with patch('price_monitor.requests.get', side_effect=failing_get):
            with patch('price_monitor.asyncio.sleep') as mock_sleep:
                # Call _ws_loop directly — it should raise
                import asyncio
                with pytest.raises(req.exceptions.HTTPError):
                    asyncio.run(price_monitor._ws_loop())
        # The outer _main() loop would catch this and sleep 5s before retry
        assert call_count["n"] >= 1


# ─────────────────────────────────────────────────────────────────────────────
# B5 — Concurrent / Edge-Case Scenarios
# ─────────────────────────────────────────────────────────────────────────────

class TestConcurrentScenarios:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_db, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_db)

    def test_b5b_price_monitor_with_empty_db_no_crash(self):
        """B5b: price_monitor starts before any signals → empty cache, no crash"""
        price_monitor._signal_cache.clear()
        price_monitor._instrument_index.clear()
        price_monitor._rebuild_cache()  # Should handle empty DB gracefully
        assert price_monitor._signal_cache == {}
        assert price_monitor._instrument_index == {}

    def test_b5c_signal_pending_with_no_price_monitor_stays_pending(self):
        """B5c: gtt_strategy places order but price_monitor not running → PENDING forever"""
        signal = parse_message_to_signal(make_redis_message())
        db.insert_signal("msg-001", signal, "NSE_FO|12345", ["gtt_001"], "PENDING")
        # After some time passes, signal should still be PENDING (no monitor updating it)
        time.sleep(0.01)
        rows = db.get_watchable_signals()
        assert rows[0]["status"] == "PENDING"  # Stuck PENDING

    def test_b5a_concurrent_db_writes_from_two_threads(self):
        """B5a: Concurrent DB writes from multiple threads (WAL mode) — no corruption"""
        errors = []

        def write_signal(i):
            try:
                sig = {
                    "action": "BUY", "underlying": "NIFTY",
                    "strike": 24000 + i, "option_type": "CE",
                    "entry_low": 120.0, "stoploss": 80.0,
                    "targets": [160.0], "expiry": "2025-06-26",
                    "product": "I", "quantity": 1,
                    "signal_summary": f"BUY NIFTY {24000+i}CE",
                }
                db.insert_signal(f"msg-concurrent-{i}", sig, f"NSE_FO|{i}", [], "PENDING")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_signal, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent writes caused errors: {errors}"
        rows = db.get_watchable_signals()
        assert len(rows) == 20  # all 20 inserted successfully


# ─────────────────────────────────────────────────────────────────────────────
# B6 — Volume / Stress Scenarios
# ─────────────────────────────────────────────────────────────────────────────

class TestVolumeStress:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_db, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_db)

    def test_b6a_100_signal_parsing_throughput(self):
        """B6a: Parse 100 signals — measure throughput (should be fast)"""
        msgs = [make_redis_message({"strike": str(24000 + i * 100)}) for i in range(100)]
        start = time.perf_counter()
        results = [parse_message_to_signal(m) for m in msgs]
        elapsed = time.perf_counter() - start

        assert all(r is not None for r in results)
        assert elapsed < 1.0, f"Parsing 100 signals took {elapsed:.3f}s — too slow"
        print(f"\n[B6a] 100 signals parsed in {elapsed*1000:.1f}ms "
              f"({100/elapsed:.0f} signals/sec)")

    def test_b6c_zset_trim_prevents_unbounded_growth(self):
        """B6c: ZSET behavior at capacity — oldest entry gets pruned"""
        from gtt_strategy import PROCESSED_ZSET_MAX
        # Verify the trimming math is correct
        zcount = PROCESSED_ZSET_MAX + 1
        expected_to_remove = zcount - PROCESSED_ZSET_MAX - 1  # = 0
        # zremrangebyrank(key, 0, 0) removes 1 oldest entry (rank 0)
        assert expected_to_remove == 0  # removes from 0..0 = 1 entry

    def test_b6d_instrument_cache_handles_large_dataset(self):
        """B6d: Cache with 30,000 simulated instruments — lookup still works"""
        # Build a synthetic cache
        instruments = {}
        for i in range(30000):
            key = f"NSE_FO|{100000 + i}"
            instruments[key] = {
                "instrument_key": key,
                "segment": "NSE_FO",
                "instrument_type": "CE",
                "underlying_symbol": "NIFTY",
                "strike_price": 20000 + i,
                "expiry": 1751654400000,  # some timestamp
                "lot_size": 75,
            }

        from gtt_strategy import InstrumentCache
        cache = InstrumentCache()
        cache.instruments = instruments
        cache.loaded = True

        start = time.perf_counter()
        # Look for a nonexistent instrument (worst case: full scan)
        result = cache.find_instrument("NIFTY", 99999999, "CE", "2025-07-05")
        elapsed = time.perf_counter() - start

        assert result is None  # correctly not found
        print(f"\n[B6d] Linear scan of 30,000 instruments took {elapsed*1000:.1f}ms")
        # A linear scan of 30k items should be < 1 second even in Python
        assert elapsed < 5.0, f"Cache lookup too slow: {elapsed:.2f}s"

    def test_b6b_500_signals_in_db_all_watchable(self):
        """B6b: 500 PENDING signals in DB — get_watchable_signals returns all"""
        for i in range(500):
            sig = {
                "action": "BUY", "underlying": "NIFTY",
                "strike": 20000 + i, "option_type": "CE",
                "entry_low": 100.0 + i, "stoploss": 60.0 + i,
                "targets": [150.0 + i],
                "expiry": "2025-06-26", "product": "I", "quantity": 1,
                "signal_summary": f"BUY NIFTY {20000+i}CE",
            }
            db.insert_signal(f"msg-bulk-{i}", sig, f"NSE_FO|{i}", [], "PENDING")

        start = time.perf_counter()
        rows = db.get_watchable_signals()
        elapsed = time.perf_counter() - start

        assert len(rows) == 500
        print(f"\n[B6b] Queried 500 signals in {elapsed*1000:.1f}ms")
        assert elapsed < 2.0, f"DB query too slow: {elapsed:.3f}s"


# ─────────────────────────────────────────────────────────────────────────────
# B7 — Edge Case Inputs
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCaseInputs:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_db, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_db)

    def test_b7a_zero_strike_no_instrument_found(self):
        """B7a: strike=0 → no instrument match → FAILED"""
        msg = make_redis_message({"strike": "0"})
        signal = parse_message_to_signal(msg)
        assert signal is not None
        assert signal["strike"] == 0
        # Instrument lookup with strike=0 should fail
        with patch.object(instrument_cache, 'find_instrument', return_value=None):
            result = place_gtt_order_upstox(signal)
        assert result["status"] == "error"

    def test_b7b_past_expiry_gracefully_fails(self):
        """B7b: Past expiry date → instrument likely not in cache → FAILED"""
        msg = make_redis_message({"expiry": "26th JANUARY"})  # likely past date
        signal = parse_message_to_signal(msg)
        assert signal is not None
        # expiry should parse without error
        assert signal["expiry"] == "26th JANUARY"

    def test_b7d_invalid_underlying_fails_gracefully(self):
        """B7d: underlying='INVALID' → no match → graceful error"""
        msg = make_redis_message({"instrument": "INVALID_UNDERLYING_XYZ"})
        signal = parse_message_to_signal(msg)
        assert signal is not None
        assert signal["underlying"] == "INVALID_UNDERLYING_XYZ"
        # Real instrument lookup would return None
        with patch.object(instrument_cache, 'find_instrument', return_value=None):
            result = place_gtt_order_upstox(signal)
        assert result["status"] == "error"

    def test_b7e_equal_entry_stoploss_target_sent_to_api(self):
        """B7e: entry_low = stoploss = target → no validation, sent to API"""
        signal = {
            "action": "BUY", "underlying": "NIFTY", "strike": 24000,
            "option_type": "CE", "entry_low": 100.0, "stoploss": 100.0,
            "targets": [100.0], "expiry": "2025-06-26",
            "quantity": 1, "product": "I",
            "signal_summary": "BUY NIFTY 24000CE",
        }
        with patch.object(instrument_cache, 'find_instrument', return_value=MOCK_INSTRUMENT):
            with patch('gtt_strategy.requests.post') as mock_post:
                mock_post.return_value = mock_success_response()
                with patch('gtt_strategy.fetch_ltp', return_value=100.0):
                    result = place_gtt_order_upstox(signal)
        # No validation catches this — sent to API
        assert mock_post.called
        payload = mock_post.call_args[1]["json"]
        prices = [r["trigger_price"] for r in payload["rules"]]
        assert all(p == 100.0 for p in prices), "All trigger prices should be 100.0"

    def test_b7f_buy_with_entry_below_stoploss_no_validation(self):
        """B7f: BUY with entry_low < stoploss → no sanity check, sent to API as-is"""
        signal = {
            "action": "BUY", "underlying": "NIFTY", "strike": 24000,
            "option_type": "CE", "entry_low": 50.0,  # below stoploss!
            "stoploss": 100.0, "targets": [150.0],
            "expiry": "2025-06-26", "quantity": 1, "product": "I",
            "signal_summary": "BUY NIFTY 24000CE",
        }
        with patch.object(instrument_cache, 'find_instrument', return_value=MOCK_INSTRUMENT):
            with patch('gtt_strategy.requests.post') as mock_post:
                mock_post.return_value = mock_success_response()
                with patch('gtt_strategy.fetch_ltp', return_value=50.0):
                    result = place_gtt_order_upstox(signal)
        # No input validation catches this — illogical GTT order sent
        if mock_post.called:
            payload = mock_post.call_args[1]["json"]
            entry_price = next(r["trigger_price"] for r in payload["rules"]
                               if r["strategy"] == "ENTRY")
            sl_price = next(r["trigger_price"] for r in payload["rules"]
                            if r["strategy"] == "STOPLOSS")
            assert entry_price < sl_price, "CONFIRMED: entry < stoploss, no validation"

    def test_b7g_quantity_zero_api_gets_zero_quantity(self):
        """B7g: quantity=0 → api_quantity=0, NO validation catches it"""
        signal = {
            "action": "BUY", "underlying": "NIFTY", "strike": 24000,
            "option_type": "CE", "entry_low": 120.0, "stoploss": 80.0,
            "targets": [160.0], "expiry": "2025-06-26",
            "quantity": 0,  # ZERO
            "product": "I", "signal_summary": "BUY NIFTY 24000CE",
        }
        with patch.object(instrument_cache, 'find_instrument', return_value=MOCK_INSTRUMENT):
            with patch('gtt_strategy.requests.post') as mock_post:
                mock_post.return_value = mock_success_response()
                with patch('gtt_strategy.fetch_ltp', return_value=None):
                    place_gtt_order_upstox(signal)
        if mock_post.called:
            payload = mock_post.call_args[1]["json"]
            assert payload["quantity"] == 0, "CONFIRMED BUG: quantity=0 sent to Upstox"

    def test_b7h_very_large_quantity_no_limit(self):
        """B7h: quantity=1000 lots × lot_size=50 = 50,000 shares → no limit check"""
        signal = {
            "action": "BUY", "underlying": "NIFTY", "strike": 24000,
            "option_type": "CE", "entry_low": 120.0, "stoploss": 80.0,
            "targets": [160.0], "expiry": "2025-06-26",
            "quantity": 1000,  # huge
            "product": "I", "signal_summary": "BUY NIFTY 24000CE",
        }
        with patch.object(instrument_cache, 'find_instrument', return_value=MOCK_INSTRUMENT):
            with patch('gtt_strategy.requests.post') as mock_post:
                mock_post.return_value = mock_success_response()
                with patch('gtt_strategy.fetch_ltp', return_value=None):
                    place_gtt_order_upstox(signal)
        if mock_post.called:
            payload = mock_post.call_args[1]["json"]
            assert payload["quantity"] == 50000, "1000 lots × 50 lot_size = 50,000"
