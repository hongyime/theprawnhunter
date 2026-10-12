"""Scanner client for Wayback."""

from ..common import *

class WaybackService:
    """
    Internet Archive Wayback Machine — historical URL scanner.

    Free, no API key. Uses CDX API for URL discovery + archived content fetch.
    Rate limit: ~1 req/sec courtesy (no documented hard cap; we sleep 1.2s).

    Token extraction strategy:
      1. From URL itself — many leaks are `.../bot<TOKEN>/sendMessage?...`
      2. From archived response body — paste content preserved in archive

    Dedup: SHA256 of original_url, cached in Redis 7 days. Avoids re-fetching
    the same snapshot across runs (CDX returns stable URLs across queries).

    Cost profile: 500 snapshots/run × ~2 HTTP calls each = 1000 archive.org
    requests/run, paced at 1.2s = ~20 min run time. Schedule once daily at
    04:00 UTC during quiet period.
    """

    CDX_URL = "https://web.archive.org/cdx/search/cdx"
    ARCHIVE_URL_TEMPLATE = "https://web.archive.org/web/{timestamp}/{url}"

    def __init__(self):
        self.timeout = httpx.Timeout(20.0, connect=10.0)
        self.dedupe_ttl = 7 * 86400  # 7 days

    async def search(self, query_pattern: str = "api.telegram.org", limit: int = 500) -> list[dict[str, Any]]:
        """Query CDX, fetch unseen archived content, extract tokens."""
        from app.workers.tasks.flow_tasks import redis_client
        results: list[dict[str, Any]] = []

        # Step 1: CDX query
        # matchType=domain searches the domain + all subpaths — required for
        # api.telegram.org/bot* style URLs since `prefix` matchType doesn't
        # honor wildcards in the path (only in the host portion).
        params = {
            "url": query_pattern,
            "matchType": "domain",
            "output": "json",
            "limit": limit,
            "filter": ["statuscode:200", "urlkey:.*bot.*"],
        }

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            try:
                resp = await client.get(self.CDX_URL, params=params)
                resp.raise_for_status()
                rows = resp.json()
            except Exception as e:
                logger.error(f"    [Wayback] CDX query failed: {e}")
                return []

            if not rows or len(rows) < 2:
                logger.info("    [Wayback] CDX returned 0 rows")
                return []

            # First row is header
            header = rows[0]
            try:
                ts_idx = header.index("timestamp")
                url_idx = header.index("original")
            except ValueError:
                logger.error(f"    [Wayback] Unexpected CDX header: {header}")
                return []

            seen_in_run = set()
            for row in rows[1:]:
                if len(row) < max(ts_idx, url_idx) + 1:
                    continue
                timestamp = row[ts_idx]
                original = row[url_idx]

                # Dedupe by URL hash (multiple snapshots of same URL = redundant)
                url_hash = hashlib.sha256(original.encode("utf-8", errors="replace")).hexdigest()[:16]
                if url_hash in seen_in_run:
                    continue
                seen_in_run.add(url_hash)

                redis_key = f"wayback:seen:{url_hash}"
                try:
                    if redis_client.exists(redis_key):
                        continue
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")  # Redis down — process anyway, we'll skip the marker

                # Step 2: Extract token from URL itself
                url_tokens = TOKEN_PATTERN.findall(original)
                for tok in url_tokens:
                    if not _is_valid_token(tok):
                        continue
                    results.append({
                        "token": tok,
                        "meta": {
                            "wayback_url": original,
                            "wayback_timestamp": timestamp,
                            "extracted_from": "url",
                        }
                    })

                # Step 3: Fetch archived body (may have additional tokens)
                archive_url = self.ARCHIVE_URL_TEMPLATE.format(timestamp=timestamp, url=original)
                try:
                    arc_resp = await client.get(archive_url)
                    if arc_resp.status_code == 200:
                        body_tokens = set(TOKEN_PATTERN.findall(arc_resp.text))
                        for tok in body_tokens:
                            if tok in url_tokens:
                                continue  # already added via URL extraction
                            if not _is_valid_token(tok):
                                continue
                            results.append({
                                "token": tok,
                                "meta": {
                                    "wayback_url": original,
                                    "wayback_timestamp": timestamp,
                                    "extracted_from": "body",
                                }
                            })
                except Exception as e:
                    logger.debug(f"    [Wayback] Fetch failed {original[:60]}: {e}")

                # Mark seen (1-week dedup)
                with contextlib.suppress(Exception):
                    redis_client.setex(redis_key, self.dedupe_ttl, "1")

                # Courtesy rate limit
                await asyncio.sleep(1.2)

        logger.info(f"    [Wayback] Returned {len(results)} matches across {len(seen_in_run)} snapshots")
        return results
