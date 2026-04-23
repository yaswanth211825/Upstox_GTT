"""
conftest.py — Shared test fixtures for UpstoxGTT test suite.

Sets up:
- Environment variables (token, Redis, URLs)
- Mocked Redis client (prevents real connections)
- Temporary SQLite DB per test (clean isolation)
"""

import os
import sys
import tempfile
from unittest.mock import MagicMock

# ── Add project root to path so we can import gtt_strategy, db, price_monitor
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ── Set env vars BEFORE gtt_strategy is imported (module-level code reads these)
os.environ.setdefault("UPSTOX_ACCESS_TOKEN", "test_token_for_testing")
os.environ.setdefault("REDIS_HOST", "127.0.0.1")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("UPSTOX_BASE_URL", "http://localhost:19999")
os.environ.setdefault("DEFAULT_QUANTITY", "1")
os.environ.setdefault("REDIS_STREAM_NAME", "raw_trade_signals")
os.environ.setdefault("REDIS_START_FROM", "resume")
os.environ.setdefault("USE_POOLED_HTTP_POST", "false")

# ── Mock the redis module BEFORE gtt_strategy imports it
# gtt_strategy.py does redis.Redis(...).ping() at module level — must succeed
mock_redis_instance = MagicMock()
mock_redis_instance.ping.return_value = True
mock_redis_instance.zscore.return_value = None
mock_redis_instance.xread.return_value = []
mock_redis_instance.get.return_value = None
mock_redis_instance.zadd.return_value = 1
mock_redis_instance.set.return_value = True
mock_redis_instance.zcard.return_value = 0
mock_redis_instance.hset.return_value = 1
mock_redis_instance.expire.return_value = True

mock_redis_module = MagicMock()
mock_redis_module.Redis.return_value = mock_redis_instance
mock_redis_module.exceptions = MagicMock()
mock_redis_module.exceptions.Timeout = TimeoutError

sys.modules["redis"] = mock_redis_module

# ── Now safe to import project modules
import pytest
import db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fixture: fresh in-memory SQLite DB for each test."""
    db_file = str(tmp_path / "test_signals.db")
    monkeypatch.setattr(db, "DB_PATH", db_file)
    db.init_db()
    return db_file


@pytest.fixture
def mock_redis():
    """Returns the shared mock redis instance for assertions."""
    return mock_redis_instance


@pytest.fixture
def sample_signal():
    """A valid parsed signal dict (as returned by parse_message_to_signal)."""
    return {
        "action": "BUY",
        "underlying": "NIFTY",
        "strike": 24000,
        "option_type": "CE",
        "entry_low": 120.0,
        "entry_high": None,
        "stoploss": 80.0,
        "targets": [160.0, 200.0, 250.0],
        "expiry": "26th JUNE",
        "product": "I",
        "quantity": 1,
        "signal_summary": "BUY NIFTY 24000CE",
    }


@pytest.fixture
def sample_sell_signal():
    """A valid parsed SELL signal dict."""
    return {
        "action": "SELL",
        "underlying": "SENSEX",
        "strike": 85000,
        "option_type": "PE",
        "entry_low": 300.0,
        "entry_high": None,
        "stoploss": 400.0,
        "targets": [200.0, 150.0],
        "expiry": "30th JUNE",
        "product": "I",
        "quantity": 2,
        "signal_summary": "SELL SENSEX 85000PE",
    }


@pytest.fixture
def sample_redis_message():
    """Raw Redis stream message dict (key-value pairs from xread)."""
    return {
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
