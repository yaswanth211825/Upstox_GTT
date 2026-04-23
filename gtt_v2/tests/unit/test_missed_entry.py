from types import SimpleNamespace

import pytest

from shared.rules.missed_entry import check_missed_entry


class _RedisStub:
    def __init__(self, value):
        self._value = value

    async def get(self, key: str):
        return self._value


def _mk_adj(action: str = "BUY"):
    return SimpleNamespace(
        instrument_key="NSE_FO|12345",
        targets_adj=[270.0],
        stoploss_adj=220.0,
        raw=SimpleNamespace(action=action),
    )


@pytest.mark.asyncio
async def test_buy_is_not_blocked_when_ltp_is_below_stoploss():
    adj = _mk_adj("BUY")

    can_place, reason = await check_missed_entry(adj, _RedisStub("215.0"))

    assert can_place is True
    assert reason == ""


@pytest.mark.asyncio
async def test_buy_is_still_blocked_when_target_already_hit():
    adj = _mk_adj("BUY")

    can_place, reason = await check_missed_entry(adj, _RedisStub("275.0"))

    assert can_place is False
    assert "already_above_target" in reason
