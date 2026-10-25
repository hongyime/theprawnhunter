import asyncio
import fnmatch
import time

from app.workers.tasks.flows import health


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}
        self.expirations = {}

    def scan_iter(self, match="*", count=None):
        for key in list(self.values):
            if fnmatch.fnmatch(key, match):
                yield key

    def ttl(self, key):
        return self.ttls.get(key, -2)

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.values[key] = str(value)
        self.expirations[key] = ttl


class _FakeBroadcaster:
    def __init__(self):
        self.logs = []

    async def send_log(self, message):
        self.logs.append(message)
        return True


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _install_api(monkeypatch, status_code=200, payload=None):
    calls = []

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            calls.append((url, headers))
            return _FakeResponse(
                status_code,
                payload if payload is not None else {"status": "healthy"},
            )

    monkeypatch.setattr(health.httpx, "AsyncClient", _FakeAsyncClient)
    return calls


def _seed_healthy(fake):
    fake.values["bot_listener:poll_lock:123"] = "instance-a"
    fake.ttls["bot_listener:poll_lock:123"] = 90
    fake.values["system:heartbeat:last_seen"] = str(time.time())
    for signal in health.HEALTH_ALERT_SIGNALS:
        fake.values[f"{health.HEALTH_ALERT_STATE_PREFIX}{signal}"] = "healthy"


def _run(monkeypatch, fake, broadcaster):
    monkeypatch.setattr(health, "redis_client", fake)
    monkeypatch.setattr(health, "get_broadcaster", lambda: broadcaster)
    return asyncio.run(health._health_alert_logic())


def test_alerts_on_healthy_to_unhealthy_transition(monkeypatch):
    fake = _FakeRedis()
    _seed_healthy(fake)
    fake.values["system:heartbeat:last_seen"] = str(time.time() - 4000)
    _install_api(monkeypatch)
    broadcaster = _FakeBroadcaster()

    result = _run(monkeypatch, fake, broadcaster)

    assert len(broadcaster.logs) == 1
    assert "heartbeat" in broadcaster.logs[0]
    assert "unhealthy" in broadcaster.logs[0]
    assert fake.values[
        f"{health.HEALTH_ALERT_STATE_PREFIX}heartbeat"
    ] == "unhealthy"
    assert result["transitions"] == [{"signal": "heartbeat", "state": "unhealthy"}]


def test_repeated_unhealthy_does_not_realert(monkeypatch):
    fake = _FakeRedis()
    _seed_healthy(fake)
    fake.values["system:heartbeat:last_seen"] = str(time.time() - 4000)
    _install_api(monkeypatch)
    broadcaster = _FakeBroadcaster()

    _run(monkeypatch, fake, broadcaster)
    second = _run(monkeypatch, fake, broadcaster)

    assert len(broadcaster.logs) == 1
    assert second["transitions"] == []
    assert second["checks"]["heartbeat"]["healthy"] is False


def test_recovery_message_on_unhealthy_to_healthy(monkeypatch):
    fake = _FakeRedis()
    _seed_healthy(fake)
    fake.values[f"{health.HEALTH_ALERT_STATE_PREFIX}heartbeat"] = "unhealthy"
    _install_api(monkeypatch)
    broadcaster = _FakeBroadcaster()

    result = _run(monkeypatch, fake, broadcaster)

    assert len(broadcaster.logs) == 1
    assert "heartbeat" in broadcaster.logs[0]
    assert "recovered" in broadcaster.logs[0]
    assert fake.values[
        f"{health.HEALTH_ALERT_STATE_PREFIX}heartbeat"
    ] == "healthy"
    assert result["transitions"] == [{"signal": "heartbeat", "state": "healthy"}]


def test_failing_probe_does_not_block_other_probes(monkeypatch):
    fake = _FakeRedis()
    _seed_healthy(fake)
    _install_api(monkeypatch)
    broadcaster = _FakeBroadcaster()

    async def _boom():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(health, "_probe_heartbeat", _boom)

    result = _run(monkeypatch, fake, broadcaster)

    assert len(broadcaster.logs) == 1
    assert "heartbeat" in broadcaster.logs[0]
    assert result["checks"]["heartbeat"]["healthy"] is False
    assert result["checks"]["bot_listener_lock"]["healthy"] is True
    assert result["checks"]["api_detailed"]["healthy"] is True
    assert fake.values[
        f"{health.HEALTH_ALERT_STATE_PREFIX}bot_listener_lock"
    ] == "healthy"
    assert fake.values[
        f"{health.HEALTH_ALERT_STATE_PREFIX}api_detailed"
    ] == "healthy"


def test_first_healthy_observation_is_silent(monkeypatch):
    fake = _FakeRedis()
    fake.values["bot_listener:poll_lock:123"] = "instance-a"
    fake.ttls["bot_listener:poll_lock:123"] = 90
    fake.values["system:heartbeat:last_seen"] = str(time.time())
    _install_api(monkeypatch)
    broadcaster = _FakeBroadcaster()

    result = _run(monkeypatch, fake, broadcaster)

    assert broadcaster.logs == []
    assert result["transitions"] == []
    for signal in health.HEALTH_ALERT_SIGNALS:
        assert fake.values[
            f"{health.HEALTH_ALERT_STATE_PREFIX}{signal}"
        ] == "healthy"


def test_api_detailed_uses_monitor_key_and_reports_degraded(monkeypatch):
    fake = _FakeRedis()
    _seed_healthy(fake)
    calls = _install_api(
        monkeypatch,
        status_code=503,
        payload={"status": "degraded", "checks": {}},
    )
    broadcaster = _FakeBroadcaster()

    result = _run(monkeypatch, fake, broadcaster)

    assert len(calls) == 1
    assert calls[0][0].endswith("/health/detailed")
    assert calls[0][1] == {"X-Monitor-Key": health.settings.MONITOR_API_KEY}
    assert result["checks"]["api_detailed"]["healthy"] is False
    assert "api_detailed" in broadcaster.logs[0]
