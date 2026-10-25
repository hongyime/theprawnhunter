"""Scanner client for the PyPI package index."""

from ..common import *  # noqa: I001 — house style: shared scanner imports first

import io
import tarfile
import zipfile
from html.parser import HTMLParser

from app.domain.tokens import sweep_text_for_tokens

MAX_SDISTS_PER_RUN = 15
MAX_SDIST_BYTES = 5_000_000
SEARCH_RESULTS_PER_QUERY = 10


class _PackageSnippetParser(HTMLParser):
    """Collect PyPI package names from search-result snippet anchors."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.names: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if "package-snippet" not in classes:
            return
        href = attributes.get("href") or ""
        match = re.match(r"^(?:https?://pypi\.org)?/project/([^/?#]+)/?", href)
        if match:
            from urllib.parse import unquote

            self.names.append(unquote(match.group(1)))


class PypiService:
    """Scanner client for the PyPI package index.

    Scrapes the public search page, resolves each package's source
    distribution through the JSON API, and sweeps the archive text for
    Telegram bot tokens. Requires no API key.
    """

    SEARCH_URL = "https://pypi.org/search/"
    QUERIES = [
        "telegram bot token",
        "api.telegram.org/bot",
        "TELEGRAM_BOT_TOKEN",
    ]
    MAX_SDISTS_PER_RUN = MAX_SDISTS_PER_RUN
    MAX_SDIST_BYTES = MAX_SDIST_BYTES
    SEARCH_RESULTS_PER_QUERY = SEARCH_RESULTS_PER_QUERY

    def __init__(self):
        self.timeout = httpx.Timeout(20.0, connect=10.0)

    @staticmethod
    def _parse_package_names(html_text: str) -> list[str]:
        """Return package names from search-result markup; [] when it changes."""
        try:
            parser = _PackageSnippetParser()
            parser.feed(html_text or "")
            parser.close()
            return parser.names
        except Exception as error:
            logger.debug("[PyPI] Search markup could not be parsed: %s", error)
            return []

    @staticmethod
    def _select_sdist(payload: Any) -> tuple[str, str, int | None] | None:
        """Return (version, sdist_url, size) for the package's source dist."""
        if not isinstance(payload, dict):
            return None
        info = payload.get("info")
        version = str(info.get("version") or "") if isinstance(info, dict) else ""
        for entry in payload.get("urls") or []:
            if not isinstance(entry, dict) or entry.get("packagetype") != "sdist":
                continue
            url = entry.get("url")
            if not isinstance(url, str) or not url:
                continue
            size = entry.get("size")
            try:
                size_value = int(size) if size is not None else None
            except (TypeError, ValueError):
                size_value = None
            return version, url, size_value
        return None

    @staticmethod
    def _read_member(data: bytes) -> str:
        if not data:
            return ""
        if b"\x00" in data[:4096]:
            return ""
        return data.decode("utf-8", errors="ignore")

    def _extract_text(self, payload: bytes, url: str) -> str:
        """Extract decoded text from a tar/zip sdist, entirely in memory."""
        chunks: list[str] = []
        total = 0
        buffer = io.BytesIO(payload)
        try:
            if zipfile.is_zipfile(buffer):
                buffer.seek(0)
                with zipfile.ZipFile(buffer) as archive:
                    for info in archive.infolist():
                        if info.is_dir() or info.file_size <= 0:
                            continue
                        with archive.open(info) as handle:
                            text = self._read_member(handle.read())
                        chunks.append(text)
                        total += len(text)
                        if total >= self.MAX_SDIST_BYTES:
                            break
            else:
                buffer.seek(0)
                with tarfile.open(fileobj=buffer, mode="r:*") as archive:
                    for member in archive.getmembers():
                        if not member.isfile() or member.size <= 0:
                            continue
                        handle = archive.extractfile(member)
                        if handle is None:
                            continue
                        with handle:
                            text = self._read_member(handle.read())
                        chunks.append(text)
                        total += len(text)
                        if total >= self.MAX_SDIST_BYTES:
                            break
        except Exception as error:
            logger.warning("[PyPI] Could not read sdist %s: %s", url, error)
            return ""
        return "\n".join(chunk for chunk in chunks if chunk)

    async def _scan_package(self, client, name: str) -> tuple[list[dict[str, Any]], bool, bool]:
        """Return (results, stop_run, downloaded) for one package."""
        json_url = f"https://pypi.org/pypi/{name}/json"
        try:
            response = await client.get(json_url)
        except Exception as error:
            logger.warning("[PyPI] Could not resolve package %r: %s", name, error)
            return [], False, False

        if response.status_code == 429:
            logger.warning("[PyPI] Rate limited while resolving %r; stopping this run.", name)
            return [], True, False
        if response.status_code != 200:
            logger.warning(
                "[PyPI] Package %r metadata returned HTTP %s.", name, response.status_code
            )
            return [], False, False

        try:
            payload = response.json()
        except Exception as error:
            logger.warning("[PyPI] Package %r returned unusable metadata: %s", name, error)
            return [], False, False

        selected = self._select_sdist(payload)
        if selected is None:
            return [], False, False
        version, sdist_url, size = selected

        if size is not None and size > self.MAX_SDIST_BYTES:
            logger.debug(
                "[PyPI] Skipping %r sdist of %s bytes (cap %s).",
                name,
                size,
                self.MAX_SDIST_BYTES,
            )
            return [], False, False

        try:
            download = await client.get(sdist_url)
        except Exception as error:
            logger.warning("[PyPI] Could not download sdist for %r: %s", name, error)
            return [], False, False

        if download.status_code == 429:
            logger.warning("[PyPI] Rate limited while downloading %r; stopping this run.", name)
            return [], True, False
        if download.status_code != 200:
            logger.warning("[PyPI] Sdist for %r returned HTTP %s.", name, download.status_code)
            return [], False, False

        headers = getattr(download, "headers", {}) or {}
        declared = headers.get("content-length") if hasattr(headers, "get") else None
        if declared is not None:
            try:
                if int(declared) > self.MAX_SDIST_BYTES:
                    logger.debug("[PyPI] Skipping %r sdist with oversized Content-Length.", name)
                    return [], False, True
            except (TypeError, ValueError):
                pass

        body = download.content or b""
        if len(body) > self.MAX_SDIST_BYTES:
            logger.debug("[PyPI] Skipping %r sdist body over the size cap.", name)
            return [], False, True

        text = self._extract_text(body, sdist_url)
        hits = sweep_text_for_tokens(text, "pypi")
        results: list[dict[str, Any]] = []
        for hit in hits:
            meta: dict[str, Any] = {
                "source": "pypi",
                "package": name,
                "version": version,
                "url": sdist_url,
            }
            if hit.get("chat_id"):
                meta["chat_id"] = hit["chat_id"]
            results.append({"token": hit["token"], "meta": meta})
        return results, False, True

    async def search(self, query: str | None = None) -> list[dict[str, Any]]:
        """Search PyPI for exposed Telegram bot tokens."""
        results: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        seen_packages: set[str] = set()
        downloads = 0
        queries = [query] if query else self.QUERIES

        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                for search_query in queries:
                    try:
                        response = await client.get(self.SEARCH_URL, params={"q": search_query})
                    except Exception as error:
                        logger.warning(
                            "[PyPI] Search %r failed: %s; skipping it.", search_query, error
                        )
                        continue

                    if response.status_code == 429:
                        logger.warning(
                            "[PyPI] Rate limited on %r; stopping this run.", search_query
                        )
                        return results
                    if response.status_code != 200:
                        logger.warning(
                            "[PyPI] Search %r returned HTTP %s; skipping it.",
                            search_query,
                            response.status_code,
                        )
                        continue

                    names = self._parse_package_names(response.text)[
                        : self.SEARCH_RESULTS_PER_QUERY
                    ]
                    if not names:
                        logger.warning(
                            "[PyPI] Search %r returned no package snippets; "
                            "the markup may have changed.",
                            search_query,
                        )
                        continue

                    for name in names:
                        if name in seen_packages:
                            continue
                        seen_packages.add(name)
                        if downloads >= self.MAX_SDISTS_PER_RUN:
                            break
                        package_results, stop_run, downloaded = await self._scan_package(
                            client, name
                        )
                        if downloaded:
                            downloads += 1
                        if stop_run:
                            return results
                        if package_results:
                            for item in package_results:
                                token = item["token"]
                                if token in seen_tokens:
                                    continue
                                seen_tokens.add(token)
                                results.append(item)
                    if downloads >= self.MAX_SDISTS_PER_RUN:
                        break
        except Exception as error:
            logger.warning("[PyPI] Scan failed unexpectedly: %s", error)
        return results
