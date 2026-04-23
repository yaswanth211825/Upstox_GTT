"""
app.py -- single-entry supervisor for the unified Telegram -> Redis -> Upstox stack.
"""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from settings import DATA_DIR, PYTHON_BIN as CONFIGURED_PYTHON_BIN

ROOT = Path(__file__).resolve().parent
PYTHON = CONFIGURED_PYTHON_BIN or sys.executable
PID_FILE = DATA_DIR / "app.pid"

load_dotenv(ROOT / ".env")

PROCESS_MODE = os.getenv("PROCESS_MODE", os.getenv("SIGNAL_SOURCE_MODE", "web")).strip().lower()


def _services_for_mode() -> list[tuple[str, list[str]]]:
    shared = [
        ("gtt_strategy", ["gtt_strategy.py"]),
        ("upstox_order_tracker", ["upstox_order_tracker.py"]),
    ]
    if PROCESS_MODE == "telegram":
        return [
            ("telegram_ai_listener", ["telegram_ai_listener.py"]),
            *shared,
        ]
    if PROCESS_MODE == "web":
        return [
            ("frontend_signal_bridge", ["trade_terminal_app/frontend_signal_bridge.py"]),
            *shared,
        ]
    raise SystemExit("Invalid PROCESS_MODE. Use 'telegram' or 'web'.")


def _spawn(name: str, args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [PYTHON, *args],
        cwd=ROOT,
    )


def _is_port_open(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.3)
    try:
        return sock.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        sock.close()


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

    services = _services_for_mode()

    print(f"Starting unified UpstoxGTT stack in {PROCESS_MODE.upper()} mode...")
    for name, args in services:
        if name == "frontend_signal_bridge":
            bridge_host = os.getenv("FRONTEND_BIND_HOST", "127.0.0.1")
            bridge_port = int(os.getenv("FRONTEND_BIND_PORT", "8787"))
            if _is_port_open(bridge_host, bridge_port):
                print(f"{name} already running at {bridge_host}:{bridge_port}, reusing existing process")
                continue
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
