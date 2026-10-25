"""Scanner client for CommonCrawl."""

from app.domain.tokens import sweep_text_for_tokens

from ..common import *

class CommonCrawlService:
    """
    Common Crawl Index API — free historical web crawl URL search.

    Endpoint pattern:
        https://index.commoncrawl.org/CC-MAIN-{crawl_id}-index
        ?url=api.telegram.org&matchType=domain&output=json&limit=N

    Response format: NDJSON (one JSON object per line, NOT a JSON array).

    Strategy:
      1. Discover the latest crawl ID from /collinfo.json
      2. Query that crawl's index for URLs under api.telegram.org domain
      3. Tokens leak via URL query strings: api.telegram.org/bot{TOKEN}/...
      4. Extract tokens directly from URL — no WARC fetch needed for the
         common case (CDX URL field already contains everything we need).

    Cost: zero. No AWS, no Athena, no API key.
    Rate: ~1 req/sec courtesy (no documented hard cap).
    """

    COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
    QUERY_DOMAIN = "api.telegram.org"

    def __init__(self):
        self.timeout = httpx.Timeout(30.0, connect=10.0)
        self.dedupe_ttl = 30 * 86400  # 30 days

    async def search(self, limit: int = 500) -> list[dict[str, Any]]:
        from app.core.redis_srv import redis_srv as _redis
        results: list[dict[str, Any]] = []

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "TheprawnHunter/1.0 (+research)"},
        ) as client:
            # Step 1: discover the newest crawl
            try:
                cr = await client.get(self.COLLINFO_URL)
                cr.raise_for_status()
                colls = cr.json()
                if not colls:
                    logger.warning("[CommonCrawl] empty collinfo")
                    return []
                latest = colls[0]  # newest first
                index_api = latest.get("cdx-api")
                crawl_id = latest.get("id")
                if not index_api:
                    logger.warning(f"[CommonCrawl] no cdx-api for {crawl_id}")
                    return []
                logger.info(f"[CommonCrawl] using crawl {crawl_id}")
            except Exception as e:
                logger.error(f"[CommonCrawl] collinfo failed: {e}")
                return []

            # Step 2: query the index — domain match, paginated by limit
            try:
                idx_resp = await client.get(
                    index_api,
                    params={
                        "url": self.QUERY_DOMAIN,
                        "matchType": "domain",
                        "output": "json",
                        "limit": limit,
                        "filter": "=status:200",  # successful captures only
                    },
                )
                if idx_resp.status_code != 200:
                    logger.warning(f"[CommonCrawl] index HTTP {idx_resp.status_code}")
                    return []
                # NDJSON — one JSON object per line
                raw = idx_resp.text or ""
                lines = [ln for ln in raw.split("\n") if ln.strip()]
            except Exception as e:
                logger.error(f"[CommonCrawl] index query failed: {e}")
                return []

            seen_in_run = set()
            for line in lines:
                try:
                    rec = json.loads(line)
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")
                    continue
                url = rec.get("url")
                if not isinstance(url, str) or not url:
                    logger.debug("[CommonCrawl] Skipping non-text indexed URL")
                    continue

                url_hash = hashlib.sha256(url.encode("utf-8", errors="replace")).hexdigest()[:16]
                if url_hash in seen_in_run:
                    continue
                seen_in_run.add(url_hash)

                redis_key = f"commoncrawl:seen:{url_hash}"
                try:
                    if _redis.client.exists(redis_key):
                        continue
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")

                # Extract tokens directly from URL
                for hit in sweep_text_for_tokens(url, "commoncrawl"):
                    results.append({
                        "token": hit["token"],
                        "meta": {
                            "commoncrawl_url": url,
                            "commoncrawl_timestamp": rec.get("timestamp"),
                            "commoncrawl_crawl": crawl_id,
                            "extracted_from": "url",
                            "source_kind": "commoncrawl",
                        }
                    })

                # Mark seen regardless of match (avoids re-processing same URL)
                with contextlib.suppress(Exception):
                    _redis.client.setex(redis_key, self.dedupe_ttl, "1")

        logger.info(
            f"    [CommonCrawl] returned {len(results)} matches "
            f"across {len(seen_in_run)} URLs"
        )
        return results
