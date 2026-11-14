"""Unit tests for CircuitBreaker cluster and in-memory states."""

import time

import pytest

from app.core.circuit_breaker import CircuitBreaker, CircuitBreakerError, CircuitState


@pytest.fixture(autouse=True)
def clean_redis(monkeypatch):
    """Ensure tests run against a clean memory state and do not bleed into shared Redis."""
    from app.core import circuit_breaker
    store = {}

    class FakeRedis:
        def get(self, key):
            return store.get(key)

        def set(self, key, val, **_kw):
            store[key] = val
            return True

        def delete(self, *keys):
            for k in keys:
                store.pop(k, None)

        def eval(self, script, numkeys, key, event, fail_th, succ_th, now):
            import json
            raw = store.get(key)
            s = json.loads(raw) if raw else {"state": "closed", "failure_count": 0, "success_count": 0}
            if event == "failure":
                s["failure_count"] = s.get("failure_count", 0) + 1
                s["last_failure_time"] = float(now)
                if s["state"] == "half_open" or s["failure_count"] >= int(fail_th):
                    s["state"] = "open"
                    s["success_count"] = 0
            elif event == "success":
                if s["state"] == "half_open":
                    s["success_count"] = s.get("success_count", 0) + 1
                    if s["success_count"] >= int(succ_th):
                        s["state"] = "closed"
                        s["failure_count"] = 0
                        s["success_count"] = 0
                else:
                    s["failure_count"] = 0
            store[key] = json.dumps(s)
            return store[key]

    monkeypatch.setattr(circuit_breaker, "_redis_client", lambda: FakeRedis())


def test_circuit_breaker_closed_allows_calls():
    breaker = CircuitBreaker("test_service_closed", failure_threshold=2)

    @breaker.call
    def succeed():
        return "ok"

    assert succeed() == "ok"
    assert breaker.state == CircuitState.CLOSED


def test_circuit_breaker_trips_to_open_after_threshold():
    breaker = CircuitBreaker("test_service_trip", failure_threshold=2, recovery_timeout=60)

    @breaker.call
    def fail():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        fail()
    assert breaker.state == CircuitState.CLOSED

    with pytest.raises(RuntimeError, match="boom"):
        fail()
    assert breaker.state == CircuitState.OPEN

    # Subsequent call fails fast without calling function
    with pytest.raises(CircuitBreakerError, match="is OPEN"):
        fail()


@pytest.mark.asyncio
async def test_async_circuit_breaker_recovery_cycle():
    breaker = CircuitBreaker(
        "test_service_recovery",
        failure_threshold=1,
        recovery_timeout=0.01,
        success_threshold=1,
    )

    @breaker.call
    async def run(should_fail=False):
        if should_fail:
            raise RuntimeError("service down")
        return "recovered"

    with pytest.raises(RuntimeError):
        await run(should_fail=True)
    assert breaker.state == CircuitState.OPEN

    # Wait for recovery timeout
    time.sleep(0.02)

    # Next call transitions to HALF_OPEN then CLOSED on success
    res = await run(should_fail=False)
    assert res == "recovered"
    assert breaker.state == CircuitState.CLOSED


def test_circuit_breaker_manual_reset():
    breaker = CircuitBreaker("test_service_reset", failure_threshold=1)

    @breaker.call
    def fail():
        raise RuntimeError("err")

    with pytest.raises(RuntimeError):
        fail()
    assert breaker.state == CircuitState.OPEN

    breaker.reset()
    assert breaker.state == CircuitState.CLOSED
