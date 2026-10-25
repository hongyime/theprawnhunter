import asyncio  # Ensure asyncio is imported

import hashlib

import logging

import os

import random

import re

import time

from app.core.config import settings

from app.core.constants import MAX_ERRORS_BUFFER

from app.core.database import db

from app.services.scanners import (
    CommonCrawlService,
    ExaService,
    FofaService,
    GithubService,
    GitlabService,
    ShodanService,
    SourcegraphService,
    UrlScanService,
    WaybackService,
)

from app.services.scanners_extension import (
    BitbucketService,
    GithubGistService,
    GoogleSearchService,
    GrepAppService,
    NetlasService,
    PastebinService,
    PostmanService,
    PublicWwwService,
    ReplitService,
    SearchcodeService,
)

from app.workers.celery_app import app

from app.workers.tasks.flow_tasks import (  # Import for triggering and DB
    redis_client,  # Shared module-level pool — avoids per-call ConnectionPool allocation
)

logger = logging.getLogger("scanner.tasks")

logger.setLevel(logging.INFO)

shodan = ShodanService()

github = GithubService()

urlscan = UrlScanService()

fofa = FofaService()

gitlab_srv = GitlabService()

bitbucket_srv = BitbucketService()

gist_srv = GithubGistService()

grepapp_srv = GrepAppService()

pastebin_srv = PastebinService()

exa_srv = ExaService()

wayback_srv = WaybackService()

commoncrawl_srv = CommonCrawlService()

sourcegraph_srv = SourcegraphService()

google_srv = GoogleSearchService()

netlas_srv = NetlasService()

publicwww_srv = PublicWwwService()  # BUG-002: was missing, caused NameError in scan_publicwww

replit_srv = ReplitService()

postman_srv = PostmanService()

searchcode_srv = SearchcodeService()

def _calculate_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def _is_own_bot_token(token: str) -> bool:
    """Returns True if token belongs to one of our own monitor bots.

    Checked at scanner output — prevents own tokens from ever being
    enqueued for validation, regardless of where they were found.
    Hard-fail safe: if settings can't load, returns False (don't block).
    """
    try:
        from app.services.scraper_srv import scraper_service
        return scraper_service.is_monitor_bot(token)
    except Exception as _swallowed:
        logger.debug(f"[suppressed] {_swallowed}")
        return False

def _token_already_validated(token: str) -> bool:
    """
    Cross-source soft dedup: returns True if this token was enqueued for
    validation within the last 24h. Saves Telegram getMe quota when multiple
    scanners (and pivots) find the same token in the same hour.

    Also returns True for own monitor bot tokens — these are filtered at
    source so they never reach the validation queue.

    SOFT layer: this only checks Redis. The DB-level dedup still happens
    inside validate_token. This gate prevents the queue fanout itself.
    """
    # Own-bot fast-path: drop before Redis/queue touch
    if _is_own_bot_token(token):
        return True

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    # Use full 64-char hash as Redis key suffix — truncating to 16 chars halved
    # the collision space to 64-bit, which is borderline for a key-space of 100k+ tokens.
    key = f"validated:recent:{token_hash}"
    try:
        # SET NX EX = atomic check-and-set with 24h TTL
        was_new = redis_client.set(key, "1", nx=True, ex=86400)
        return not was_new  # if set was rejected, key already existed
    except Exception:
        return False  # Redis down — fall through, validate anyway

async def _save_credentials_async(results, source_name: str):
    """
    Stage each candidate in the encrypted `validation_queue` for priority dispatch.

    Returns the number of tokens actually staged (inserted into the queue) — not
    saved. The dispatcher leases rows highest-priority-first and hands them to
    worker-validators, which still apply global Redis-backed rate limiting.

    This replaces the old `validate_token.delay(item, source_name)` broker fanout,
    which put the plaintext token on the Celery broker and validated FIFO. Now the
    broker only ever sees queue row ids: tokens are encrypted at rest by
    :func:`app.services.validation_queue_srv.enqueue` and deduplicated there by
    ``token_hash``.

    Failure isolation: if a queue write fails (DB down), the error is logged and
    the remaining candidates are retried one-by-one so a single bad row cannot
    abort a scanner run. The returned count is what was actually staged.
    """
    from app.services.validation_queue_srv import enqueue

    entries: list[dict] = []
    skipped_dedup = 0
    for item in results:
        token = item.get("token")
        if not token or token == "MANUAL_REVIEW_REQUIRED":
            continue
        # Cross-source soft dedup — skip if same token was enqueued in last 24h
        if _token_already_validated(token):
            skipped_dedup += 1
            continue
        raw_meta = item.get("meta")
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        chat_id = item.get("chat_id")
        if chat_id is not None and "chat_id" not in meta:
            meta["chat_id"] = chat_id
        # Plaintext token stays in memory only; enqueue encrypts it before storage.
        entries.append({"token": token, "source": source_name, "meta": meta})

    enqueued = 0
    if entries:
        try:
            enqueued = await enqueue(entries)
        except Exception as exc:
            # Do not abort the scanner run: retry per-item to salvage the batch.
            logger.error(
                f"🔍 [Enqueue] staging {len(entries)} tokens from {source_name} "
                f"failed, retrying individually: {exc}"
            )
            for entry in entries:
                try:
                    enqueued += await enqueue([entry])
                except Exception as item_exc:
                    logger.error(
                        f"🔍 [Enqueue] dropping candidate from {source_name} "
                        f"after queue write failure: {item_exc}"
                    )

    if enqueued or skipped_dedup:
        logger.info(
            f"🔍 [Enqueue] {enqueued}/{len(results)} tokens from {source_name} "
            f"(staged {enqueued}, soft-deduped {skipped_dedup}) "
            f"queued for priority validation"
        )
    return enqueued

from datetime import UTC  # noqa: E402 — intentional deferred import

from app.workers.celery_app import _run_sync  # noqa: F401, E402  (re-export, intentional deferred)

def _cb(service_name: str):
    """Return the circuit breaker for a scanner service (cached singleton per name)."""
    from app.core.circuit_breaker import get_circuit_breaker
    return get_circuit_breaker(service_name)

def _save_credentials(results, source_name: str):
    return _run_sync(_save_credentials_async(results, source_name))

async def _send_log_async(message: str):
    from app.workers.tasks.flow_tasks import get_broadcaster
    broadcaster = get_broadcaster()
    await broadcaster.send_log(message)

import contextlib  # noqa: E402 — intentional deferred import

from app.workers.tasks._scanner.base import _run_scanner  # noqa: E402 — intentional deferred import

from app.workers.tasks._scanner.queries import (  # noqa: F401, E402
    FOFA_DEFAULT_QUERIES,
    NETLAS_QUERIES,
    SHODAN_DEFAULT_QUERIES,
    _shodan_body_query,
)

__all__ = ['BitbucketService', 'CommonCrawlService', 'ExaService', 'FOFA_DEFAULT_QUERIES', 'FofaService', 'GithubGistService', 'GithubService', 'GitlabService', 'GoogleSearchService', 'GrepAppService', 'MAX_ERRORS_BUFFER', 'NETLAS_QUERIES', 'NetlasService', 'PastebinService', 'PostmanService', 'PublicWwwService', 'ReplitService', 'SHODAN_DEFAULT_QUERIES', 'SearchcodeService', 'ShodanService', 'SourcegraphService', 'UTC', 'UrlScanService', 'WaybackService', '_calculate_hash', '_cb', '_is_own_bot_token', '_run_scanner', '_run_sync', '_save_credentials', '_save_credentials_async', '_send_log_async', '_shodan_body_query', '_token_already_validated', 'app', 'asyncio', 'bitbucket_srv', 'commoncrawl_srv', 'contextlib', 'db', 'exa_srv', 'fofa', 'gist_srv', 'github', 'gitlab_srv', 'google_srv', 'grepapp_srv', 'hashlib', 'logger', 'logging', 'netlas_srv', 'os', 'pastebin_srv', 'postman_srv', 'publicwww_srv', 'random', 're', 'redis_client', 'replit_srv', 'searchcode_srv', 'settings', 'shodan', 'sourcegraph_srv', 'time', 'urlscan', 'wayback_srv']
