"""
Shared runtime settings for local and AWS deployments.
"""

from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("APP_DATA_DIR", str(ROOT_DIR / "data"))).resolve()
LOG_DIR = Path(os.getenv("APP_LOG_DIR", str(DATA_DIR / "logs"))).resolve()
DB_PATH = Path(os.getenv("APP_DB_PATH", str(DATA_DIR / "gtt_signals.db"))).resolve()
TELEGRAM_SESSION_PATH = Path(
    os.getenv("TELEGRAM_SESSION_PATH", str(DATA_DIR / "session_name"))
).resolve()
PYTHON_BIN = os.getenv("APP_PYTHON_BIN", "")

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
