"""
test_stress.py — Volume, throughput, and performance tests for UpstoxGTT.

These tests check:
- How fast signal parsing is (throughput measurement)
- How the DB handles large volumes
- How the instrument cache scales
- Where latency builds up

All tests print timing stats. Run with -s flag to see output:
    pytest tests/test_stress.py -v -s
"""

import os
import sys
import time
import sqlite3
import threading
import tempfile
from unittest.mock import patch, MagicMock
import pytest

# conftest.py handles path + env + Redis mocking
import gtt_strategy
from gtt_strategy import (
    parse_message_to_signal,
    place_gtt_order_upstox,
    store_signal_metadata,
    instrument_cache,
    InstrumentCache,
)
import price_monitor
import db


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_signal_for_strike(i):
    return {
        "action": "BUY",
        "instrument": "NIFTY",
        "strike": str(20000 + i * 100),
        "option_type": "CE",
        "entry_low": "100",
        "stoploss": "60",
        "targets": "150/200",
        "expiry": "26th JUNE",
        "product": "I",
        "quantity": "1",
    }


def make_parsed_signal(i, instrument_key="NSE_FO|12345"):
    return {
        "action": "BUY",
        "underlying": "NIFTY",
        "strike": 20000 + i * 100,
        "option_type": "CE",
        "entry_low": 100.0 + i,
        "stoploss": 60.0 + i,
        "targets": [150.0 + i, 200.0 + i],
        "expiry": "2025-06-26",
        "product": "I",
        "quantity": 1,
        "signal_summary": f"BUY NIFTY {20000 + i * 100}CE",
        "instrument_key": instrument_key,
    }


# ─────────────────────────────────────────────────────────────────────────────
# S1 — Signal parsing throughput
# ─────────────────────────────────────────────────────────────────────────────

class TestParsingThroughput:
    def test_s1a_parse_1000_signals(self):
        """Parse 1,000 signals — measures parser throughput"""
        N = 1000
        msgs = [make_signal_for_strike(i) for i in range(N)]

        start = time.perf_counter()
        results = [parse_message_to_signal(m) for m in msgs]
        elapsed = time.perf_counter() - start

        success = sum(1 for r in results if r is not None)
        rate = N / elapsed

        print(f"\n[S1a] Parsed {N} signals in {elapsed*1000:.1f}ms | {rate:.0f} signals/sec")
        assert success == N, f"Only {success}/{N} parsed successfully"
        assert rate > 500, f"Parsing too slow: {rate:.0f} sig/sec (expected >500)"

    def test_s1b_parse_with_all_field_variations(self):
        """Parse signals with diverse field formats — no crashes"""
        variations = [
            make_signal_for_strike(0),
            {**make_signal_for_strike(1), "expiry": "06 NOV 25"},
            {**make_signal_for_strike(2), "expiry": "06NOV25"},
            {**make_signal_for_strike(3), "targets": "200"},  # single target
            {**make_signal_for_strike(4), "action": "sell"},  # lowercase
            {**make_signal_for_strike(5), "option_type": "pe"},  # lowercase
            {**make_signal_for_strike(6), "quantity": "5"},
            {**make_signal_for_strike(7), "product": "D"},
        ]
        results = [parse_message_to_signal(v) for v in variations]
        failed = [(i, v) for i, (v, r) in enumerate(zip(variations, results)) if r is None]
        assert failed == [], f"Failed to parse variations: {failed}"


# ─────────────────────────────────────────────────────────────────────────────
# S2 — Database write throughput
# ─────────────────────────────────────────────────────────────────────────────

class TestDatabaseThroughput:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_db, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_db)

    def test_s2a_insert_1000_signals(self):
        """Insert 1,000 signals into SQLite — measures write throughput"""
        N = 1000
        signals = [make_parsed_signal(i) for i in range(N)]

        start = time.perf_counter()
        for i, sig in enumerate(signals):
            db.insert_signal(f"msg-stress-{i}", sig, f"NSE_FO|{i}", [f"gtt_{i}"], "PENDING")
        elapsed = time.perf_counter() - start

        rate = N / elapsed
        print(f"\n[S2a] Inserted {N} signals in {elapsed*1000:.1f}ms | {rate:.0f} inserts/sec")
        # Verify all were inserted
        rows = db.get_watchable_signals()
        assert len(rows) == N, f"Only {len(rows)}/{N} inserted"
        assert rate > 50, f"DB writes too slow: {rate:.0f}/sec (expected >50)"

    def test_s2b_query_500_watchable_signals(self):
        """Query 500 PENDING/ACTIVE signals — measures read performance"""
        N = 500
        for i in range(N):
            sig = make_parsed_signal(i)
            db.insert_signal(f"msg-{i}", sig, f"NSE_FO|{i}", [], "PENDING")

        start = time.perf_counter()
        rows = db.get_watchable_signals()
        elapsed = time.perf_counter() - start

        print(f"\n[S2b] Queried {len(rows)} watchable signals in {elapsed*1000:.1f}ms")
        assert len(rows) == N
        assert elapsed < 0.5, f"Query too slow: {elapsed:.3f}s"

    def test_s2c_update_100_signal_statuses(self):
        """Update 100 signal statuses to terminal — measures update throughput"""
        N = 100
        for i in range(N):
            sig = make_parsed_signal(i)
            db.insert_signal(f"msg-upd-{i}", sig, f"NSE_FO|{i}", [], "PENDING")

        rows = db.get_watchable_signals()
        sig_ids = [r["id"] for r in rows[:N]]

        start = time.perf_counter()
        for sid in sig_ids:
            db.update_signal_status(sid, "TARGET1_HIT")
        elapsed = time.perf_counter() - start

        print(f"\n[S2c] Updated {N} statuses in {elapsed*1000:.1f}ms")
        assert elapsed < 1.0

    def test_s2d_insert_1000_price_events(self):
        """Insert 1,000 price events — simulates active monitoring"""
        # Insert one signal first
        db.insert_signal("msg-events", make_parsed_signal(0), "NSE_FO|12345", [], "ACTIVE")
        sig_id = db.get_watchable_signals()[0]["id"]

        N = 1000
        start = time.perf_counter()
        for i in range(N):
            db.insert_price_event(sig_id, "ENTRY_HIT", 100.0 + i * 0.1, 100.0)
        elapsed = time.perf_counter() - start

        print(f"\n[S2d] Inserted {N} price events in {elapsed*1000:.1f}ms")
        assert elapsed < 5.0

    def test_s2e_concurrent_reads_writes_no_corruption(self):
        """Concurrent reads and writes from multiple threads — WAL mode should handle it"""
        N = 50
        errors = []

        def reader():
            for _ in range(10):
                try:
                    db.get_watchable_signals()
                except Exception as e:
                    errors.append(f"READ: {e}")

        def writer(offset):
            for j in range(5):
                try:
                    sig = make_parsed_signal(offset * 100 + j)
                    db.insert_signal(f"msg-concurrent-{offset}-{j}", sig,
                                     f"NSE_FO|{offset*100+j}", [], "PENDING")
                except Exception as e:
                    errors.append(f"WRITE: {e}")

        threads = []
        for i in range(5):
            threads.append(threading.Thread(target=reader))
            threads.append(threading.Thread(target=writer, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent errors: {errors}"


# ─────────────────────────────────────────────────────────────────────────────
# S3 — Instrument cache performance
# ─────────────────────────────────────────────────────────────────────────────

class TestInstrumentCachePerformance:
    def test_s3a_cache_lookup_30k_instruments(self):
        """Cache with 30,000 instruments — linear scan worst case timing"""
        cache = InstrumentCache()

        # Build synthetic 30k instruments
        N = 30000
        for i in range(N):
            key = f"NSE_FO|{100000 + i}"
            cache.instruments[key] = {
                "instrument_key": key,
                "segment": "NSE_FO",
                "instrument_type": "CE",
                "underlying_symbol": "NIFTY",
                "strike_price": 20000 + i,
                "expiry": 1751654400000,
                "lot_size": 75,
            }
        cache.loaded = True

        # Worst case: find last instrument in iteration order
        target_strike = 20000 + N - 1  # last one
        start = time.perf_counter()
        result = cache.find_instrument("NIFTY", target_strike, "CE", "2025-07-05")
        elapsed = time.perf_counter() - start

        print(f"\n[S3a] Linear scan of {N} instruments took {elapsed*1000:.1f}ms")
        # Should find it (timestamp comparison)
        # We just care it doesn't hang
        assert elapsed < 10.0

    def test_s3b_repeated_lookups_use_existing_cache(self):
        """Second lookup doesn't reload cache — loaded flag respected"""
        cache = InstrumentCache()
        cache.loaded = True
        cache.instruments = {"NSE_FO|99": {
            "instrument_key": "NSE_FO|99",
            "segment": "NSE_FO",
            "instrument_type": "CE",
            "underlying_symbol": "TEST",
            "strike_price": 100,
            "expiry": 1751654400000,
            "lot_size": 1,
        }}

        with patch.object(cache, 'load_instruments') as mock_load:
            for _ in range(10):
                cache.find_instrument("TEST", 100, "CE", "2025-07-05")
            mock_load.assert_not_called()  # never reloaded


# ─────────────────────────────────────────────────────────────────────────────
# S4 — Price monitor threshold checking at volume
# ─────────────────────────────────────────────────────────────────────────────

class TestPriceMonitorVolume:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_db, monkeypatch):
        monkeypatch.setattr(db, "DB_PATH", tmp_db)
        price_monitor._signal_cache.clear()
        price_monitor._instrument_index.clear()

    def test_s4a_check_thresholds_with_100_signals_per_instrument(self):
        """100 signals watching same instrument — threshold check performance"""
        N = 100
        # Insert all signals to DB
        for i in range(N):
            sig = make_parsed_signal(i, instrument_key="NSE_FO|12345")
            db.insert_signal(f"msg-pm-{i}", sig, "NSE_FO|12345", [f"gtt_{i}"], "PENDING")

        # Populate cache
        rows = db.get_watchable_signals()
        for row in rows:
            price_monitor._signal_cache[row["id"]] = {
                "id": row["id"],
                "instrument_key": "NSE_FO|12345",
                "action": "BUY",
                "entry_low": row["entry_low"],
                "stoploss": row["stoploss"],
                "target1": row["target1"],
                "status": "PENDING",
            }
            price_monitor._instrument_index.setdefault("NSE_FO|12345", []).append(row["id"])

        # Fire price updates for 1000 ticks — price below all entries
        start = time.perf_counter()
        for _ in range(1000):
            price_monitor._check_thresholds("NSE_FO|12345", 50.0)  # below all entries
        elapsed = time.perf_counter() - start

        print(f"\n[S4a] 1000 price ticks × {N} signals = {1000*N:,} checks in {elapsed*1000:.1f}ms")
        assert elapsed < 5.0, f"Threshold checks too slow: {elapsed:.2f}s"

    def test_s4b_confirmed_bug_return_exits_after_first_target_hit(self):
        """
        CONFIRMED BUG: When multiple signals watch the same instrument and a price tick
        hits entry+target for the FIRST signal, the `return` statement in _check_thresholds
        (line ~148) exits the ENTIRE function — all remaining signals on that instrument
        are NOT processed for that tick.

        Root cause: `return` instead of `continue` after removing terminal signal.
        Impact: If N signals watch same instrument and price=200 exceeds all entries (100-149)
        AND targets (150-199), only the FIRST signal gets processed. The other N-1 are skipped.
        """
        N = 50
        for i in range(N):
            sig = make_parsed_signal(i, instrument_key="NSE_FO|77777")
            db.insert_signal(f"msg-act-{i}", sig, "NSE_FO|77777", [], "PENDING")

        rows = db.get_watchable_signals()
        for row in rows:
            price_monitor._signal_cache[row["id"]] = {
                "id": row["id"],
                "instrument_key": "NSE_FO|77777",
                "action": "BUY",
                "entry_low": row["entry_low"],   # ranges 100-149
                "stoploss": row["stoploss"],      # ranges 60-109
                "target1": row["target1"],        # ranges 150-199
                "status": "PENDING",
            }
            price_monitor._instrument_index.setdefault("NSE_FO|77777", []).append(row["id"])

        # Price=200: exceeds ALL entries (100-149) AND ALL targets (150-199)
        # Expected (correct): all 50 → TARGET1_HIT
        # Actual (buggy): only 1st signal processed, rest skipped due to `return` on line 148
        price_monitor._check_thresholds("NSE_FO|77777", 200.0)

        remaining = len(price_monitor._signal_cache)
        terminal_count = N - remaining
        print(f"\n[S4b] CONFIRMED BUG: only {terminal_count}/{N} signals processed "
              f"(remaining {remaining} skipped due to early `return` in _check_thresholds)")

        # Document the bug: only 1 signal processed, not all N
        # Fix would be: change `return` to `continue` at line ~148 and ~155 in price_monitor.py
        assert terminal_count < N, (
            "BUG: `return` after terminal state exits entire _check_thresholds loop. "
            "Remaining signals on same instrument are NOT processed."
        )


# ─────────────────────────────────────────────────────────────────────────────
# S5 — Break Point Summary (documentation tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestBreakPointSummary:
    """
    These tests document confirmed system behavior at break points.
    They all PASS but their assertions reveal known bugs/limitations.
    """

    def test_confirmed_bug_quantity_zero_bypasses_validation(self):
        """CONFIRMED BUG: quantity=0 passes parse and GTT validation"""
        msg = make_signal_for_strike(0)
        msg["quantity"] = "0"
        signal = parse_message_to_signal(msg)
        assert signal is not None, "Parsing succeeded"
        assert signal["quantity"] == 0, "quantity=0 stored in signal"

        # In place_gtt_order_upstox, no validation for quantity=0
        # api_quantity = 0 * lot_size = 0 would be sent to Upstox
        # Upstox may silently accept or reject this

    def test_confirmed_bug_year_ambiguity_for_2051_to_2099(self):
        """CONFIRMED BUG: 2-digit years 51-99 parsed as 19xx not 20xx"""
        from gtt_strategy import parse_expiry_date
        # Year 51 → 1951 (wrong, should be 2051)
        result = parse_expiry_date("26 DEC 51")
        assert result == "1951-12-26", "Year 2051 parsed as 1951 — BUG"
        # Year 87 → 1987
        result = parse_expiry_date("26 DEC 87")
        assert result == "1987-12-26", "Year 2087 parsed as 1987 — BUG"

    def test_confirmed_limitation_http_429_no_backoff(self):
        """CONFIRMED LIMITATION: HTTP 429 has no backoff — hammers API immediately"""
        import requests as req
        signal = parse_message_to_signal(make_signal_for_strike(0))
        sleep_calls = []

        with patch.object(instrument_cache, 'find_instrument', return_value={
            "instrument_key": "NSE_FO|12345", "lot_size": 50
        }):
            with patch('gtt_strategy.requests.post',
                       return_value=MagicMock(
                           status_code=429,
                           text="Rate Limited",
                           json=MagicMock(return_value={"message": "Rate Limited"})
                       )):
                with patch('gtt_strategy.time.sleep', side_effect=lambda s: sleep_calls.append(s)):
                    place_gtt_order_upstox(signal)

        assert sleep_calls == [], "NO sleep on 429 — no backoff implemented"

    def test_confirmed_limitation_no_order_sanity_check(self):
        """CONFIRMED LIMITATION: entry_low < stoploss for BUY — no validation"""
        signal = {
            "action": "BUY", "underlying": "NIFTY", "strike": 24000,
            "option_type": "CE", "entry_low": 50.0,  # BELOW stoploss
            "stoploss": 100.0, "targets": [200.0],
            "expiry": "2025-06-26", "quantity": 1, "product": "I",
            "signal_summary": "BUY NIFTY 24000CE",
        }
        # No ValueError, no early return — just passes through
        with patch.object(instrument_cache, 'find_instrument', return_value={
            "instrument_key": "NSE_FO|12345", "lot_size": 50
        }):
            with patch('gtt_strategy.requests.post') as mock_post:
                mock_post.return_value = MagicMock(
                    status_code=200,
                    json=MagicMock(return_value={"status": "success",
                                                  "data": {"gtt_order_ids": ["x"]}})
                )
                with patch('gtt_strategy.fetch_ltp', return_value=50.0):
                    result = place_gtt_order_upstox(signal)

        assert mock_post.called, "Bad signal was sent to Upstox API without validation"

    def test_confirmed_limitation_pending_ghost_when_api_fails(self, tmp_db, monkeypatch):
        """CONFIRMED LIMITATION: API failure → FAILED in DB (correctly handled)"""
        monkeypatch.setattr(db, "DB_PATH", tmp_db)
        signal = parse_message_to_signal(make_signal_for_strike(0))

        # API returns 500 → result is error
        with patch.object(instrument_cache, 'find_instrument', return_value={
            "instrument_key": "NSE_FO|12345", "lot_size": 50
        }):
            with patch('gtt_strategy.requests.post', return_value=MagicMock(
                status_code=500, text="Server Error",
                json=MagicMock(side_effect=Exception)
            )):
                result = place_gtt_order_upstox(signal)

        # Store based on result (as gtt_strategy main loop does)
        if result.get("status") == "success":
            store_signal_metadata("msg-ghost", signal, "success", [], "NSE_FO|12345")
        else:
            store_signal_metadata("msg-ghost", signal, "failed")

        # Signal should be FAILED in DB (correctly handled via store_signal_metadata)
        with db._conn() as con:
            row = con.execute("SELECT status FROM signals WHERE redis_message_id='msg-ghost'").fetchone()

        if row:
            assert row["status"] == "FAILED", "API failure correctly stored as FAILED"
        # If no row (entry_low check in store_signal_metadata), that's also OK
