import logging

import httpx
import pytest

from app.infrastructure.scanners.providers import wayback as wayback_provider
from app.workers.tasks import flow_tasks
from app.workers.tasks.scanners.providers import wayback as wayback_task

TOKEN = f"12345678:{'Z' * 35}"
BODY_TOKEN = f"87654321:{'Y' * 35}"
ORIGINAL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
CLEAN_ORIGINAL = "https://paste.example/raw/leaked-config"
TIMESTAMP = "20260101000000"
CDX_HEADER = [["timestamp", "original"]]
CDX_URL = wayback_provider.WaybackService.CDX_URL


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
    request = httpx.Request("GET", url or CDX_URL)
    if payload is not None:
        return httpx.Response(status, json=payload, headers=headers or {}, request=request)
    return httpx.Response(status, text=text or "", headers=headers or {}, request=request)


def _rows(*originals):
    return CDX_HEADER + [[TIMESTAMP, original] for original in originals]


def _prepare(monkeypatch, responder):
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


def test_default_query_set_holds_the_bounded_archive_patterns():
    service = wayback_provider.WaybackService()

    assert service.DEFAULT_QUERY_TEMPLATES == (
        "api.telegram.org",
        "t.me/{username}",
        "*/raw/*",
        "*config*.json",
        "*env*",
    )
    assert len(service.DEFAULT_QUERY_TEMPLATES) <= 6
    assert service.default_queries() == [
        "api.telegram.org",
        "t.me/*",
        "*/raw/*",
        "*config*.json",
        "*env*",
    ]
    assert service.default_queries("prawnbot") == [
        "api.telegram.org",
        "t.me/prawnbot",
        "*/raw/*",
        "*config*.json",
        "*env*",
    ]


def test_per_run_request_cap_and_pacing_constants_are_unchanged():
    service = wayback_provider.WaybackService()

    assert service.MAX_REQUESTS == 500
    assert service.MAX_RESULTS == 500
    assert service.REQUEST_PAUSE >= 1.0


@pytest.mark.asyncio
async def test_default_search_runs_every_template_once(monkeypatch):
    def reply(url, _params, _attempt):
        return _response(payload=CDX_HEADER, url=url)

    _, http, service, _, _, _ = _prepare(monkeypatch, reply)

    await service.search(return_batch=True)

    cdx_params = [params for url, params in http.calls if CDX_URL in url]
    assert [params["url"] for params in cdx_params] == service.default_queries()


@pytest.mark.asyncio
async def test_default_queries_share_one_per_run_request_budget(monkeypatch):
    originals = [f"https://api.telegram.org/bot{10000000 + index}/sendMessage" for index in range(10)]

    def reply(url, _params, _attempt):
        if CDX_URL in url:
            return _response(payload=_rows(*originals), url=url)
        return _response(200, text="clean page", url=url)

    _, http, service, _, _, _ = _prepare(monkeypatch, reply)
    service.MAX_REQUESTS = 3

    await service.search(limit=500, return_batch=True)

    archive_calls = [url for url, _ in http.calls if CDX_URL not in url]
    assert len(archive_calls) == service.MAX_REQUESTS


@pytest.mark.asyncio
async def test_snapshot_fetches_wait_at_least_one_second_apart(monkeypatch):
    def reply(url, _params, _attempt):
        if CDX_URL in url:
            return _response(payload=_rows(ORIGINAL), url=url)
        return _response(200, text="clean page", url=url)

    _, _, service, _, _, sleeps = _prepare(monkeypatch, reply)

    await service.search(query_pattern="api.telegram.org", return_batch=True)

    assert sleeps == [service.REQUEST_PAUSE]
    assert min(sleeps) >= 1.0


@pytest.mark.asyncio
async def test_snapshot_content_is_swept_with_wayback_context(monkeypatch):
    body = f"export TELEGRAM_BOT_TOKEN={BODY_TOKEN}\nchat_id=-1001234567890"

    def reply(url, _params, _attempt):
        if CDX_URL in url:
            return _response(payload=_rows(CLEAN_ORIGINAL), url=url)
        return _response(200, text=body, url=url)

    _, _, service, _, _, _ = _prepare(monkeypatch, reply)

    batch = await service.search(query_pattern="api.telegram.org", return_batch=True)

    assert len(batch.matches) == 1
    match = batch.matches[0]
    assert match["token"] == BODY_TOKEN
    assert match["chat_id"] == "-1001234567890"
    assert BODY_TOKEN in match["context"]
    assert match["meta"] == {
        "wayback_url": CLEAN_ORIGINAL,
        "wayback_timestamp": TIMESTAMP,
        "extracted_from": "body",
        "source": "wayback",
    }


@pytest.mark.asyncio
async def test_url_token_is_marked_as_extracted_from_url(monkeypatch):
    def reply(url, _params, _attempt):
        if CDX_URL in url:
            return _response(payload=_rows(ORIGINAL), url=url)
        return _response(200, text="clean page", url=url)

    _, _, service, _, _, _ = _prepare(monkeypatch, reply)

    batch = await service.search(query_pattern="api.telegram.org", return_batch=True)

    assert [match["token"] for match in batch.matches] == [TOKEN]
    assert batch.matches[0]["meta"]["extracted_from"] == "url"
    assert batch.matches[0]["meta"]["wayback_url"] == ORIGINAL
    assert batch.matches[0]["meta"]["wayback_timestamp"] == TIMESTAMP


@pytest.mark.asyncio
async def test_cdx_429_backs_off_with_zero_enqueues_and_no_exception(monkeypatch, caplog):
    def reply(url, _params, _attempt):
        if CDX_URL in url:
            return _response(429, headers={"Retry-After": "2"}, url=url)
        raise AssertionError("no snapshot should be fetched after a blocked CDX reply")

    _, http, service, saved, _, sleeps = _prepare(monkeypatch, reply)
    caplog.set_level(logging.INFO)

    result = await wayback_task._scan_wayback_async()

    cdx_calls = [url for url, _ in http.calls if CDX_URL in url]
    assert len(cdx_calls) == 2
    assert sleeps == [2.0]
    assert saved == []
    assert "issue" in result
    assert "blocked or rate limited" in caplog.text
