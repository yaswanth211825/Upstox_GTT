"""
Market simulation runner.

Runs all scenarios against a real PostgreSQL + Redis instance with:
- Fake market clock (time mocked)
- Mock Upstox client (records GTT placements, no real HTTP)
- Real price movements written to Redis
- Real buffer, timing, P&L guard, dedup, lot-split logic

Usage:
    PYTHONPATH=. python3 tests/simulation/runner.py
    PYTHONPATH=. python3 tests/simulation/runner.py --filter BUF
    PYTHONPATH=. python3 tests/simulation/runner.py --filter TT-2
"""
import asyncio
import os
import sys
import time
import argparse
import traceback
from typing import Optional

# ── Bootstrap .env ────────────────────────────────────────────────────────────
os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(".env")

from shared.config import settings, configure_logging
from shared.db.postgres import create_pool, run_migrations
from shared.db.writer import AsyncDBWriter
from shared.redis.client import create_redis, ensure_consumer_group, stream_add, stream_read_group, stream_ack
from shared.instruments.cache import InstrumentCache
from shared.instruments.loader import download_instruments
from shared.rules.timing import MarketTimingGate
from shared.rules.pnl_guard import DailyPnLGuard

from tests.simulation.scenarios import SCENARIOS
from tests.simulation.mock_upstox import MockUpstoxClient
from tests.simulation.market_clock import at_time
from tests.simulation.price_feed import set_ltp

import structlog
log = structlog.get_logger()


# ── ANSI colours ──────────────────────────────────────────────────────────────
GREEN = "\033[92m"
RED   = "\033[91m"
YELLOW= "\033[93m"
CYAN  = "\033[96m"
BOLD  = "\033[1m"
RESET = "\033[0m"
TICK  = f"{GREEN}✔{RESET}"
CROSS = f"{RED}✘{RESET}"
WARN  = f"{YELLOW}⚠{RESET}"


class SimResult:
    def __init__(self, scenario_id: str, name: str):
        self.id = scenario_id
        self.name = name
        self.passed = True
        self.errors: list[str] = []
        self.details: dict = {}
        self.latency_ms: float = 0

    def fail(self, msg: str):
        self.passed = False
        self.errors.append(msg)


async def run_scenario(
    scenario: dict,
    pool,
    redis,
    cache: InstrumentCache,
    upstox: MockUpstoxClient,
    db_writer: AsyncDBWriter,
) -> SimResult:
    """Run a single scenario and return its result."""
    from services.trading_engine.main import process_signal
    from shared.db.signals import compute_signal_hash

    sid = scenario["id"]
    result = SimResult(sid, scenario["name"])
    t_start = time.monotonic()

    # Override trader profile if scenario specifies
    original_profile = settings.trader_profile
    if "trader_profile" in scenario:
        settings.trader_profile = scenario["trader_profile"]

    # Override DRY_RUN to false so we use the mock upstox (not dry_run path)
    original_dry_run = settings.dry_run
    settings.dry_run = False

    try:
        stream = settings.redis_stream_name + "_sim"   # isolated stream per run
        group  = "sim-cg"
        await ensure_consumer_group(redis, stream, group)

        # Reset the mock upstox GTT list for this scenario
        upstox.gtts.clear()
        upstox.cancelled.clear()

        sig = scenario["signal"]

        # ── Clean up any DB rows from prior runs with the same hash ──────────
        # Must delete child rows first (gtt_rules FK) before deleting signals.
        prior_hash = _compute_scenario_hash(sig)
        if prior_hash:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    DELETE FROM gtt_rules
                    WHERE signal_id IN (
                        SELECT id FROM signals WHERE signal_hash = $1
                    )
                    """,
                    prior_hash,
                )
                await conn.execute(
                    "DELETE FROM signals WHERE signal_hash = $1", prior_hash
                )
        ltp_before = scenario.get("ltp_before")
        repeat = scenario.get("repeat", 1)

        # Inject daily P&L into Redis if scenario requires it
        daily_pnl_inject = scenario.get("daily_pnl_inject")
        if daily_pnl_inject is not None:
            import datetime as _dt
            today = _dt.datetime.now().strftime("%Y%m%d")
            pnl_key = f"daily_pnl:{today}"
            limit_key = f"pnl_limit:{today}"
            await redis.set(pnl_key, str(daily_pnl_inject), ex=90000)
            capital = float(os.getenv("ACCOUNT_CAPITAL", "100000"))
            limit_pct = float(os.getenv("DAILY_PNL_LIMIT_PCT", "0.10"))
            if daily_pnl_inject >= capital * limit_pct:
                await redis.set(limit_key, "1", ex=90000)
            else:
                await redis.delete(limit_key)

        injected_ids = []
        for i in range(repeat):
            # Add a small variation to avoid exact dedup on repeated injection
            # (only for non-dedup test scenarios)
            payload = dict(sig)
            if i > 0 and scenario.get("id") != "DEDUP-1":
                payload["_run"] = str(i)   # make it distinct

            msg_id = await stream_add(redis, stream, payload)
            injected_ids.append(msg_id)

        # Process messages through the pipeline (with mocked time)
        timing_gate = MarketTimingGate()
        pnl_guard = DailyPnLGuard()

        market_time = scenario.get("market_time", "mid_session")
        processed_count = 0

        with at_time(market_time):
            messages = await stream_read_group(redis, stream, group, "sim-consumer", count=10, block_ms=500)
            for msg_id, fields in messages:
                # Inject LTP before processing (so missed_entry_guard can check)
                # We need the instrument_key — we do a quick resolve first
                instrument_key = await _resolve_instrument_key(cache, fields)
                if instrument_key and ltp_before is not None:
                    await set_ltp(redis, instrument_key, ltp_before)
                elif instrument_key and ltp_before is None:
                    # Explicitly delete LTP so guard can't check (no-cache scenario)
                    await redis.delete(f"ltp:{instrument_key}")

                await process_signal(
                    msg_id, fields, upstox, cache, db_writer,
                    redis, pool, timing_gate, pnl_guard,
                )
                await stream_ack(redis, stream, group, msg_id)
                processed_count += 1

        # Flush DB writer
        await asyncio.sleep(0.3)

        result.latency_ms = round((time.monotonic() - t_start) * 1000)

        # ── Fetch signal from DB ────────────────────────────────────────────
        async with pool.acquire() as conn:
            # Use the hash to find our specific signal
            sig_hash = _compute_scenario_hash(sig)
            rows = await conn.fetch(
                "SELECT * FROM signals WHERE signal_hash = $1 ORDER BY id DESC", sig_hash
            )

        result.details["db_rows"] = len(rows)
        result.details["gtt_count"] = len(upstox.gtts)

        expected = scenario.get("expected", {})

        # ── Check signal_count ───────────────────────────────────────────────
        if "signal_count" in expected:
            if len(rows) != expected["signal_count"]:
                result.fail(f"expected {expected['signal_count']} DB row(s), got {len(rows)}")
            if expected["signal_count"] == 0:
                return result   # nothing more to check

        # ── Check status ─────────────────────────────────────────────────────
        if rows and "status" in expected:
            actual_status = rows[0]["status"]
            result.details["status"] = actual_status
            if actual_status not in expected["status"]:
                result.fail(f"status={actual_status!r}, expected one of {expected['status']}")

        # ── Check block_reason ───────────────────────────────────────────────
        if rows and "block_reason_prefix" in expected:
            br = rows[0]["block_reason"] or ""
            result.details["block_reason"] = br
            prefix = expected["block_reason_prefix"]
            if not br.startswith(prefix):
                result.fail(f"block_reason={br!r}, expected prefix {prefix!r}")

        # ── Check buffer math ─────────────────────────────────────────────────
        _check_numeric(result, rows, "entry_low_adj", expected)
        _check_numeric(result, rows, "stoploss_adj", expected)
        _check_array_first(result, rows, "targets_adj", 0, expected)

        # ── Check entry_high_adj ──────────────────────────────────────────────
        if "entry_high_adj" in expected and rows:
            actual = float(rows[0]["entry_high_adj"] or 0)
            exp_val = expected["entry_high_adj"]
            result.details["entry_high_adj"] = actual
            if abs(actual - exp_val) > 0.01:
                result.fail(f"entry_high_adj={actual}, expected {exp_val}")

        # ── Check trade_type ─────────────────────────────────────────────────
        if "trade_type" in expected and rows:
            actual = rows[0]["trade_type"]
            result.details["trade_type"] = actual
            if actual != expected["trade_type"]:
                result.fail(f"trade_type={actual!r}, expected {expected['trade_type']!r}")

        # ── Check GTT count ───────────────────────────────────────────────────
        if "gtt_count" in expected:
            actual_gtt = len(upstox.gtts)
            result.details["gtt_count"] = actual_gtt
            if actual_gtt != expected["gtt_count"]:
                result.fail(f"gtt_count={actual_gtt}, expected {expected['gtt_count']}")

    except Exception as e:
        result.fail(f"Exception: {e}\n{traceback.format_exc()}")
    finally:
        settings.trader_profile = original_profile
        settings.dry_run = original_dry_run

    return result


def _check_numeric(result: SimResult, rows, field: str, expected: dict):
    if field not in expected or not rows:
        return
    actual = rows[0][field]
    if actual is None:
        result.fail(f"{field}=None, expected {expected[field]}")
        return
    actual = float(actual)
    exp_val = float(expected[field])
    result.details[field] = actual
    if abs(actual - exp_val) > 0.01:
        result.fail(f"{field}={actual}, expected {exp_val}")


def _check_array_first(result: SimResult, rows, field: str, idx: int, expected: dict):
    key = f"{field}[{idx}]"
    if key not in expected or not rows:
        return
    arr = rows[0][field]
    if not arr or len(arr) <= idx:
        result.fail(f"{key}: array empty or too short (got {arr})")
        return
    actual = float(arr[idx])
    exp_val = float(expected[key])
    result.details[key] = actual
    if abs(actual - exp_val) > 0.01:
        result.fail(f"{key}={actual}, expected {exp_val}")


async def _resolve_instrument_key(cache: InstrumentCache, fields: dict) -> Optional[str]:
    from shared.signal.parser import parse_message_to_signal
    sig = parse_message_to_signal(fields)
    if not sig or not sig.expiry or not sig.strike or not sig.option_type:
        return None
    try:
        inst = await cache.resolve(sig.underlying, sig.strike, sig.option_type, str(sig.expiry))
        return inst.get("instrument_key")
    except Exception:
        return None


def _compute_scenario_hash(sig: dict) -> Optional[str]:
    from shared.db.signals import compute_signal_hash
    from shared.signal.parser import parse_message_to_signal
    parsed = parse_message_to_signal(sig)
    if not parsed:
        return None
    return compute_signal_hash(
        parsed.underlying, parsed.strike, parsed.option_type,
        parsed.entry_low, parsed.stoploss, parsed.targets,
        str(parsed.expiry) if parsed.expiry else None,
    )


def _print_result(r: SimResult, verbose: bool = False):
    icon = TICK if r.passed else CROSS
    status_str = ""
    if r.details.get("status"):
        status_str = f"  [{CYAN}{r.details['status']}{RESET}]"
    latency = f"  {YELLOW}{r.latency_ms}ms{RESET}"
    print(f"  {icon} {BOLD}{r.id}{RESET}  {r.name}{status_str}{latency}")
    if not r.passed or verbose:
        for err in r.errors:
            print(f"      {RED}→ {err}{RESET}")
        if verbose and r.details:
            for k, v in r.details.items():
                print(f"      {CYAN}{k}{RESET} = {v}")


async def main(filter_prefix: str = "", verbose: bool = False):
    configure_logging("simulation")

    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════════════════╗")
    print(f"║   GTT v2 — Market Simulation Test Suite          ║")
    print(f"╚══════════════════════════════════════════════════╝{RESET}\n")

    pool = await create_pool(settings.postgres_dsn)
    await run_migrations(pool)
    redis = await create_redis(settings.redis_host, settings.redis_port)

    # Reset rate limiters so they don't affect scenario results
    await redis.delete("rate_limit:gtt_place")
    await redis.delete(f"pnl_limit:{__import__('datetime').datetime.now().strftime('%Y%m%d')}")

    print("Loading instrument cache...", end=" ", flush=True)
    cache = InstrumentCache(redis)
    loaded_count = int(await redis.hlen("instruments:index") or 0)
    if loaded_count < 1000:
        import httpx
        async with httpx.AsyncClient() as http:
            instruments = await download_instruments(http)
        await cache.load(instruments)
        print(f"downloaded {len(instruments)} total, {await redis.hlen('instruments:index')} options")
    else:
        print(f"using cached {loaded_count} options (Redis)")

    upstox = MockUpstoxClient()
    db_writer = AsyncDBWriter(pool)
    writer_task = asyncio.create_task(db_writer.run())

    scenarios = SCENARIOS
    if filter_prefix:
        scenarios = [s for s in SCENARIOS if filter_prefix.upper() in s["id"]]
        if not scenarios:
            print(f"{WARN} No scenarios match filter {filter_prefix!r}")
            return

    groups: dict[str, list] = {}
    for s in scenarios:
        prefix = s["id"].split("-")[0]
        groups.setdefault(prefix, []).append(s)

    total = passed = failed = 0
    all_results = []

    for group_name, group_scenarios in groups.items():
        label_map = {
            "BUF":   "Buffer Rules",
            "TT":    "Trade Types",
            "TIME":  "Timing Gate",
            "ME":    "Missed Entry Guard",
            "DEDUP": "Deduplication",
            "PROF":  "Trader Profile Filter",
            "VAL":   "Signal Validation",
        }
        print(f"\n{BOLD}── {label_map.get(group_name, group_name)} ──────────────────────────────{RESET}")

        for scenario in group_scenarios:
            result = await run_scenario(scenario, pool, redis, cache, upstox, db_writer)
            all_results.append(result)
            _print_result(result, verbose=verbose)
            total += 1
            if result.passed:
                passed += 1
            else:
                failed += 1

            # Reset rate limiter and P&L keys between scenarios
            await redis.delete("rate_limit:gtt_place")
            import datetime as _dt2
            _today = _dt2.datetime.now().strftime("%Y%m%d")
            await redis.delete(f"daily_pnl:{_today}")
            await redis.delete(f"pnl_limit:{_today}")

    print(f"\n{'═' * 52}")
    colour = GREEN if failed == 0 else RED
    print(f"{colour}{BOLD}  {passed}/{total} scenarios passed  ({failed} failed){RESET}\n")

    await db_writer.stop()
    writer_task.cancel()
    await pool.close()

    return failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GTT v2 Market Simulation")
    parser.add_argument("--filter", default="", help="Run only scenarios matching this prefix (e.g. BUF, TT-2)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show full details for every scenario")
    args = parser.parse_args()

    failed = asyncio.run(main(filter_prefix=args.filter, verbose=args.verbose))
    sys.exit(0 if failed == 0 else 1)
