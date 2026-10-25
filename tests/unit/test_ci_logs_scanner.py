import io
import logging
import zipfile

import httpx
import pytest

from app.core.config import settings
from app.infrastructure.scanners.providers import ci_logs as ci_logs_provider
from app.infrastructure.scanners.providers.ci_logs import CiLogsService

TOKEN_ONE = "12345678:" + "Z" + "a" * 34
TOKEN_TWO = "23456789:" + "Y" + "b" * 34
CHAT_ID = "-1001234567890"

RUNS_PATH = "/repos/acme/leaky/actions/runs"
LOGS_PATH = "/repos/acme/leaky/actions/runs/101/logs"
SIGNED_URL = "https://logs.example.invalid/101.zip"


class _HttpxProxy:
    """Give the provider a MockTransport-backed AsyncClient without global patches."""

    def __init__(self, handler):
        self.handler = handler

    def __getattr__(self, name):
        if name == "AsyncClient":
            real_client = httpx.AsyncClient

            def build(**kwargs):
                kwargs["transport"] = httpx.MockTransport(self.handler)
                return real_client(**kwargs)

            return build
        return getattr(httpx, name)


def _zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _json(request, payload, status=200):
    return httpx.Response(status, json=payload, request=request)


def _redirect(request, location):
    return httpx.Response(302, headers={"Location": location}, request=request)


def _body(request, content, status=200):
    return httpx.Response(status, content=content, request=request)


def _install(monkeypatch, handler, repos=None):
    monkeypatch.setattr(ci_logs_provider, "httpx", _HttpxProxy(handler))
    return CiLogsService(repos=["acme/leaky"] if repos is None else repos)


def _token_line(token, chat_id=CHAT_ID):
    return f'TELEGRAM_BOT_TOKEN = "{token}"\nchat_id = {chat_id}\n'.encode()


def _run(run_id=101):
    return {
        "id": run_id,
        "html_url": f"https://github.com/acme/leaky/actions/runs/{run_id}",
    }


@pytest.fixture(autouse=True)
def _single_token_pool(monkeypatch):
    monkeypatch.setattr(settings, "GITHUB_TOKEN", "test-github-token", raising=False)
    monkeypatch.setattr(settings, "GITHUB_TOKENS", "test-github-token", raising=False)


@pytest.mark.asyncio
async def test_happy_path_follows_redirect_and_extracts_txt_entries(monkeypatch):
    archive = _zip(
        {
            "build/1_build.txt": _token_line(TOKEN_ONE),
            "build/notes.md": f"leaked {TOKEN_TWO}".encode(),
        }
    )
    requests = []

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path == RUNS_PATH:
            assert request.url.params["status"] == "completed"
            assert request.url.params["per_page"] == "3"
            return _json(request, {"workflow_runs": [_run()]})
        if path == LOGS_PATH:
            assert request.headers["authorization"] == "token test-github-token"
            return _redirect(request, SIGNED_URL)
        if str(request.url) == SIGNED_URL:
            return _body(request, archive)
        return httpx.Response(404, request=request)

    service = _install(monkeypatch, handler)

    results = await service.search()

    assert results == [
        {
            "token": TOKEN_ONE,
            "meta": {
                "source": "ci_logs",
                "repo": "acme/leaky",
                "run_id": 101,
                "url": "https://github.com/acme/leaky/actions/runs/101",
                "chat_id": CHAT_ID,
            },
        }
    ]
    # The .md entry was never swept.
    assert all(TOKEN_TWO not in result["token"] for result in results)


@pytest.mark.asyncio
async def test_non_txt_entries_are_ignored(monkeypatch):
    archive = _zip(
        {
            "build/1_build.log": _token_line(TOKEN_ONE),
            "build/notes.md": f"leaked {TOKEN_TWO}".encode(),
        }
    )

    def handler(request):
        path = request.url.path
        if path == RUNS_PATH:
            return _json(request, {"workflow_runs": [_run()]})
        if path == LOGS_PATH:
            return _body(request, archive)
        return httpx.Response(404, request=request)

    service = _install(monkeypatch, handler)

    assert await service.search() == []


@pytest.mark.asyncio
async def test_zip_bomb_declared_uncompressed_size_is_rejected(monkeypatch, caplog):
    oversized = b"A" * (CiLogsService.MAX_UNCOMPRESSED_BYTES + 1)
    archive = _zip({"build/huge.txt": oversized})

    def handler(request):
        path = request.url.path
        if path == RUNS_PATH:
            return _json(request, {"workflow_runs": [_run()]})
        if path == LOGS_PATH:
            return _body(request, archive)
        return httpx.Response(404, request=request)

    service = _install(monkeypatch, handler)
    caplog.set_level(logging.WARNING, logger="scanners")

    assert await service.search() == []
    assert "uncompressed bytes" in caplog.text


@pytest.mark.asyncio
async def test_more_than_100_archive_entries_is_rejected(monkeypatch, caplog):
    files = {f"build/{index}.txt": b"scratch" for index in range(101)}
    archive = _zip(files)

    def handler(request):
        path = request.url.path
        if path == RUNS_PATH:
            return _json(request, {"workflow_runs": [_run()]})
        if path == LOGS_PATH:
            return _body(request, archive)
        return httpx.Response(404, request=request)

    service = _install(monkeypatch, handler)
    caplog.set_level(logging.WARNING, logger="scanners")

    assert await service.search() == []
    assert "entry cap" in caplog.text


@pytest.mark.asyncio
async def test_caps_log_lines_at_2000_per_repo(monkeypatch):
    lines = ["noise"] * 2500
    lines[1998] = f"TELEGRAM_BOT_TOKEN={TOKEN_ONE}"
    lines[2000] = f"TELEGRAM_BOT_TOKEN={TOKEN_TWO}"
    archive = _zip({"build/1_build.txt": "\n".join(lines).encode()})

    def handler(request):
        path = request.url.path
        if path == RUNS_PATH:
            return _json(request, {"workflow_runs": [_run()]})
        if path == LOGS_PATH:
            return _body(request, archive)
        return httpx.Response(404, request=request)

    service = _install(monkeypatch, handler)

    results = await service.search()

    assert [result["token"] for result in results] == [TOKEN_ONE]


@pytest.mark.asyncio
async def test_missing_token_skips_all_repos_without_crashing(
    monkeypatch, caplog
):
    monkeypatch.setattr(settings, "GITHUB_TOKEN", None, raising=False)
    monkeypatch.setattr(settings, "GITHUB_TOKENS", None, raising=False)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500, request=request)

    service = _install(monkeypatch, handler, repos=["acme/leaky", "acme/second"])
    caplog.set_level(logging.WARNING, logger="scanners")

    results = await service.search()

    assert results == []
    assert calls == []
    assert caplog.text.count("GitHub token is missing") == 1


@pytest.mark.asyncio
async def test_403_rate_limit_stops_with_partial_results(monkeypatch, caplog):
    archive = _zip({"build/1_build.txt": _token_line(TOKEN_ONE)})
    requested_repos = []

    def handler(request):
        path = request.url.path
        requested_repos.append(path)
        if path == RUNS_PATH:
            return _json(request, {"workflow_runs": [_run()]})
        if path == LOGS_PATH:
            return _body(request, archive)
        if path == "/repos/blocked/two/actions/runs":
            return httpx.Response(403, text="rate limit exceeded", request=request)
        if path == "/repos/never/three/actions/runs":
            raise AssertionError("must not request a repo after a rate limit")
        return httpx.Response(404, request=request)

    service = _install(monkeypatch, handler, repos=["acme/leaky", "blocked/two", "never/three"])
    caplog.set_level(logging.WARNING, logger="scanners")

    results = await service.search()

    assert [result["token"] for result in results] == [TOKEN_ONE]
    assert "rate limit" in caplog.text.lower()
    assert not any("never/three" in path for path in requested_repos)


@pytest.mark.asyncio
async def test_invalid_token_skips_repo_and_continues(monkeypatch, caplog):
    archive = _zip({"build/1_build.txt": _token_line(TOKEN_ONE)})

    def handler(request):
        path = request.url.path
        if path == "/repos/bad/one/actions/runs":
            return httpx.Response(401, text="Bad credentials", request=request)
        if path == RUNS_PATH:
            return _json(request, {"workflow_runs": [_run()]})
        if path == LOGS_PATH:
            return _body(request, archive)
        return httpx.Response(404, request=request)

    service = _install(monkeypatch, handler, repos=["bad/one", "acme/leaky"])
    caplog.set_level(logging.WARNING, logger="scanners")

    results = await service.search()

    assert [result["token"] for result in results] == [TOKEN_ONE]
    assert caplog.text.count("token was rejected") == 1


@pytest.mark.asyncio
async def test_explicit_query_is_a_single_repository(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/repos/other/project/actions/runs":
            return _json(request, {"workflow_runs": []})
        return httpx.Response(404, request=request)

    service = _install(monkeypatch, handler, repos=["acme/leaky"])

    assert await service.search("other/project") == []
    assert seen == ["https://api.github.com/repos/other/project/actions/runs?status=completed&per_page=3"]


@pytest.mark.asyncio
async def test_discovered_seeds_are_used_when_no_query_or_repos(monkeypatch):
    seen = []

    async def fake_seeds(self, table):
        return ["seeded/one"] if table == "validation_queue" else []

    def handler(request):
        seen.append(request.url.path)
        return _json(request, {"workflow_runs": []})

    monkeypatch.setattr(CiLogsService, "_repo_seeds_from", fake_seeds)
    service = _install(monkeypatch, handler, repos=[])

    assert await service.search() == []
    assert seen == ["/repos/seeded/one/actions/runs"]


@pytest.mark.asyncio
async def test_repository_list_is_capped_at_25(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return _json(request, {"workflow_runs": []})

    repos = [f"owner/repo{index}" for index in range(30)]
    service = _install(monkeypatch, handler, repos=repos)

    assert await service.search() == []
    assert len(seen) == CiLogsService.MAX_REPOS
    assert all("repo24" in path for path in seen[-1:])
