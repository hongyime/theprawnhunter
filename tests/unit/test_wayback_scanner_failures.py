import logging

import httpx
import pytest

from app.infrastructure.scanners.providers import wayback as wayback_provider
from app.workers.tasks import flow_tasks
from app.workers.tasks.scanners.providers import wayback as wayback_task


QUERY = "api.telegram.org"
TOKEN = f"12345678:{'Z' * 35}"
ORIGINAL_ONE = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
ORIGINAL_TWO = "https://api.telegram.org/bot22222222/sendMessage"
TIMESTAMP = "20260101000000"
CDX_ROWS = [["timestamp", "original"]]


class _MemoryRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}

    def get(self, key):
        return self.values.get(key)

    def exists(self, key):
        return int(key in self.values)

    def setex(self, key, _ttl, value):
        self.values[key] = value
        return True

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    def hdel(self, key, field):
        return int(self.hashes.setdefault(key, {}).pop(field, None) is not None)


class _ClientContext:
    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *_args):
        return False


class _FakeClient:
    def __init__(self, responder, calls):
        self.responder = responder
        self.calls = calls

    async def get(self, url, params=None):
        self.calls.append((url, params))
        attempt = sum(1 for seen_url, _ in self.calls if seen_url == url)
        return self.responder(url, params, attempt)


class _FakeHttp:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def client(self, **_kwargs):
        return _ClientContext(_FakeClient(self.responder, self.calls))


def _response(status=200, *, payload=None, text=None, headers=None, url=None):
    request = httpx.Request("GET", url or wayback_provider.WaybackService.CDX_URL)
    if payload is not None:
        return httpx.Response(status, json=payload, headers=headers or {}, request=request)
    return httpx.Response(status, text=text or "", headers=headers or {}, request=request)


def _rows(*originals):
    return CDX_ROWS + [[TIMESTAMP, original] for original in originals]


def _prepare(monkeypatch, responder):
    monkeypatch.setenv("WAYBACK_LIMIT", "500")
    redis = _MemoryRedis()
    fake_http = _FakeHttp(responder)
    service = wayback_provider.WaybackService()
    saved = []
    statuses = []
    sleeps = []

    async def save_results(results, source_name):
        saved.extend((source_name, result) for result in results)
        return len(results)

    async def send_status(message):
        statuses.append(message)

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(wayback_provider.httpx, "AsyncClient", fake_http.client)
    monkeypatch.setattr(wayback_provider.asyncio, "sleep", sleep)
    monkeypatch.setattr(flow_tasks, "redis_client", redis)
    monkeypatch.setattr(wayback_task, "redis_client", redis)
    monkeypatch.setattr(wayback_task, "wayback_srv", service)
    monkeypatch.setattr(wayback_task, "_save_credentials_async", save_results)
    monkeypatch.setattr(wayback_task, "_send_log_async", send_status)
    return redis, fake_http, service, saved, statuses, sleeps


@pytest.mark.asyncio
async def test_cdx_service_down_is_logged_and_can_be_retried_next_scan(monkeypatch, caplog):
    calls = 0

    def reply(url, _params, _attempt):
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise httpx.ConnectError("Wayback cannot be reached")
        return _response(payload=CDX_ROWS, url=url)

    _, http, service, _, _, sleeps = _prepare(monkeypatch, reply)
    caplog.set_level(logging.INFO)

    failed_batch = await service.search(QUERY, return_batch=True)
    next_scan_batch = await service.search(QUERY, return_batch=True)

    assert len(http.calls) == 4
    assert failed_batch.failures == 1
    assert next_scan_batch.failures == 0
    assert "Search 'api.telegram.org' failed after 3 tries" in caplog.text
    assert "repeat next scan" in caplog.text
    assert sleeps == [1.2, 2.4]


@pytest.mark.parametrize(
    ("status", "expected_calls"),
    [(401, 1), (404, 1), (422, 1), (500, 3)],
)
@pytest.mark.asyncio
async def test_cdx_http_errors_are_logged_and_search_is_left_for_next_scan(
    monkeypatch, caplog, status, expected_calls,
):
    def reply(_url, _params, _attempt):
        return _response(status, text="provider error")

    _, http, service, _, _, _ = _prepare(monkeypatch, reply)
    caplog.set_level(logging.INFO)

    batch = await service.search(QUERY, return_batch=True)

    assert len(http.calls) == expected_calls
    assert batch.failures == 1
    assert "api.telegram.org" in caplog.text and f"HTTP {status}" in caplog.text
    assert "next scan" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "retry_after", "expected_wait"), [(403, "2", 2.0), (429, "4", 4.0)])
@pytest.mark.asyncio
async def test_cdx_rate_limit_waits_before_one_retry(monkeypatch, caplog, status, retry_after, expected_wait):
    def reply(_url, _params, attempt):
        if attempt == 1:
            return _response(status, headers={"Retry-After": retry_after})
        return _response(payload=CDX_ROWS)

    _, http, service, _, _, sleeps = _prepare(monkeypatch, reply)
    caplog.set_level(logging.INFO)

    batch = await service.search(QUERY, return_batch=True)

    assert len(http.calls) == 2
    assert batch.failures == 0
    assert sleeps == [expected_wait]
    assert f"HTTP {status}" in caplog.text and f"waiting {expected_wait:.1f}s" in caplog.text


@pytest.mark.parametrize(("status", "expected_calls"), [(403, 2), (429, 2)])
@pytest.mark.asyncio
async def test_repeated_snapshot_rate_limit_stops_requests_and_keeps_snapshot(
    monkeypatch, caplog, status, expected_calls,
):
    def reply(url, _params, _attempt):
        if "/cdx/search/cdx" in url:
            return _response(payload=_rows(ORIGINAL_ONE), url=url)
        return _response(status, headers={"Retry-After": "2"}, url=url)

    redis, http, service, _, _, sleeps = _prepare(monkeypatch, reply)
    caplog.set_level(logging.INFO)

    batch = await service.search(QUERY, return_batch=True)

    archive_calls = [url for url, _ in http.calls if "/cdx/search/cdx" not in url]
    snapshot_id = service._url_hash(ORIGINAL_ONE)
    assert len(archive_calls) == expected_calls
    assert snapshot_id in redis.hashes[service.PENDING_KEY]
    assert f"wayback:seen:{snapshot_id}" not in redis.values
    assert sleeps == [1.2, 2.0]
    assert batch.failures == 1
    assert ORIGINAL_ONE in caplog.text and "stop fetches this run" in caplog.text


@pytest.mark.asyncio
async def test_empty_cdx_results_are_reported_as_no_snapshots(monkeypatch, caplog):
    def reply(url, _params, _attempt):
        return _response(payload=CDX_ROWS, url=url)

    _, _, service, _, _, _ = _prepare(monkeypatch, reply)
    caplog.set_level(logging.INFO)

    batch = await service.search(QUERY, return_batch=True)

    assert batch.matches == []
    assert batch.failures == 0
    assert "Search 'api.telegram.org' returned no snapshots" in caplog.text


@pytest.mark.parametrize(
    ("status", "body", "expected_log"),
    [
        (404, "not found", "HTTP 404"),
        (200, "", "empty snapshot"),
        (200, "Loading this snapshot", "still loading"),
        (200, "The Wayback Machine has encountered an error", "Wayback error reply"),
    ],
)
@pytest.mark.asyncio
async def test_missing_empty_loading_or_error_snapshot_stays_pending(
    monkeypatch, caplog, status, body, expected_log,
):
    def reply(url, _params, _attempt):
        if "/cdx/search/cdx" in url:
            return _response(payload=_rows(ORIGINAL_ONE), url=url)
        return _response(status, text=body, url=url)

    redis, _, service, _, _, _ = _prepare(monkeypatch, reply)
    caplog.set_level(logging.INFO)

    batch = await service.search(QUERY, return_batch=True)

    snapshot_id = service._url_hash(ORIGINAL_ONE)
    assert snapshot_id in redis.hashes[service.PENDING_KEY]
    assert f"wayback:seen:{snapshot_id}" not in redis.values
    assert batch.failures == 1
    assert ORIGINAL_ONE in caplog.text and expected_log in caplog.text
    assert "next scan" in caplog.text


@pytest.mark.parametrize(("status", "expected_calls"), [(401, 1), (404, 1), (422, 1), (500, 3)])
@pytest.mark.asyncio
async def test_failed_snapshot_does_not_stop_a_later_snapshot(monkeypatch, caplog, status, expected_calls):
    def reply(url, _params, _attempt):
        if "/cdx/search/cdx" in url:
            return _response(payload=_rows(ORIGINAL_ONE, ORIGINAL_TWO), url=url)
        if ORIGINAL_ONE in url:
            return _response(status, text="provider error", url=url)
        return _response(200, text=TOKEN, url=url)

    redis, http, _, saved, _, _ = _prepare(monkeypatch, reply)
    caplog.set_level(logging.INFO)

    result = await wayback_task._scan_wayback_async()

    first_id = wayback_provider.WaybackService._url_hash(ORIGINAL_ONE)
    second_id = wayback_provider.WaybackService._url_hash(ORIGINAL_TWO)
    first_calls = [url for url, _ in http.calls if ORIGINAL_ONE in url]
    second_calls = [url for url, _ in http.calls if ORIGINAL_TWO in url]
    assert "issue" in result
    assert len(first_calls) == expected_calls and len(second_calls) == 1
    assert first_id in redis.hashes[wayback_provider.WaybackService.PENDING_KEY]
    assert second_id not in redis.hashes[wayback_provider.WaybackService.PENDING_KEY]
    assert f"wayback:seen:{first_id}" not in redis.values
    assert f"wayback:seen:{second_id}" in redis.values
    assert len(saved) == 1 and saved[0][1]["token"] == TOKEN
    assert saved[0][1]["meta"]["wayback_url"] == ORIGINAL_TWO
    assert ORIGINAL_ONE in caplog.text and f"HTTP {status}" in caplog.text


@pytest.mark.asyncio
async def test_task_save_failure_leaves_snapshot_for_another_scan(monkeypatch, caplog):
    def reply(url, _params, _attempt):
        if "/cdx/search/cdx" in url:
            return _response(payload=_rows(ORIGINAL_ONE), url=url)
        return _response(200, text=TOKEN, url=url)

    redis, _, service, saved, _, _ = _prepare(monkeypatch, reply)
    caplog.set_level(logging.INFO)
    save_calls = 0

    async def fail_once(results, source_name):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            raise RuntimeError("validation queue is unavailable")
        saved.extend((source_name, item) for item in results)
        return len(results)

    monkeypatch.setattr(wayback_task, "_save_credentials_async", fail_once)
    snapshot_id = service._url_hash(ORIGINAL_ONE)

    with pytest.raises(RuntimeError, match="validation queue is unavailable"):
        await wayback_task._scan_wayback_async()
    assert snapshot_id in redis.hashes[service.PENDING_KEY]
    assert f"wayback:seen:{snapshot_id}" not in redis.values

    await wayback_task._scan_wayback_async()

    assert snapshot_id not in redis.hashes[service.PENDING_KEY]
    assert f"wayback:seen:{snapshot_id}" in redis.values
    assert len(saved) == 1
    assert "Celery will retry this task" in caplog.text


@pytest.mark.asyncio
async def test_configured_limit_is_capped_and_only_one_cdx_page_is_read(monkeypatch):
    originals = [ORIGINAL_ONE, ORIGINAL_TWO, "https://api.telegram.org/bot33333333/sendMessage"]

    def reply(url, _params, _attempt):
        if "/cdx/search/cdx" in url:
            return _response(payload=_rows(*originals), url=url)
        return _response(200, text="clean page", url=url)

    _, http, service, _, _, _ = _prepare(monkeypatch, reply)
    service.MAX_RESULTS = 2
    service.MAX_REQUESTS = 2

    batch = await service.search(QUERY, limit=99, return_batch=True)

    cdx_calls = [(url, params) for url, params in http.calls if "/cdx/search/cdx" in url]
    archive_calls = [url for url, _ in http.calls if "/cdx/search/cdx" not in url]
    assert len(cdx_calls) == 1
    assert cdx_calls[0][1]["limit"] == 2
    assert len(archive_calls) == 2
    assert len(batch.completed_snapshot_ids) == 2


@pytest.mark.asyncio
async def test_default_search_keeps_list_return_for_username_pivot(monkeypatch):
    def reply(url, _params, _attempt):
        if "/cdx/search/cdx" in url:
            return _response(payload=_rows(ORIGINAL_ONE), url=url)
        return _response(200, text=TOKEN, url=url)

    redis, _, service, _, _, _ = _prepare(monkeypatch, reply)

    results = await service.search(QUERY)

    snapshot_id = service._url_hash(ORIGINAL_ONE)
    assert isinstance(results, list) and results[0]["token"] == TOKEN
    assert snapshot_id not in redis.hashes[service.PENDING_KEY]
    assert f"wayback:seen:{snapshot_id}" in redis.values
