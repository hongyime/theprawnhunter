"""Bounded, opt-in load test for the public health endpoint."""

import asyncio
import os
import time

import httpx
import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.live, pytest.mark.load, pytest.mark.slow]


async def test_health_endpoint_sustains_bounded_concurrency():
    if os.getenv("RUN_HTTP_LOAD_TEST") != "1":
        pytest.skip("set RUN_HTTP_LOAD_TEST=1 to enable the bounded HTTP load test")

    base_url = os.getenv("LOAD_TEST_BASE_URL", "http://127.0.0.1:8011").rstrip("/")
    request_count = int(os.getenv("LOAD_TEST_REQUESTS", "200"))
    concurrency = int(os.getenv("LOAD_TEST_CONCURRENCY", "20"))
    max_p95_ms = float(os.getenv("LOAD_TEST_MAX_P95_MS", "1000"))

    assert 1 <= request_count <= 10_000
    assert 1 <= concurrency <= 200

    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=5) as client:
        async def request_once() -> tuple[int, float]:
            async with semaphore:
                started = time.perf_counter()
                response = await client.get("/health/")
                elapsed_ms = (time.perf_counter() - started) * 1000
                return response.status_code, elapsed_ms

        started = time.perf_counter()
        results = await asyncio.gather(*(request_once() for _ in range(request_count)))
        total_seconds = time.perf_counter() - started

    status_codes = [status for status, _ in results]
    latencies = sorted(latency for _, latency in results)
    p95_ms = latencies[max(0, int(len(latencies) * 0.95) - 1)]
    throughput = request_count / total_seconds

    print(
        f"load result: requests={request_count} concurrency={concurrency} "
        f"errors={sum(status != 200 for status in status_codes)} "
        f"p95_ms={p95_ms:.1f} throughput_rps={throughput:.1f}"
    )
    assert set(status_codes) == {200}
    assert p95_ms <= max_p95_ms
