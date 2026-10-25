"""
Pivot tasks — fan out new searches when a token validates.

Architecture:
    validate_token() confirms a token is live → fan out 3 pivot tasks:
      1. search_github_user(owner)         — owner of source repo
      2. search_bot_username(@bot_username) — Exa + Wayback
      3. search_webhook_host(host)          — Exa search for C2 host

Each pivot task is fire-and-forget on the `validation` queue (so the
worker-validators consume them naturally without a new container).

Owner pivots walk an owner graph at most two hops deep: the original
credential's repo owner is hop 1, an owner surfaced by a hop-1 candidate is
hop 2. Every candidate an owner pivot produces carries its provenance in
`meta.pivot_owner` / `meta.pivot_depth`, and depth is the only re-enqueue
gate (`MAX_PIVOT_DEPTH`), so a cyclic owner graph terminates.

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
    except Exception as _swallowed:
        # Redis down: fail OPEN to keep pivots flowing, since failure mode is
        # transient and pivots are recoverable on next scan anyway.
        logger.debug(f"[suppressed] {_swallowed}")
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


# -------- Owner-pivot depth contract --------
# A candidate carries the owner hop that produced it:
#   absent meta / absent pivot_depth -> 0 (original credential, no owner hop)
#   1 -> found by pivoting the original credential's repo owner
#   2 -> found by pivoting an owner surfaced by a depth-1 candidate
# Depth is the sole re-enqueue gate: hop N may queue hop N+1 only while
# N < MAX_PIVOT_DEPTH. Depth MAX_PIVOT_DEPTH never re-enqueues, which bounds
# cyclic owner graphs (A -> B -> A) before the 7-day dedup gate even fires.
MAX_PIVOT_DEPTH = 2
PIVOT_DEPTH_META_KEY = "pivot_depth"
PIVOT_OWNER_META_KEY = "pivot_owner"


def normalise_pivot_depth(value) -> int:
    """Clamp any provenance value into [0, MAX_PIVOT_DEPTH].

    Absent, non-numeric and negative values all mean "no owner hop yet", so a
    legacy payload (or a hand-queued task) can never crash or jump the cap.
    """
    try:
        depth = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(depth, MAX_PIVOT_DEPTH))


def next_owner_pivot_depth(meta) -> int | None:
    """Return the hop depth an owner pivot should run at, or None at the cap."""
    depth = normalise_pivot_depth((meta or {}).get(PIVOT_DEPTH_META_KEY))
    if depth >= MAX_PIVOT_DEPTH:
        return None
    return depth + 1


def _resolve_hop_depth(value) -> int | None:
    """Depth this pivot task should execute at; None means "beyond the cap"."""
    try:
        depth = int(value)
    except (TypeError, ValueError):
        return 1
    if depth < 1:
        return 1
    if depth > MAX_PIVOT_DEPTH:
        return None
    return depth


def _repo_owner(meta) -> str:
    data = meta if isinstance(meta, dict) else {}
    repo = data.get("repo") or data.get("repository")
    if not isinstance(repo, str) or "/" not in repo:
        return ""
    return repo.split("/", 1)[0].strip()


def _other_repo_owners(results, current_owner: str) -> set[str]:
    """Owners surfaced by this hop's results, minus the owner just pivoted."""
    current = (current_owner or "").lower()
    owners: dict[str, str] = {}
    for item in results or []:
        if not isinstance(item, dict):
            continue
        owner = _repo_owner(item.get("meta"))
        if not owner:
            continue
        key = owner.lower()
        if key == current or key in owners:
            continue
        owners[key] = owner
    return set(owners.values())


def _tag_pivot_results(results, owner: str, pivot_depth: int):
    """Stamp provenance on every candidate before it reaches the validator."""
    for item in results or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("meta")
        item["meta"] = {
            **(meta if isinstance(meta, dict) else {}),
            PIVOT_OWNER_META_KEY: owner,
            PIVOT_DEPTH_META_KEY: pivot_depth,
        }
    return results


def enqueue_owner_pivot(owner: str, pivot_depth) -> bool:
    """Queue one owner-pivot hop at an explicit depth. True when queued."""
    depth = normalise_pivot_depth(pivot_depth)
    if not owner or depth < 1 or depth > MAX_PIVOT_DEPTH:
        return False
    search_github_user.apply_async(
        args=[owner], kwargs={"pivot_depth": depth}, queue="validation"
    )
    return True


def enqueue_owner_pivot_from_meta(owner: str, meta) -> bool:
    """Advance the owner graph for a candidate's repo owner.

    Depth 0 -> hop 1, depth 1 -> hop 2, depth 2+ -> nothing. Re-pivoting the
    owner that already produced this candidate is skipped here as well as by
    the 7-day `_pivot_already_done` gate, so a cycle never re-queues itself.
    """
    if not owner:
        return False
    previous_owner = str((meta or {}).get(PIVOT_OWNER_META_KEY) or "").strip()
    if previous_owner and previous_owner.lower() == owner.lower():
        return False
    depth = next_owner_pivot_depth(meta)
    if depth is None:
        return False
    return enqueue_owner_pivot(owner, depth)


def fan_out_pivots(meta, bot_username: str | None = None, webhook_url: str | None = None) -> dict:
    """Queue every pivot implied by a credential that just validated.

    Returns which seeds actually queued work. Callers keep the surrounding
    try/except: a pivot failure must never block the validation pipeline.
    """
    enqueued = {"owner": False, "bot_username": False, "webhook_host": False}
    repo = (meta or {}).get("repo")  # format "owner/repo"
    if isinstance(repo, str) and "/" in repo:
        owner = repo.split("/", 1)[0].strip()
        enqueued["owner"] = enqueue_owner_pivot_from_meta(owner, meta)
    # Seed 2: Bot @username (always available from getMe)
    if bot_username and bot_username != "unknown":
        search_bot_username.apply_async(args=[bot_username], queue="validation")
        enqueued["bot_username"] = True
    # Seed 3: Webhook host (only if set)
    if webhook_url:
        search_webhook_host.apply_async(args=[webhook_url], queue="validation")
        enqueued["webhook_host"] = True
    return enqueued


# -------- 1. GitHub user pivot --------

@app.task(
    name="pivot.search_github_user",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=2,
)
def search_github_user(username: str, pivot_depth: int = 1):
    """Search every public repo of `username` for token leaks.

    `pivot_depth` is the owner hop this search represents — 1 for the owner of
    the credential that surfaced the seed, 2 for an owner surfaced by a hop-1
    candidate. Requests beyond `MAX_PIVOT_DEPTH` are dropped, and this task
    only chains the next hop while still inside the cap.
    """
    if not username:
        return "skip:empty"
    hop = _resolve_hop_depth(pivot_depth)
    if hop is None:
        logger.warning(
            f"[Pivot] gh_user={username} requested at depth {pivot_depth} "
            f"> {MAX_PIVOT_DEPTH}, dropping"
        )
        return f"depth-cap:{username}"
    if _pivot_already_done("gh_user", username):
        logger.info(f"[Pivot] gh_user={username} already pivoted in last 7d, skipping")
        return f"skipped:{username}"
    if not _consume_pivot_budget("gh_user"):
        return f"budget-drop:{username}"
    return _run_sync(_search_github_user_async(username, hop))


async def _search_github_user_async(username: str, pivot_depth: int = 1):
    from app.services.scanners import GithubService
    g = GithubService()
    queries = [
        f'"api.telegram.org/bot" user:{username}',
        f'"TELEGRAM_BOT_TOKEN" user:{username}',
        f'"bot_token" user:{username}',
    ]
    total = 0
    next_owners: set[str] = set()
    for q in queries:
        try:
            results = await g.search(q)
            if results:
                _tag_pivot_results(results, username, pivot_depth)
                next_owners |= _other_repo_owners(results, username)
                saved = await _save_credentials_async(results, f"pivot_gh_user:{username}")
                total += saved
            await asyncio.sleep(2)  # courtesy spacing
        except Exception as e:
            logger.warning(f"[Pivot:gh_user] '{q}' failed: {e}")
    logger.info(f"[Pivot:gh_user={username}] enqueued {total} tokens")
    chained = 0
    if pivot_depth < MAX_PIVOT_DEPTH:
        for owner in sorted(next_owners):
            if enqueue_owner_pivot(owner, pivot_depth + 1):
                chained += 1
    if chained:
        logger.info(
            f"[Pivot:gh_user={username}] chained {chained} "
            f"hop-{pivot_depth + 1} owner pivot(s)"
        )
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
