"""Shared latency helpers for cross-process tracing and stage timing."""

from __future__ import annotations

import logging
import time
from typing import Any


def now_ms() -> int:
    """Return current epoch timestamp in milliseconds for cross-process handoff."""
    return int(time.time() * 1000)


def now_perf_ns() -> int:
    """Return monotonic nanoseconds for in-process duration measurement."""
    return time.perf_counter_ns()


def duration_ms(start_perf_ns: int) -> int:
    """Return elapsed milliseconds from a monotonic perf_counter_ns start."""
    if not isinstance(start_perf_ns, int):
        return 0
    delta_ns = time.perf_counter_ns() - start_perf_ns
    if delta_ns < 0:
        return 0
    return int(delta_ns / 1_000_000)


def log_latency(logger: logging.Logger, trace_id: str, stage: str, **metrics: Any) -> None:
    """Emit a searchable single-line latency log with key/value pairs."""
    safe_trace = trace_id or "unknown"
    safe_stage = stage or "unknown"

    fields = [f"trace={safe_trace}", f"stage={safe_stage}"]
    for key, value in metrics.items():
        if value is None:
            continue
        fields.append(f"{key}={value}")

    logger.info("LATENCY " + " ".join(fields))
