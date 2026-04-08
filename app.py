"""
app.py -- single-entry supervisor for the unified Telegram -> Redis -> Upstox stack.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from settings import DATA_DIR, PYTHON_BIN as CONFIGURED_PYTHON_BIN

ROOT = Path(__file__).resolve().parent
PYTHON = CONFIGURED_PYTHON_BIN or sys.executable
PID_FILE = DATA_DIR / "app.pid"

SERVICES = [
    ("telegram_ai_listener", ["telegram_ai_listener.py"]),
    ("gtt_strategy", ["gtt_strategy.py"]),
    ("upstox_order_tracker", ["upstox_order_tracker.py"]),
]


def _spawn(name: str, args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [PYTHON, *args],
        cwd=ROOT,
    )


def main() -> int:
    if not Path(PYTHON).exists():
        print(f"Virtualenv python not found: {PYTHON}", file=sys.stderr)
        return 1
    if PID_FILE.exists():
        print(f"PID lock exists at {PID_FILE}. Stop the old app instance first.", file=sys.stderr)
        return 1

    processes: list[tuple[str, subprocess.Popen]] = []
    stopping = False
    PID_FILE.write_text(str(os.getpid()))

    def _shutdown(signum=None, frame=None):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print("\nStopping unified app...")
        for _, proc in processes:
            if proc.poll() is None:
                proc.terminate()
        deadline = time.time() + 8
        for _, proc in processes:
            if proc.poll() is None:
                remaining = max(0.0, deadline - time.time())
                try:
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    proc.kill()
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print("Starting unified UpstoxGTT stack...")
    for name, args in SERVICES:
        proc = _spawn(name, args)
        processes.append((name, proc))
        print(f"{name} PID: {proc.pid}")

    try:
        while True:
            for name, proc in processes:
                code = proc.poll()
                if code is not None:
                    print(f"{name} exited with code {code}")
                    _shutdown()
                    return code if code != 0 else 0
            time.sleep(1)
    finally:
        _shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
