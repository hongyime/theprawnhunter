"""
Pivot tasks — fan out new searches when a token validates.

Architecture:
    validate_token() confirms a token is live → fan out 3 pivot tasks:
      1. search_github_user(owner)         — owner of source repo
      2. search_bot_username(@bot_username) — Exa + Wayback
      3. search_webhook_host(host)          — Exa search for C2 host

Each pivot task is fire-and-forget on the `validation` queue (so the
worker-validators consume them naturally without a new container).

Dedup: 7-day Redis SET per (seed_type, seed_value). Same seed never gets
re-pivoted within 7 days regardless of which token surfaced it.

Rate limiting: pivots reuse existing scanner services (GithubService,
ExaService, WaybackService) which already have their own rate limit
discipline. We add courtesy sleeps between queries.
"""
import asyncio
import hashlib
import logging
import os

from app.workers.celery_app import _run_sync, app
from app.workers.tasks.flow_tasks import redis_client
from app.workers.tasks.scanner_tasks import _save_credentials_async

logger = logging.getLogger(__name__)

PIVOT_DEDUP_TTL = 7 * 86400  # 7 days

# ---- Global pivot rate limit: anti-cascade ----
# A high-yield token can spawn 3 pivots, each can validate 50+ new tokens,
# each of those can spawn 3 more pivots — geometric explosion. Cap the
# global pivot fan-out via a Redis token bucket. If exhausted, the pivot
# is dropped entirely (NOT retried — that would just defer the cascade).
PIVOT_BUDGET_KEY = "rate_limit:pivot_budget"
PIVOT_BUDGET_MAX = int(os.getenv("PIVOT_BUDGET_MAX", "60"))   # max pivots/window
PIVOT_BUDGET_WINDOW = int(os.getenv("PIVOT_BUDGET_WINDOW", "300"))  # 5 min


def _consume_pivot_budget(seed_type: str) -> bool:
    """Atomic INCR + EXPIRE-on-first. Returns False if window budget exhausted.

    Drop semantics chosen on purpose: a pivot we skip now is functionally
    equivalent to "never discovered" — the next scheduled scanner will catch
    the same surface. Better to lose 5% of pivots than triple our GitHub
    PAT spend during a high-yield burst.
    """
    try:
        pipe = redis_client.pipeline()
        pipe.incr(PIVOT_BUDGET_KEY)
        pipe.ttl(PIVOT_BUDGET_KEY)
        count, ttl = pipe.execute()
        if ttl is None or ttl < 0:
            # First call in window (or key was evicted by allkeys-lru).
            # Eviction resets the count to 1 — same as a fresh window start.
            # This is the SAFE eviction behavior: we never go negative, and a
            # cache miss under memory pressure just opens a fresh pivot window
            # rather than blocking pivots entirely.
            redis_client.expire(PIVOT_BUDGET_KEY, PIVOT_BUDGET_WINDOW)
        if count > PIVOT_BUDGET_MAX:
            logger.warning(
                f"[Pivot] budget exhausted ({count}/{PIVOT_BUDGET_MAX} in "
                f"{PIVOT_BUDGET_WINDOW}s window) — dropping {seed_type}"
            )
            return False
        return True
    except Exception:
        # Redis down: fail OPEN to keep pivots flowing, since failure mode is
        # transient and pivots are recoverable on next scan anyway.
        return True


def _pivot_already_done(seed_type: str, seed_value: str) -> bool:
    """Returns True if we've pivoted on this seed in the last 7 days."""
    seed_hash = hashlib.sha256(f"{seed_type}:{seed_value}".encode()).hexdigest()[:16]
    key = f"pivot:done:{seed_hash}"
    try:
        was_new = redis_client.set(key, "1", nx=True, ex=PIVOT_DEDUP_TTL)
        return not was_new
    except Exception:
        return False  # Redis down — fall through, pivot anyway


# -------- 1. GitHub user pivot --------

@app.task(
    name="pivot.search_github_user",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=2,
)
def search_github_user(username: str):
    """Search every public repo of `username` for token leaks."""
    if not username:
        return "skip:empty"
    if _pivot_already_done("gh_user", username):
        logger.info(f"[Pivot] gh_user={username} already pivoted in last 7d, skipping")
        return f"skipped:{username}"
    if not _consume_pivot_budget("gh_user"):
        return f"budget-drop:{username}"
    return _run_sync(_search_github_user_async(username))


async def _search_github_user_async(username: str):
    from app.services.scanners import GithubService
    g = GithubService()
    queries = [
        f'"api.telegram.org/bot" user:{username}',
        f'"TELEGRAM_BOT_TOKEN" user:{username}',
        f'"bot_token" user:{username}',
    ]
    total = 0
    for q in queries:
        try:
            results = await g.search(q)
            if results:
                saved = await _save_credentials_async(results, f"pivot_gh_user:{username}")
                total += saved
            await asyncio.sleep(2)  # courtesy spacing
        except Exception as e:
            logger.warning(f"[Pivot:gh_user] '{q}' failed: {e}")
    logger.info(f"[Pivot:gh_user={username}] enqueued {total} tokens")
    return f"pivoted:{username}:{total}"


# -------- 2. Bot username pivot --------

@app.task(
    name="pivot.search_bot_username",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=1,
)
def search_bot_username(username: str):
    """Search Exa + Wayback for the literal bot @username."""
    if not username or username == "unknown":
        return "skip:empty"
    if _pivot_already_done("bot_username", username):
        logger.info(f"[Pivot] bot_username={username} already pivoted, skipping")
        return f"skipped:{username}"
    if not _consume_pivot_budget("bot_username"):
        return f"budget-drop:{username}"
    return _run_sync(_search_bot_username_async(username))


async def _search_bot_username_async(username: str):
    from app.services.scanners import ExaService, WaybackService
    exa = ExaService()
    wayback = WaybackService()
    total = 0

    # Exa search — semantic + literal
    try:
        results = await exa.search(f'"@{username}" telegram bot')
        if results:
            saved = await _save_credentials_async(results, f"pivot_botusername_exa:{username}")
            total += saved
    except Exception as e:
        logger.warning(f"[Pivot:bot_username:exa] failed: {e}")

    await asyncio.sleep(2)

    # Wayback — historical snapshots of t.me/<username> pages
    # Wayback CDX is URL-only search, not content search; we look for archived
    # t.me/<username> pages whose URLs may have token query strings.
    try:
        results = await wayback.search(query_pattern=f"t.me/{username}", limit=50)
        if results:
            saved = await _save_credentials_async(results, f"pivot_botusername_wb:{username}")
            total += saved
    except Exception as e:
        logger.warning(f"[Pivot:bot_username:wayback] failed: {e}")

    logger.info(f"[Pivot:bot_username={username}] enqueued {total} tokens")
    return f"pivoted:{username}:{total}"


# -------- 3. Webhook host pivot --------

@app.task(
    name="pivot.search_webhook_host",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=1,
)
def search_webhook_host(webhook_url: str):
    """Search Exa for the webhook host — attacker C2 sometimes leaks too."""
    if not webhook_url:
        return "skip:empty"
    from urllib.parse import urlparse
    try:
        host = urlparse(webhook_url).netloc
    except Exception:
        return "skip:bad_url"
    if not host:
        return "skip:no_host"
    # Skip Telegram-native infrastructure
    if host in ("api.telegram.org", "core.telegram.org", "telegram.org"):
        return "skip:telegram_native"
    if _pivot_already_done("webhook_host", host):
        logger.info(f"[Pivot] webhook_host={host} already pivoted, skipping")
        return f"skipped:{host}"
    if not _consume_pivot_budget("webhook_host"):
        return f"budget-drop:{host}"
    return _run_sync(_search_webhook_host_async(host))


async def _search_webhook_host_async(host: str):
    from app.services.scanners import ExaService
    exa = ExaService()
    try:
        results = await exa.search(f'"{host}" telegram bot token')
        total = 0
        if results:
            total = await _save_credentials_async(results, f"pivot_webhook:{host}")
        logger.info(f"[Pivot:webhook_host={host}] enqueued {total} tokens")
        return f"pivoted:{host}:{total}"
    except Exception as e:
        logger.warning(f"[Pivot:webhook_host={host}] failed: {e}")
        return f"err:{host}"
