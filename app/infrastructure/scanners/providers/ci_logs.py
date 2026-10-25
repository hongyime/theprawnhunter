"""Scanner client for GitHub Actions CI logs.

Fetches the logs of the latest completed workflow runs for a bounded set of
repositories and sweeps the text entries for Telegram bot tokens. Everything is
processed in memory: archives are never extracted to disk and every archive is
checked against a zip-bomb guard before it is read.
"""

import io
import zipfile

from app.domain.tokens import sweep_text_for_tokens

from ..common import *
from .github import GithubService


class CiLogsService:
    """Scan bounded GitHub Actions logs for exposed Telegram bot tokens."""

    API_ROOT = "https://api.github.com"
    MAX_REPOS = 25
    MAX_RUNS_PER_REPO = 3
    MAX_LOG_LINES_PER_REPO = 2000
    MAX_COMPRESSED_BYTES = 20 * 1024 * 1024
    MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
    MAX_ARCHIVE_ENTRIES = 100
    SEED_ROW_LIMIT = 200
    REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

    def __init__(self, repos: list[str] | None = None):
        self.repos = list(repos or [])
        # Reuse the existing token pool (GITHUB_TOKENS / GITHUB_TOKEN) and its
        # rotation rather than reading the settings again here.
        self.github = GithubService()
        self.timeout = httpx.Timeout(60.0, connect=15.0)

    async def search(self, query: str | None = None) -> list[dict[str, Any]]:
        """Return candidate token dicts from the latest completed CI runs."""
        repos = await self._resolve_repos(query)
        if not repos:
            logger.info("[CI logs] No repositories to scan.")
            return []

        token = self.github._get_token()
        if not token:
            logger.warning(
                "[CI logs] GitHub token is missing; skipping %d repositories. "
                "Set GITHUB_TOKEN or GITHUB_TOKENS.",
                len(repos),
            )
            return []

        headers = self._headers(token)
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        invalid_token_logged = False

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            for repo in repos:
                repo_results, stop_reason = await self._scan_repo(
                    client, repo, headers, seen
                )
                results.extend(repo_results)

                if stop_reason == "invalid_token":
                    if not invalid_token_logged:
                        logger.warning(
                            "[CI logs] GitHub token was rejected (HTTP 401) for %s; "
                            "skipping repositories that reject it.",
                            repo,
                        )
                        invalid_token_logged = True
                    continue
                if stop_reason == "rate_limit":
                    logger.warning(
                        "[CI logs] GitHub rate limit reached; stopping this run "
                        "with %d matches.",
                        len(results),
                    )
                    break

        logger.info(
            "[CI logs] returned %d matches from %d repositories",
            len(results),
            len(repos),
        )
        return results

    async def _resolve_repos(self, query: str | None) -> list[str]:
        """Normalize explicit, configured, or discovered repository seeds."""
        if query:
            normalized = self._normalize_repo(query)
            if normalized:
                return [normalized]
            logger.warning(
                "[CI logs] Query %r is not a single owner/repo; ignoring it.",
                query,
            )

        candidates: list[str] = []
        for repo in self.repos:
            normalized = self._normalize_repo(repo)
            if normalized:
                candidates.append(normalized)
            else:
                logger.debug("[CI logs] Ignoring invalid repository seed %r.", repo)

        if not candidates:
            candidates.extend(await self._load_seed_repos())

        deduped: list[str] = []
        for repo in candidates:
            if repo not in deduped:
                deduped.append(repo)
        return deduped[: self.MAX_REPOS]

    @classmethod
    def _normalize_repo(cls, value: Any) -> str | None:
        """Return ``owner/repo`` for a well-formed value, else ``None``."""
        if not isinstance(value, str):
            return None
        repo = value.strip()
        if not repo:
            return None
        if repo.startswith(("https://github.com/", "http://github.com/")):
            repo = repo.split("github.com/", 1)[1]
        repo = repo.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        if not cls.REPO_PATTERN.fullmatch(repo):
            return None
        return repo

    async def _load_seed_repos(self) -> list[str]:
        """Read ``meta.repo`` seeds surfaced by earlier GitHub searches.

        ``validation_queue`` is the staging table for recent candidates, while
        ``discovered_credentials`` holds anything that already validated. Read
        both so a repo stays a seed even before its token is confirmed.
        """
        repos: list[str] = []
        for table in ("validation_queue", "discovered_credentials"):
            for repo in await self._repo_seeds_from(table):
                if repo not in repos:
                    repos.append(repo)
                if len(repos) >= self.MAX_REPOS:
                    return repos
        return repos

    async def _repo_seeds_from(self, table: str) -> list[str]:
        try:
            from app.core.database import db
            from app.infrastructure.database import execute_async

            response = await execute_async(
                db.table(table)
                .select("meta")
                .eq("source", "github")
                .order("created_at", desc=True)
                .limit(self.SEED_ROW_LIMIT)
            )
        except Exception as error:
            logger.debug(
                "[CI logs] Could not load repository seeds from %s (%s).",
                table,
                type(error).__name__,
            )
            return []

        repos: list[str] = []
        for row in getattr(response, "data", None) or []:
            meta = row.get("meta") if isinstance(row, dict) else None
            if not isinstance(meta, dict):
                continue
            normalized = self._normalize_repo(meta.get("repo") or meta.get("repository"))
            if normalized and normalized not in repos:
                repos.append(normalized)
        return repos

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        scheme = "Bearer" if token.startswith(("ghp_", "github_pat_")) else "token"
        return {
            "Authorization": f"{scheme} {token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _scan_repo(
        self,
        client: httpx.AsyncClient,
        repo: str,
        headers: dict[str, str],
        seen: set[tuple[str, str, str]],
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Scan up to three completed runs for one repository."""
        runs, stop_reason = await self._list_runs(client, repo, headers)
        if stop_reason:
            return [], stop_reason

        results: list[dict[str, Any]] = []
        lines_used = 0

        for run in runs[: self.MAX_RUNS_PER_REPO]:
            remaining = self.MAX_LOG_LINES_PER_REPO - lines_used
            if remaining <= 0:
                break

            run_id = run.get("id")
            if run_id is None:
                continue

            body, stop_reason = await self._download_logs(
                client, repo, run_id, headers
            )
            if stop_reason:
                return results, stop_reason
            if body is None:
                continue

            text, extracted_lines = self._extract_text(body, remaining)
            lines_used += extracted_lines
            if not text:
                continue

            html_url = run.get("html_url") or (
                f"https://github.com/{repo}/actions/runs/{run_id}"
            )
            for hit in sweep_text_for_tokens(text, "ci_logs"):
                key = (hit["token"], repo, str(run_id))
                if key in seen:
                    continue
                seen.add(key)

                meta: dict[str, Any] = {
                    "source": "ci_logs",
                    "repo": repo,
                    "run_id": run_id,
                    "url": html_url,
                }
                if hit.get("chat_id"):
                    meta["chat_id"] = hit["chat_id"]
                results.append({"token": hit["token"], "meta": meta})

        return results, None

    async def _list_runs(
        self, client: httpx.AsyncClient, repo: str, headers: dict[str, str]
    ) -> tuple[list[dict[str, Any]], str | None]:
        url = f"{self.API_ROOT}/repos/{repo}/actions/runs"
        try:
            response = await client.get(
                url,
                headers=headers,
                params={"status": "completed", "per_page": self.MAX_RUNS_PER_REPO},
            )
        except (httpx.TimeoutException, httpx.TransportError) as error:
            logger.warning(
                "[CI logs] Could not list runs for %s (%s); skipping this repository.",
                repo,
                error,
            )
            return [], None

        if response.status_code in (403, 429):
            return [], "rate_limit"
        if response.status_code == 401:
            return [], "invalid_token"
        if response.status_code != 200:
            logger.debug(
                "[CI logs] %s returned HTTP %s when listing runs; skipping it.",
                repo,
                response.status_code,
            )
            return [], None

        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            logger.warning(
                "[CI logs] %s returned unreadable run JSON (%s); skipping it.",
                repo,
                error,
            )
            return [], None

        runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
        if not isinstance(runs, list):
            return [], None
        return [run for run in runs if isinstance(run, dict)], None

    async def _download_logs(
        self,
        client: httpx.AsyncClient,
        repo: str,
        run_id: Any,
        headers: dict[str, str],
    ) -> tuple[bytes | None, str | None]:
        url = f"{self.API_ROOT}/repos/{repo}/actions/runs/{run_id}/logs"
        try:
            response = await client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as error:
            logger.warning(
                "[CI logs] Could not download logs for %s run %s (%s); skipping it.",
                repo,
                run_id,
                error,
            )
            return None, None

        if response.status_code in (403, 429):
            return None, "rate_limit"
        if response.status_code == 401:
            return None, "invalid_token"
        if response.status_code != 200:
            logger.debug(
                "[CI logs] Logs for %s run %s returned HTTP %s; skipping it.",
                repo,
                run_id,
                response.status_code,
            )
            return None, None

        body = response.content
        if len(body) > self.MAX_COMPRESSED_BYTES:
            logger.warning(
                "[CI logs] Logs for %s run %s are %d compressed bytes, above the "
                "%d byte cap; skipping the archive.",
                repo,
                run_id,
                len(body),
                self.MAX_COMPRESSED_BYTES,
            )
            return None, None
        return body, None

    def _extract_text(self, body: bytes, remaining: int) -> tuple[str, int]:
        """Extract ``.txt`` entries from a logs zip, up to ``remaining`` lines."""
        if len(body) > self.MAX_COMPRESSED_BYTES:
            logger.warning(
                "[CI logs] Archive is %d compressed bytes, above the %d byte cap; "
                "skipping it.",
                len(body),
                self.MAX_COMPRESSED_BYTES,
            )
            return "", 0

        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                entries = archive.infolist()
                if len(entries) > self.MAX_ARCHIVE_ENTRIES:
                    logger.warning(
                        "[CI logs] Archive has %d entries, above the %d entry cap; "
                        "skipping it.",
                        len(entries),
                        self.MAX_ARCHIVE_ENTRIES,
                    )
                    return "", 0

                declared_size = sum(info.file_size for info in entries)
                if declared_size > self.MAX_UNCOMPRESSED_BYTES:
                    logger.warning(
                        "[CI logs] Archive declares %d uncompressed bytes, above the "
                        "%d byte cap; skipping it.",
                        declared_size,
                        self.MAX_UNCOMPRESSED_BYTES,
                    )
                    return "", 0

                texts: list[str] = []
                used_lines = 0
                for info in entries:
                    if remaining <= 0:
                        break
                    if info.is_dir() or not info.filename.lower().endswith(".txt"):
                        continue

                    data = archive.read(info)
                    if len(data) > self.MAX_UNCOMPRESSED_BYTES:
                        logger.warning(
                            "[CI logs] Entry %s is larger than the %d byte cap; "
                            "skipping it.",
                            info.filename,
                            self.MAX_UNCOMPRESSED_BYTES,
                        )
                        continue

                    entry_lines = data.decode("utf-8", errors="replace").splitlines()
                    if len(entry_lines) > remaining:
                        entry_lines = entry_lines[:remaining]
                    texts.append("\n".join(entry_lines))
                    used_lines += len(entry_lines)
                    remaining -= len(entry_lines)
        except Exception as error:
            logger.warning(
                "[CI logs] Could not read a logs archive (%s); skipping it.", error
            )
            return "", 0

        return "\n".join(texts), used_lines
