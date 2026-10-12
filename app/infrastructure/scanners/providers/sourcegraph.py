"""Scanner client for Sourcegraph."""

from ..common import *

class SourcegraphService:
    """
    Sourcegraph public code search — free, no auth, no key.

    Endpoint: https://sourcegraph.com/.api/search/stream
    Format: Server-Sent Events (SSE). Stream lines like 'event: matches\ndata: [...]\n\n'.
    Index size: ~91k repos containing 'api.telegram.org'.
    Rate limit: undocumented; we use 5s between queries as courtesy.

    Replaces the abandoned Replit attempt — Replit's GraphQL now requires
    persisted query hashes (anti-scraping), Sourcegraph remains open.
    """

    STREAM_URL = "https://sourcegraph.com/.api/search/stream"

    QUERIES = [
        "api.telegram.org/bot count:200",
        "TELEGRAM_BOT_TOKEN count:200",
        "TELEGRAM_TOKEN= count:200",
    ]

    def __init__(self):
        self.timeout = httpx.Timeout(45.0, connect=10.0)
        self.dedupe_ttl = 14 * 86400  # 14 days

    async def search(self, query: str = None) -> list[dict[str, Any]]:
        from app.core.redis_srv import redis_srv as _redis
        results: list[dict[str, Any]] = []
        queries = [query] if query else list(self.QUERIES)

        async with get_async_http_client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "TheprawnHunter/1.0 (+research)"},
        ) as client:
            for q in queries:
                try:
                    # SSE stream: query the stream endpoint, parse 'data:' lines
                    r = await client.get(self.STREAM_URL, params={"q": q})
                    if r.status_code != 200:
                        logger.debug(f"[Sourcegraph] q='{q[:30]}' HTTP {r.status_code}")
                        continue
                    body = r.text or ""
                except Exception as e:
                    logger.warning(f"[Sourcegraph] stream failed: {e}")
                    continue

                # SSE parsing — events come as 'event: <type>\ndata: <json>\n\n'
                for chunk in body.split("\n\n"):
                    lines = chunk.split("\n")
                    event_type = None
                    data_str = None
                    for line in lines:
                        if line.startswith("event: "):
                            event_type = line[7:].strip()
                        elif line.startswith("data: "):
                            data_str = line[6:].strip()
                    if event_type != "matches" or not data_str:
                        continue
                    try:
                        matches = json.loads(data_str)
                    except Exception as _swallowed:
                        logger.debug(f"[suppressed] {_swallowed}")
                        continue
                    if not isinstance(matches, list):
                        continue

                    for m in matches:
                        if m.get("type") != "content":
                            continue
                        repo = m.get("repository") or ""
                        path = m.get("path") or ""
                        commit = m.get("commit") or ""
                        # repo format: "github.com/owner/name" → strip "github.com/"
                        repo_full = repo.replace("github.com/", "", 1) if repo.startswith("github.com/") else repo

                        # Dedup per (repo, path, commit) — stable identity
                        h = hashlib.sha256(f"{repo}|{path}|{commit}".encode()).hexdigest()[:16]
                        redis_key = f"sourcegraph:seen:{h}"
                        try:
                            if _redis.client.exists(redis_key):
                                continue
                        except Exception as _swallowed:
                            logger.debug(f"[suppressed] {_swallowed}")

                        # Aggregate matched line text
                        line_matches = m.get("lineMatches") or []
                        for lm in line_matches:
                            line_text = lm.get("line") or ""
                            tokens = set(TOKEN_PATTERN.findall(line_text))
                            for tok in tokens:
                                if not _is_valid_token(tok):
                                    continue
                                results.append({
                                    "token": tok,
                                    "meta": {
                                        "repo": repo_full,
                                        "path": path,
                                        "commit": commit,
                                        "line_number": lm.get("lineNumber"),
                                        "source_kind": "sourcegraph",
                                    }
                                })

                        with contextlib.suppress(Exception):
                            _redis.client.setex(redis_key, self.dedupe_ttl, "1")

                await asyncio.sleep(5)  # courtesy between queries

        logger.info(f"    [Sourcegraph] returned {len(results)} matches")
        return results
