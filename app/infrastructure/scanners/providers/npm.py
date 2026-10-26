"""Scanner client for the npm registry."""

import io
import tarfile

from ..common import *


class NpmService:
    """Search the public npm registry and sweep package tarballs for tokens.

    The registry is key-free. Every tarball is processed in memory: nothing is
    written to disk and ``tarfile`` member paths are never used as filesystem
    paths, so path-traversal entries are inert.
    """

    SEARCH_URL = "https://registry.npmjs.org/-/v1/search"

    QUERIES = [
        "telegram bot token",
        "TELEGRAM_BOT_TOKEN",
        "api.telegram.org/bot",
        "telegram bot",
        "tgbot",
        "telegram bot api",
    ]

    MAX_TARBALLS_PER_RUN = 50
    MAX_TARBALL_BYTES = 2_000_000
    SEARCH_RESULTS_SIZE = 20

    def __init__(self):
        self.timeout = httpx.Timeout(20.0, connect=10.0)

    async def search(self, query: str | None = None) -> list[dict[str, Any]]:
        """Return candidate token dicts for the given query or the bounded query set."""
        from app.domain.tokens import sweep_text_for_tokens

        results: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        seen_tarballs: set[str] = set()
        fetched = 0
        queries = [query] if query else list(self.QUERIES)
        stop = False

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            for search_query in queries:
                if stop or fetched >= self.MAX_TARBALLS_PER_RUN:
                    break

                packages, stop = await self._search_packages(client, search_query)
                if stop:
                    break

                for package in packages:
                    if fetched >= self.MAX_TARBALLS_PER_RUN:
                        break

                    tarball_url = await self._resolve_tarball_url(client, package)
                    if not tarball_url or tarball_url in seen_tarballs:
                        continue

                    seen_tarballs.add(tarball_url)
                    fetched += 1

                    body, stop = await self._fetch_tarball(client, tarball_url)
                    if stop:
                        break
                    if body is None:
                        continue

                    name = str(package.get("name") or "")
                    version = str(package.get("version") or "")
                    for text in self._extract_text_members(body):
                        hits = sweep_text_for_tokens(
                            text, "npm", max_bytes=self.MAX_TARBALL_BYTES
                        )
                        for hit in hits:
                            token = hit["token"]
                            if token in seen_tokens:
                                continue
                            seen_tokens.add(token)

                            meta: dict[str, Any] = {
                                "source": "npm",
                                "package": name,
                                "version": version,
                                "url": tarball_url,
                            }
                            if hit.get("chat_id"):
                                meta["chat_id"] = hit["chat_id"]
                            results.append({"token": token, "meta": meta})

                if stop:
                    break

        logger.info(f"[npm] returned {len(results)} matches")
        return results

    @staticmethod
    def _tarball_url(package: dict[str, Any]) -> str | None:
        links = package.get("links")
        if not isinstance(links, dict):
            return None
        url = links.get("tarball")
        if isinstance(url, str) and url:
            return url
        return None

    async def _resolve_tarball_url(
        self, client: httpx.AsyncClient, package: dict[str, Any]
    ) -> str | None:
        direct = self._tarball_url(package)
        if direct:
            return direct
        name = str(package.get("name") or "").strip()
        if not name:
            return None
        version = str(package.get("version") or "").strip()
        url = (
            f"https://registry.npmjs.org/{name}/{version}"
            if version
            else f"https://registry.npmjs.org/{name}/latest"
        )
        try:
            response = await client.get(url)
            if response.status_code != 200:
                return None
            dist = response.json().get("dist") or {}
            tarball = dist.get("tarball")
            return tarball if isinstance(tarball, str) and tarball else None
        except Exception as error:  # noqa: BLE001 - one package must not stop the run
            logger.debug(f"[npm] tarball lookup failed for {name}: {error}")
            return None

    async def _search_packages(
        self, client, query: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return package records for one query and whether the run must stop."""
        try:
            response = await client.get(
                self.SEARCH_URL,
                params={"text": query, "size": self.SEARCH_RESULTS_SIZE},
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            logger.warning(
                "[npm] search %r failed: %s. Stopping this run; the remaining queries run next scan.",
                query,
                error,
            )
            return [], True

        if response.status_code == 429:
            logger.warning(
                "[npm] search %r was rate limited (HTTP 429). Stopping this run; the remaining queries run next scan.",
                query,
            )
            return [], True

        if response.status_code != 200:
            logger.warning(
                "[npm] search %r returned HTTP %s. Continuing with the next query.",
                query,
                response.status_code,
            )
            return [], False

        try:
            objects = response.json().get("objects") or []
        except (TypeError, ValueError) as error:
            logger.warning(
                "[npm] search %r returned unreadable JSON: %s. Continuing with the next query.",
                query,
                error,
            )
            return [], False

        packages = []
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            package = obj.get("package")
            if isinstance(package, dict):
                packages.append(package)
        return packages, False

    async def _fetch_tarball(self, client, url: str) -> tuple[bytes | None, bool]:
        """Download one tarball within the size cap and report whether to stop."""
        try:
            response = await client.get(url)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            logger.warning(
                "[npm] tarball fetch failed: %s. Stopping this run; the remaining packages run next scan.",
                error,
            )
            return None, True

        if response.status_code == 429:
            logger.warning(
                "[npm] tarball fetch was rate limited (HTTP 429). Stopping this run; the remaining packages run next scan."
            )
            return None, True

        if response.status_code != 200:
            logger.debug(
                "[npm] tarball %s returned HTTP %s. Skipping it.",
                url,
                response.status_code,
            )
            return None, False

        declared_length = response.headers.get("Content-Length")
        if declared_length:
            try:
                if int(declared_length) > self.MAX_TARBALL_BYTES:
                    logger.warning(
                        "[npm] tarball %s declares %s bytes, above the %s byte cap. Skipping it.",
                        url,
                        declared_length,
                        self.MAX_TARBALL_BYTES,
                    )
                    return None, False
            except ValueError:
                logger.debug("[npm] tarball %s has an unreadable Content-Length.", url)

        body = response.content
        if len(body) > self.MAX_TARBALL_BYTES:
            logger.warning(
                "[npm] tarball %s is %s bytes, above the %s byte cap. Skipping it.",
                url,
                len(body),
                self.MAX_TARBALL_BYTES,
            )
            return None, False

        return body, False

    def _extract_text_members(self, body: bytes) -> list[str]:
        """Decode text members of a gzipped tarball without touching the filesystem."""
        texts: list[str] = []
        try:
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
                for member in archive:
                    if not member.isfile():
                        continue
                    if member.size > self.MAX_TARBALL_BYTES:
                        continue
                    handle = archive.extractfile(member)
                    if handle is None:
                        continue
                    data = handle.read(self.MAX_TARBALL_BYTES + 1)
                    if len(data) > self.MAX_TARBALL_BYTES:
                        continue
                    if b"\x00" in data:
                        continue
                    try:
                        texts.append(data.decode("utf-8"))
                    except UnicodeDecodeError:
                        continue
        except (tarfile.TarError, OSError, EOFError) as error:
            logger.warning("[npm] could not read a package tarball: %s. Skipping it.", error)
            return []
        return texts
