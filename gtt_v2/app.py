"""
GTT v2 — Single launcher.
Run this file to start all services in one terminal.

    python3 app.py              # all services
    python3 app.py --no-price   # skip price-monitor (saves memory locally)
"""
import asyncio
import os
import sys
import signal
import argparse
from datetime import datetime

# ── colour codes ──────────────────────────────────────────────────────────────
COLOURS = {
    "signal-ingestor": "\033[96m",   # cyan
    "trading-engine":  "\033[92m",   # green
    "order-tracker":   "\033[93m",   # yellow
    "price-monitor":   "\033[95m",   # magenta
    "admin-api":       "\033[94m",   # blue
}
RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[91m"


def _ts():
    return datetime.now().strftime("%H:%M:%S")


async def _stream(name: str, stream: asyncio.StreamReader):
    colour = COLOURS.get(name, "")
    prefix = f"{colour}{BOLD}[{name}]{RESET}"
    async for line in stream:
        text = line.decode(errors="replace").rstrip()
        print(f"{prefix} {text}", flush=True)


async def run_service(name: str, module: str) -> asyncio.subprocess.Process:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", module,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=os.path.dirname(os.path.abspath(__file__)),
        env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.abspath(__file__))},
    )
    asyncio.create_task(_stream(name, proc.stdout))
    return proc


async def main(services: list[tuple[str, str]]):
    print(f"\n{BOLD}{'═'*58}")
    print(f"  GTT v2 — starting {len(services)} service(s)")
    print(f"{'═'*58}{RESET}\n")

    procs = []
    for name, module in services:
        colour = COLOURS.get(name, "")
        print(f"  {colour}▶ {name}{RESET}  →  {module}")
        proc = await run_service(name, module)
        procs.append((name, proc))

    print(f"\n{BOLD}  All services running. Press Ctrl+C to stop all.{RESET}\n")

    # ── graceful shutdown on Ctrl+C ───────────────────────────────────────────
    loop = asyncio.get_event_loop()
    stop = loop.create_future()

    def _on_signal():
        if not stop.done():
            stop.set_result(None)

    loop.add_signal_handler(signal.SIGINT,  _on_signal)
    loop.add_signal_handler(signal.SIGTERM, _on_signal)

    # Wait for stop signal or any process to exit unexpectedly
    monitor_tasks = [
        asyncio.create_task(_watch(name, proc, stop))
        for name, proc in procs
    ]

    await stop
    print(f"\n{RED}{BOLD}  Stopping all services…{RESET}")

    for name, proc in procs:
        if proc.returncode is None:
            proc.terminate()

    await asyncio.gather(*[proc.wait() for _, proc in procs], return_exceptions=True)
    for t in monitor_tasks:
        t.cancel()

    print(f"{BOLD}  All stopped.{RESET}\n")


async def _watch(name: str, proc: asyncio.subprocess.Process, stop: asyncio.Future):
    await proc.wait()
    if proc.returncode != 0 and not stop.done():
        print(f"\n{RED}{BOLD}[{name}] exited with code {proc.returncode} — stopping all{RESET}\n")
        stop.set_result(None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GTT v2 launcher")
    parser.add_argument("--no-price",  action="store_true", help="Skip price-monitor")
    parser.add_argument("--no-tracker",action="store_true", help="Skip order-tracker")
    parser.add_argument("--engine-only",action="store_true", help="trading-engine + signal-ingestor only")
    args = parser.parse_args()

    # Load .env from parent directory (same place all other scripts use)
    from dotenv import load_dotenv
    _env = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if not os.path.exists(_env):
        _env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(_env)

    all_services = [
        ("signal-ingestor", "services.signal_ingestor.main"),
        ("trading-engine",  "services.trading_engine.main"),
        ("order-tracker",   "services.order_tracker.main"),
        ("price-monitor",   "services.price_monitor.main"),
        ("admin-api",       "services.admin_api.main"),
    ]

    if args.engine_only:
        svc = [s for s in all_services if s[0] in ("signal-ingestor", "trading-engine")]
    else:
        svc = all_services
        if args.no_price:
            svc = [s for s in svc if s[0] != "price-monitor"]
        if args.no_tracker:
            svc = [s for s in svc if s[0] != "order-tracker"]

    asyncio.run(main(svc))
