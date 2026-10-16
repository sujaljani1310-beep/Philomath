"""Compare request-thread and queued logging against the same synthetic sink.

Run from backend with ``.venv/bin/python -B scripts/benchmark_logging.py``.
No application configuration, credentials, services, or network are used.
The baseline recreates the previous synchronous record_timing implementation;
this measures event-loop responsiveness, not production endpoint latency.
"""

import asyncio
import json
import logging
from pathlib import Path
import socket
import sys
from threading import Event
from time import perf_counter, sleep
from unittest.mock import patch


def no_network(*args, **kwargs):
    raise AssertionError("Network access is forbidden in this benchmark")


socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.getaddrinfo = no_network
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import observability


REQUESTS = 20
SINK_DELAY_SECONDS = 0.01
TICK_SECONDS = 0.005


class SlowSink(logging.Handler):
    def __init__(self):
        super().__init__()
        self.count = 0
        self.complete = Event()

    def emit(self, record):
        sleep(SINK_DELAY_SECONDS)
        self.count += 1
        if self.count == REQUESTS:
            self.complete.set()


def legacy_record_timing(operation, started, **fields):
    # The previous implementation, with the same logger and synthetic sink.
    if observability.logger.isEnabledFor(logging.INFO):
        observability.logger.info(json.dumps({
            "event": "performance",
            "operation": operation,
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "request_id": observability.request_id.get(),
            **fields,
        }))


async def exercise_requests():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        pass

    async def request():
        await observability.PerformanceMiddleware(app)(
            {"type": "http", "path": "/synthetic"}, receive, send,
        )
        if observability.request_id.get() is not None:
            raise AssertionError("Request context was not cleared")

    async def tick():
        started = perf_counter()
        await asyncio.sleep(TICK_SECONDS)
        return perf_counter() - started

    ticker = asyncio.create_task(tick())
    # Start the ticker before allowing the request batch to run.
    await asyncio.sleep(0)
    started = perf_counter()
    await asyncio.gather(*(request() for _ in range(REQUESTS)))
    elapsed = perf_counter() - started
    return {
        "request_batch_ms": round(elapsed * 1000, 3),
        "five_ms_ticker_elapsed_ms": round(await ticker * 1000, 3),
    }


def measure(queued):
    observability.shutdown_performance_logging()
    sink = SlowSink()
    logger = observability.logger
    logger.handlers = [sink]
    logger.disabled = False
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        if queued:
            observability.configure_performance_logging(True)
            result = asyncio.run(exercise_requests())
        else:
            with patch.object(observability, "record_timing", legacy_record_timing):
                result = asyncio.run(exercise_requests())
        if not sink.complete.wait(5):
            raise AssertionError("Synthetic sink did not drain all records")
        result["records_delivered"] = sink.count
        return result
    finally:
        if not observability.shutdown_performance_logging(timeout=1):
            raise AssertionError("Synthetic logging worker did not stop")
        logger.handlers = []
        sink.close()


if __name__ == "__main__":
    print(json.dumps({
        "requests": REQUESTS,
        "artificial_sink_delay_ms_per_record": SINK_DELAY_SECONDS * 1000,
        "synchronous_baseline": measure(False),
        "bounded_background_logging": measure(True),
        "limitations": "Synthetic ASGI/logging only; omits real upstream and hosting delays.",
    }, indent=2))
