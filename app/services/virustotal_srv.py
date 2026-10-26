"""VirusTotal lookups for infrastructure hosts seen in captured evidence."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx

from app.core.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

API_BASE = "https://www.virustotal.com/api/v3"
CACHE_TTL_SECONDS = 24 * 3600
RETRY_AFTER_KEY = "vt:cooldown"


def url_identifier(url: str) -> str:
    """Return VirusTotal's URL id: base64url of the URL without padding."""
    encoded = base64.urlsafe_b64encode(url.encode()).decode()
    return encoded.rstrip("=")


def _stats(attributes: dict) -> dict:
    stats = attributes.get("last_analysis_stats") or {}
    return {
        "malicious": int(stats.get("malicious") or 0),
        "suspicious": int(stats.get("suspicious") or 0),
        "harmless": int(stats.get("harmless") or 0),
        "undetected": int(stats.get("undetected") or 0),
        "reputation": attributes.get("reputation"),
        "categories": attributes.get("categories") or {},
        "checked_at": datetime.now(UTC).isoformat(),
    }


class VirusTotalService:
    def __init__(self) -> None:
        self.api_key = getattr(settings, "VIRUSTOTAL_API_KEY", None)
        self.timeout = httpx.Timeout(20.0, connect=10.0)
        self._redis = None
        self._auth_failed = False

    def _client(self):
        if self._redis is None:
            try:
                import redis

                self._redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
            except Exception as error:  # noqa: BLE001 - cache is optional
                logger.debug(f"[VirusTotal] cache unavailable: {error}")
        return self._redis

    @property
    def per_minute(self) -> int:
        return max(1, int(getattr(settings, "VIRUSTOTAL_REQUESTS_PER_MINUTE", 4) or 4))

    @property
    def per_day(self) -> int:
        return max(1, int(getattr(settings, "VIRUSTOTAL_REQUESTS_PER_DAY", 500) or 500))

    @property
    def alert_threshold(self) -> int:
        return max(1, int(getattr(settings, "VIRUSTOTAL_ALERT_THRESHOLD", 2) or 2))

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and not self._auth_failed

    def budget_exhausted(self) -> bool:
        client = self._client()
        if client is None:
            return False
        day = datetime.now(UTC).strftime("%Y%m%d")
        minute = datetime.now(UTC).strftime("%Y%m%d%H%M")
        try:
            if client.get(RETRY_AFTER_KEY):
                return True
            return int(client.get(f"vt:daily:{day}") or 0) >= self.per_day or int(
                client.get(f"vt:minute:{minute}") or 0
            ) >= self.per_minute
        except Exception:  # noqa: BLE001 - never block enrichment on cache errors
            return False

    def _consume_quota(self) -> bool:
        client = self._client()
        if client is None:
            return True
        day = datetime.now(UTC).strftime("%Y%m%d")
        minute = datetime.now(UTC).strftime("%Y%m%d%H%M")
        try:
            pipe = client.pipeline()
            pipe.incr(f"vt:minute:{minute}")
            pipe.expire(f"vt:minute:{minute}", 120)
            pipe.incr(f"vt:daily:{day}")
            pipe.expire(f"vt:daily:{day}", 90_000)
            pipe.execute()
            return True
        except Exception:  # noqa: BLE001
            return True

    def _cached(self, kind: str, value: str) -> dict | None:
        client = self._client()
        if client is None:
            return None
        try:
            raw = client.get(f"vt:cache:{kind}:{value}")
            return json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001
            return None

    def _store(self, kind: str, value: str, verdict: dict) -> None:
        client = self._client()
        if client is None:
            return
        try:
            client.setex(f"vt:cache:{kind}:{value}", CACHE_TTL_SECONDS, json.dumps(verdict))
        except Exception:  # noqa: BLE001
            pass

    async def _fetch(self, kind: str, value: str) -> dict | None:
        if not self.enabled:
            return None
        cached = self._cached(kind, value)
        if cached is not None:
            return cached
        if self.budget_exhausted():
            return None
        self._consume_quota()

        url = f"{API_BASE}/{kind}/{value}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers={"x-apikey": self.api_key})
        except Exception as error:  # noqa: BLE001
            logger.warning(f"[VirusTotal] {kind} lookup failed for {value}: {error}")
            return None

        if response.status_code == 401 or response.status_code == 403:
            self._auth_failed = True
            logger.warning("[VirusTotal] API key rejected; enrichment disabled this run")
            return None
        if response.status_code == 429:
            self._set_cooldown(response.headers.get("Retry-After", "60"))
            return None
        if response.status_code != 200:
            return None

        attributes = (response.json().get("data") or {}).get("attributes") or {}
        verdict = _stats(attributes)
        self._store(kind, value, verdict)
        return verdict

    def _set_cooldown(self, retry_after: str) -> None:
        client = self._client()
        if client is None:
            return
        try:
            client.setex(RETRY_AFTER_KEY, max(30, int(float(retry_after))), "1")
        except Exception:  # noqa: BLE001
            pass

    async def domain_report(self, domain: str) -> dict | None:
        return await self._fetch("domains", domain)

    async def ip_report(self, ip: str) -> dict | None:
        return await self._fetch("ip_addresses", ip)

    async def url_report(self, url: str) -> dict | None:
        return await self._fetch("urls", url_identifier(url))

    async def file_report(self, digest: str) -> dict | None:
        return await self._fetch("files", digest)


virustotal = VirusTotalService()
