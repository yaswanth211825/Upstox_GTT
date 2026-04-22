"""Unit tests for DailyPnLGuard."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_allow_below_limit(monkeypatch):
    monkeypatch.setenv("ACCOUNT_CAPITAL", "100000")
    monkeypatch.setenv("DAILY_PNL_LIMIT_PCT", "0.10")

    from shared.rules.pnl_guard import DailyPnLGuard
    guard = DailyPnLGuard()

    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=lambda key: "5000" if "daily_pnl" in key else None)

    allowed = await guard.allow(redis)
    assert allowed is True


@pytest.mark.asyncio
async def test_block_when_limit_flag_set():
    from shared.rules.pnl_guard import DailyPnLGuard
    guard = DailyPnLGuard()

    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=lambda key: "1" if "pnl_limit" in key else "0")

    allowed = await guard.allow(redis)
    assert allowed is False


@pytest.mark.asyncio
async def test_block_when_pnl_exceeds_limit(monkeypatch):
    monkeypatch.setenv("ACCOUNT_CAPITAL", "100000")
    monkeypatch.setenv("DAILY_PNL_LIMIT_PCT", "0.10")

    from shared.rules.pnl_guard import DailyPnLGuard
    guard = DailyPnLGuard()

    redis = AsyncMock()
    # pnl_limit key not set (returns None), but daily_pnl is 12000 (> 10%)
    async def _redis_get(key):
        if "pnl_limit" in key:
            return None
        return "12000"
    redis.get = _redis_get

    allowed = await guard.allow(redis)
    assert allowed is False
