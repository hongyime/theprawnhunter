"""Unit tests for the PyPI scanner client (no network)."""

import io
import logging
import tarfile
import zipfile

import httpx
import pytest

from app.infrastructure.scanners.providers import pypi as pypi_provider
from app.infrastructure.scanners.providers.pypi import PypiService

SEARCH_URL = PypiService.SEARCH_URL
TOKEN_ONE = "1234567890:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh"
TOKEN_TWO = "2345678901:BBHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh"


def _snippet_html(*names: str) -> str:
    items = "".join(
        f'<li><a class="package-snippet" href="/project/{name}/">'
        f'<span class="package-snippet__name">{name}</span></a></li>'
        for name in names
    )
    return f"<html><body><ul>{items}</ul></body></html>"


def _sdist_name(name: str) -> str:
    return f"{name}-1.2.3.tar.gz"


def _sdist_url(name: str) -> str:
    return f"https://files.pythonhosted.org/packages/src/{_sdist_name(name)}"


def _make_tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for filename, data in files.items():
            info = tarfile.TarInfo(filename)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for filename, data in files.items():
            archive.writestr(filename, data)
    return buffer.getvalue()


def _json_body(name: str, *, version="1.2.3", size=None, wheel_only=False) -> dict:
    urls = []
    if not wheel_only:
        urls.append(
            {
                "packagetype": "sdist",
                "url": _sdist_url(name),
                "size": size if size is not None else 0,
            }
        )
    urls.append(
        {
            "packagetype": "bdist_wheel",
            "url": f"https://files.pythonhosted.org/packages/wheels/{name}-1.2.3-py3-none-any.whl",
        }
    )
    return {"info": {"version": version}, "urls": urls}


class _Response:
    def __init__(
        self,
        status_code: int = 200,
        *,
        text: str = "",
        json_data=None,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._json_data is None:
            raise ValueError("response has no JSON body")
        return self._json_data


class _Client:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.handler(url, kwargs)


class _Context:
    def __init__(self, client: _Client) -> None:
        self.client = client

    async def __aenter__(self) -> _Client:
        return self.client

    async def __aexit__(self, *_args) -> bool:
        return False


def _install(monkeypatch, *, html_by_query, packages, downloads):
    """Install a fake httpx.AsyncClient; return the recorded client."""

    def handler(url, kwargs):
        if url == SEARCH_URL:
            query = (kwargs.get("params") or {}).get("q")
            return _Response(200, text=html_by_query.get(query, ""))
        if url.startswith("https://pypi.org/pypi/") and url.endswith("/json"):
            name = url[len("https://pypi.org/pypi/") : -len("/json")]
            spec = packages.get(name)
            if spec is None:
                return _Response(404)
            if isinstance(spec, _Response):
                return spec
            return _Response(200, json_data=spec)
        spec = downloads.get(url)
        if spec is None:
            return _Response(404)
        if isinstance(spec, _Response):
            return spec
        return _Response(200, content=spec, headers={"content-length": str(len(spec))})

    client = _Client(handler)
    monkeypatch.setattr(pypi_provider.httpx, "AsyncClient", lambda **_kwargs: _Context(client))
    return client


@pytest.mark.asyncio
async def test_happy_path_extracts_tokens_from_tar_and_zip_sdists(monkeypatch):
    html = _snippet_html("telegram-leak", "wheel-only", "zipped-leak")
    tarball = _make_tarball(
        {
            "telegram_leak/settings.py": f"BOT_TOKEN = '{TOKEN_ONE}'\nchat_id=-1001234567890\n".encode(),
            "telegram_leak/blob.bin": b"\x00\x01\x02binary",
        }
    )
    zipped = _make_zip({"zipped_leak/config.py": f"token={TOKEN_TWO}".encode()})
    packages = {
        "telegram-leak": _json_body("telegram-leak", size=len(tarball)),
        "wheel-only": _json_body("wheel-only", wheel_only=True),
        "zipped-leak": _json_body("zipped-leak", version="0.9.0", size=len(zipped)),
    }
    downloads = {_sdist_url("telegram-leak"): tarball, _sdist_url("zipped-leak"): zipped}

    client = _install(
        monkeypatch,
        html_by_query={"telegram bot token": html},
        packages=packages,
        downloads=downloads,
    )

    results = await PypiService().search()

    by_token = {item["token"]: item for item in results}
    assert set(by_token) == {TOKEN_ONE, TOKEN_TWO}
    first = by_token[TOKEN_ONE]["meta"]
    assert first["source"] == "pypi"
    assert first["package"] == "telegram-leak"
    assert first["version"] == "1.2.3"
    assert first["url"] == _sdist_url("telegram-leak")
    assert first["chat_id"] == "-1001234567890"
    second = by_token[TOKEN_TWO]["meta"]
    assert second["package"] == "zipped-leak"
    assert second["version"] == "0.9.0"
    assert "chat_id" not in second
    # Wheels are never fetched: only the two sdist URLs plus the three JSON calls.
    sdist_calls = [
        url for url, _ in client.calls if url.startswith("https://files.pythonhosted.org/")
    ]
    assert sorted(sdist_calls) == sorted(downloads)


@pytest.mark.asyncio
async def test_changed_markup_returns_empty_and_warns_once_per_query(monkeypatch, caplog):
    changed = '<html><body><div class="something-else">redesigned</div></body></html>'
    client = _install(
        monkeypatch,
        html_by_query={query: changed for query in PypiService.QUERIES},
        packages={},
        downloads={},
    )
    caplog.set_level(logging.WARNING, logger="scanners")

    results = await PypiService().search()

    assert results == []
    assert [url for url, _ in client.calls] == [SEARCH_URL] * len(PypiService.QUERIES)
    assert caplog.text.count("markup may have changed") == len(PypiService.QUERIES)


@pytest.mark.asyncio
async def test_rate_limited_search_returns_empty_without_raising(monkeypatch, caplog):
    client = _Client(lambda _url, _kwargs: _Response(429))
    monkeypatch.setattr(pypi_provider.httpx, "AsyncClient", lambda **_kwargs: _Context(client))
    caplog.set_level(logging.WARNING, logger="scanners")

    results = await PypiService().search()

    assert results == []
    assert len(client.calls) == 1
    assert "Rate limited" in caplog.text


@pytest.mark.asyncio
async def test_timeout_returns_empty_without_raising(monkeypatch):
    def handler(url, kwargs):
        raise httpx.ConnectTimeout("pypi timed out")

    client = _Client(handler)
    monkeypatch.setattr(pypi_provider.httpx, "AsyncClient", lambda **_kwargs: _Context(client))

    assert await PypiService().search() == []
    assert len(client.calls) == len(PypiService.QUERIES)


@pytest.mark.asyncio
async def test_oversized_sdists_are_skipped(monkeypatch):
    html = _snippet_html("huge-hint", "huge-body")
    oversized_body = b"A" * (PypiService.MAX_SDIST_BYTES + 1)
    packages = {
        "huge-hint": _json_body("huge-hint", size=PypiService.MAX_SDIST_BYTES + 1),
        "huge-body": _json_body("huge-body", size=10),
    }
    downloads = {_sdist_url("huge-body"): oversized_body}
    client = _install(
        monkeypatch,
        html_by_query={"telegram bot token": html},
        packages=packages,
        downloads=downloads,
    )

    results = await PypiService().search()

    assert results == []
    sdist_calls = [
        url for url, _ in client.calls if url.startswith("https://files.pythonhosted.org/")
    ]
    # The size hint rejects huge-hint before any download; huge-body is fetched then rejected.
    assert sdist_calls == [_sdist_url("huge-body")]


@pytest.mark.asyncio
async def test_download_cap_is_respected(monkeypatch):
    first_names = [f"first-{index}" for index in range(10)]
    second_names = [f"second-{index}" for index in range(10)]
    tarball = _make_tarball({"pkg/leak.py": TOKEN_ONE.encode()})
    packages = {name: _json_body(name, size=len(tarball)) for name in first_names + second_names}
    downloads = {_sdist_url(name): tarball for name in first_names + second_names}
    monkeypatch.setattr(PypiService, "QUERIES", ["first query", "second query"])
    client = _install(
        monkeypatch,
        html_by_query={
            "first query": _snippet_html(*first_names),
            "second query": _snippet_html(*second_names),
        },
        packages=packages,
        downloads=downloads,
    )

    results = await PypiService().search()

    sdist_calls = [
        url for url, _ in client.calls if url.startswith("https://files.pythonhosted.org/")
    ]
    assert len(sdist_calls) == PypiService.MAX_SDISTS_PER_RUN
    assert len(results) == 1
    assert results[0]["token"] == TOKEN_ONE


@pytest.mark.asyncio
async def test_wheel_only_results_produce_no_download_and_no_tokens(monkeypatch):
    html = _snippet_html("wheel-only-a", "wheel-only-b")
    packages = {
        "wheel-only-a": _json_body("wheel-only-a", wheel_only=True),
        "wheel-only-b": _json_body("wheel-only-b", wheel_only=True),
    }
    client = _install(
        monkeypatch,
        html_by_query={"telegram bot token": html},
        packages=packages,
        downloads={},
    )

    assert await PypiService().search() == []
    assert not [url for url, _ in client.calls if url.startswith("https://files.pythonhosted.org/")]
