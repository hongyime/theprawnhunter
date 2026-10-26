import io
import logging
import tarfile

import httpx
import pytest

from app.infrastructure.scanners.providers import npm as npm_provider
from app.infrastructure.scanners.providers.npm import NpmService

TOKEN_ONE = "1234567890:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh"
TOKEN_TWO = "9876543210:BBHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh"
CHAT_ID = -1001234567890

FIRST_TARBALL = "https://registry.npmjs.org/pkg-one/-/pkg-one-1.0.0.tgz"
SECOND_TARBALL = "https://registry.npmjs.org/pkg-two/-/pkg-two-2.0.0.tgz"


class _HttpxProxy:
    """Give the provider a MockTransport-backed AsyncClient without global patches."""

    def __init__(self, handler):
        self.handler = handler

    def __getattr__(self, name):
        if name == "AsyncClient":
            real_client = httpx.AsyncClient

            def build(**kwargs):
                return real_client(transport=httpx.MockTransport(self.handler), **kwargs)

            return build
        return getattr(httpx, name)


def _package(name, version, tarball):
    return {"name": name, "version": version, "links": {"tarball": tarball}}


def _search_reply(packages):
    return httpx.Response(200, json={"objects": [{"package": package} for package in packages]})


def _tarball(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _with_token(token, chat_id=None):
    lines = [f'TELEGRAM_BOT_TOKEN = "{token}"']
    if chat_id is not None:
        lines.append(f"chat_id = {chat_id}")
    return "\n".join(lines).encode()


def _install(monkeypatch, handler):
    monkeypatch.setattr(npm_provider, "httpx", _HttpxProxy(handler))
    return NpmService()


def _base_url(request):
    return str(request.url).split("?", 1)[0]


@pytest.mark.asyncio
async def test_happy_path_returns_candidates_from_tarball(monkeypatch):
    tarball_calls = []
    package_tarball = _tarball(
        {
            "package/index.js": _with_token(TOKEN_ONE, CHAT_ID),
            "package/assets/logo.bin": b"\x00\x01\x02" + TOKEN_TWO.encode(),
        }
    )

    def handler(request):
        if _base_url(request) == NpmService.SEARCH_URL:
            return _search_reply([_package("pkg-one", "1.0.0", FIRST_TARBALL)])
        if str(request.url) == FIRST_TARBALL:
            tarball_calls.append(str(request.url))
            return httpx.Response(200, content=package_tarball)
        return httpx.Response(404)

    service = _install(monkeypatch, handler)

    results = await service.search("telegram bot token")

    assert tarball_calls == [FIRST_TARBALL]
    assert results == [
        {
            "token": TOKEN_ONE,
            "meta": {
                "source": "npm",
                "package": "pkg-one",
                "version": "1.0.0",
                "url": FIRST_TARBALL,
                "chat_id": str(CHAT_ID),
            },
        }
    ]


@pytest.mark.asyncio
async def test_http_429_returns_empty_without_raising(monkeypatch, caplog):
    tarball_calls = []

    def handler(request):
        if _base_url(request) == NpmService.SEARCH_URL:
            return httpx.Response(429, text="slow down")
        tarball_calls.append(str(request.url))
        return httpx.Response(200)

    service = _install(monkeypatch, handler)
    caplog.set_level(logging.WARNING, logger="scanners")

    results = await service.search()

    assert results == []
    assert tarball_calls == []
    assert "rate limited" in caplog.text
    assert "Stopping this run" in caplog.text


@pytest.mark.asyncio
async def test_timeout_returns_empty_without_raising(monkeypatch, caplog):
    def handler(request):
        raise httpx.ConnectError("registry unreachable", request=request)

    service = _install(monkeypatch, handler)
    caplog.set_level(logging.WARNING, logger="scanners")

    results = await service.search()

    assert results == []
    assert "search" in caplog.text and "Stopping this run" in caplog.text


@pytest.mark.asyncio
async def test_tarball_timeout_stops_further_fetches(monkeypatch, caplog):
    calls = []

    def handler(request):
        if _base_url(request) == NpmService.SEARCH_URL:
            return _search_reply(
                [
                    _package("pkg-one", "1.0.0", FIRST_TARBALL),
                    _package("pkg-two", "2.0.0", SECOND_TARBALL),
                ]
            )
        calls.append(str(request.url))
        raise httpx.ReadTimeout("tarball too slow", request=request)

    service = _install(monkeypatch, handler)
    caplog.set_level(logging.WARNING, logger="scanners")

    results = await service.search("telegram bot token")

    assert results == []
    assert calls == [FIRST_TARBALL]
    assert "tarball fetch failed" in caplog.text


@pytest.mark.parametrize("oversized", ["body", "content-length"])
@pytest.mark.asyncio
async def test_oversized_tarball_is_skipped(monkeypatch, caplog, oversized):
    good_tarball = _tarball({"package/index.js": _with_token(TOKEN_ONE)})

    def handler(request):
        if _base_url(request) == NpmService.SEARCH_URL:
            return _search_reply(
                [
                    _package("pkg-one", "1.0.0", FIRST_TARBALL),
                    _package("pkg-two", "2.0.0", SECOND_TARBALL),
                ]
            )
        if str(request.url) == FIRST_TARBALL:
            if oversized == "content-length":
                return httpx.Response(
                    200,
                    content=b"small",
                    headers={"Content-Length": str(NpmService.MAX_TARBALL_BYTES + 1)},
                )
            return httpx.Response(200, content=b"x" * (NpmService.MAX_TARBALL_BYTES + 1))
        if str(request.url) == SECOND_TARBALL:
            return httpx.Response(200, content=good_tarball)
        return httpx.Response(404)

    service = _install(monkeypatch, handler)
    caplog.set_level(logging.WARNING, logger="scanners")

    results = await service.search("telegram bot token")

    assert [result["token"] for result in results] == [TOKEN_ONE]
    assert "above the" in caplog.text


@pytest.mark.asyncio
async def test_per_run_cap_is_enforced(monkeypatch):
    tarball_calls = []
    packages = [
        _package(
            f"pkg-{index}",
            "1.0.0",
            f"https://registry.npmjs.org/pkg-{index}/-/pkg-{index}-1.0.0.tgz",
        )
        for index in range(NpmService.MAX_TARBALLS_PER_RUN + 1)
    ]
    empty_tarball = _tarball({})

    def handler(request):
        if _base_url(request) == NpmService.SEARCH_URL:
            return _search_reply(packages)
        tarball_calls.append(str(request.url))
        return httpx.Response(200, content=empty_tarball)

    service = _install(monkeypatch, handler)

    results = await service.search("telegram bot token")

    assert results == []
    assert len(tarball_calls) == NpmService.MAX_TARBALLS_PER_RUN
    assert tarball_calls[-1] == packages[-2]["links"]["tarball"]


@pytest.mark.asyncio
async def test_duplicate_tokens_across_packages_are_deduped(monkeypatch):
    same_token_tarball = _tarball({"package/index.js": _with_token(TOKEN_ONE)})

    def handler(request):
        if _base_url(request) == NpmService.SEARCH_URL:
            return _search_reply(
                [
                    _package("pkg-one", "1.0.0", FIRST_TARBALL),
                    _package("pkg-two", "2.0.0", SECOND_TARBALL),
                ]
            )
        return httpx.Response(200, content=same_token_tarball)

    service = _install(monkeypatch, handler)

    results = await service.search("telegram bot token")

    assert len(results) == 1
    assert results[0]["token"] == TOKEN_ONE
    assert results[0]["meta"]["package"] == "pkg-one"


@pytest.mark.asyncio
async def test_default_query_set_is_bounded_and_used(monkeypatch):
    queries = []

    def handler(request):
        if _base_url(request) == NpmService.SEARCH_URL:
            text = request.url.params.get("text")
            queries.append(text)
            slug = text.replace(" ", "-").replace(".", "-").replace("_", "-")
            return _search_reply(
                [
                    _package(
                        f"pkg-{slug}",
                        "1.0.0",
                        f"https://registry.npmjs.org/pkg-{slug}/-/pkg-{slug}-1.0.0.tgz",
                    )
                ]
            )
        return httpx.Response(200, content=_tarball({}))

    service = _install(monkeypatch, handler)

    results = await service.search()

    assert results == []
    assert 3 <= len(NpmService.QUERIES) <= 8
    assert queries == list(NpmService.QUERIES)


@pytest.mark.asyncio
async def test_resolves_tarball_from_package_metadata_when_search_omits_links(monkeypatch):
    """The public search API stopped returning links.tarball; resolve it via metadata."""
    tarball = _tarball({"package/index.js": _with_token(TOKEN_ONE, CHAT_ID)})
    metadata_calls = []

    def handler(request):
        url = str(request.url)
        if url.startswith("https://registry.npmjs.org/-/v1/search"):
            return _search_reply(
                [{"name": "pkg-one", "version": "1.0.0"}]  # no "links" key
            )
        if url.startswith("https://registry.npmjs.org/pkg-one/1.0.0"):
            metadata_calls.append(url)
            return httpx.Response(
                200, json={"dist": {"tarball": FIRST_TARBALL}}
            )
        if url == FIRST_TARBALL:
            return httpx.Response(200, content=tarball)
        return httpx.Response(404)

    service = _install(monkeypatch, handler)
    results = await service.search()

    assert metadata_calls, "metadata endpoint should be consulted"
    assert [row["token"] for row in results] == [TOKEN_ONE]
    assert results[0]["meta"]["package"] == "pkg-one"
