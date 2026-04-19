import logging
import time

from latency import duration_ms, log_latency, now_ms, now_perf_ns


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_now_ms_returns_integer_milliseconds():
    value = now_ms()
    assert isinstance(value, int)
    assert value > 0


def test_duration_ms_non_negative():
    start = now_perf_ns()
    time.sleep(0.001)
    elapsed = duration_ms(start)
    assert isinstance(elapsed, int)
    assert elapsed >= 0


def test_log_latency_formats_line_and_tolerates_missing_values():
    logger = logging.getLogger("test_latency")
    logger.handlers = []
    logger.setLevel(logging.INFO)
    logger.propagate = False

    handler = _ListHandler()
    logger.addHandler(handler)

    log_latency(logger, "tg:-100123:456", "strategy", total_ms=842, redis_wait_ms=16, missing=None, status="success")

    assert len(handler.messages) == 1
    line = handler.messages[0]
    assert line.startswith("LATENCY ")
    assert "trace=tg:-100123:456" in line
    assert "stage=strategy" in line
    assert "total_ms=842" in line
    assert "redis_wait_ms=16" in line
    assert "status=success" in line
    assert "missing=" not in line
