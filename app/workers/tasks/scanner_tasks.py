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

# Configure Task Logger
logger = logging.getLogger("scanner.tasks")
logger.setLevel(logging.INFO)

# Instantiate services
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
    except Exception:
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
    Enqueue each token for async validation on the dedicated `validation` queue.

    Returns the number of tokens ENQUEUED (not saved) — the actual save happens
    later in worker-validators with global Redis-backed rate limiting.

    This replaces the old in-line VALIDATE_BATCH_CAP=50 batch validation, which:
      * silently dropped tokens beyond cap on big result sets
      * blocked the scanner queue for 10-15 min per run
      * burst getMe calls triggered Telegram per-IP secondary rate limits
        that cascaded into bot_restricted cooldowns

    Now: scanners return immediately after enqueue. Validators consume the
    queue at a controlled rate (default 30 calls / 10s globally, env-tunable
    via VALIDATE_RATE_MAX / VALIDATE_RATE_WINDOW).
    """
    from app.workers.tasks.validation_tasks import validate_token

    enqueued = 0
    skipped_dedup = 0
    for item in results:
        token = item.get("token")
        if not token or token == "MANUAL_REVIEW_REQUIRED":
            continue
        # Cross-source soft dedup — skip if same token was enqueued in last 24h
        if _token_already_validated(token):
            skipped_dedup += 1
            continue
        # Fire-and-forget — validation worker handles dedup, getMe, save
        validate_token.delay(item, source_name)
        enqueued += 1

    if enqueued or skipped_dedup:
        logger.info(
            f"🔍 [Enqueue] {enqueued}/{len(results)} tokens from {source_name} "
            f"(soft-deduped {skipped_dedup}) "
            f"queued for async validation"
        )
    return enqueued


# _run_sync: canonical definition lives in celery_app.py.
# Re-exported here for backward compat — the 14 internal scan_* callers below
# use this name.  External importers (pivot_tasks, validation_tasks, firehose_tasks)
# have all been migrated to import directly from celery_app.
from datetime import UTC

from app.workers.celery_app import _run_sync  # noqa: F401  (re-export)


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


# ==========================================
# TASKS
# ==========================================


@app.task(name="scanner.scan_shodan")
def scan_shodan(query: str = None, country_code: str = None):
    return _run_sync(_scan_shodan_async(query, country_code))


async def _scan_shodan_async(query: str = None, country_code: str = None):
    default_queries = SHODAN_DEFAULT_QUERIES

    queries = [query] if query else default_queries

    selected_country = country_code
    if country_code == "RANDOM":
        selected_country = random.choice(settings.TARGET_COUNTRIES)

    logger.info(
        f"🌎 [Shodan] Starting Scan | Queries: {len(queries)} | Country: {selected_country or 'Global'}"
    )

    # Check Pause State
    if redis_client.get("system:paused"):
        logger.warning("⏸️ [Shodan] System is PAUSED. Skipping scan.")
        return "System Paused"

    await _send_log_async(
        f"🌎 [Shodan] Starting scan with {len(queries)} queries (Country: {selected_country})..."
    )

    total_saved = 0
    errors = []
    result_msg = ""

    for q in queries:
        try:
            logger.info(f"    🔎 [Shodan] Executing query: {q}")
            results = await _cb("shodan").call(shodan.search)(q, country_code=selected_country)
            logger.info(f"    ✅ [Shodan] Query returned {len(results)} raw results.")

            # AWAIT the async save
            saved = await _save_credentials_async(results, "shodan")
            total_saved += saved

            await asyncio.sleep(1)  # Rate limit respect
        except Exception as e:
            logger.error(f"    ❌ [Shodan] Query failed: {str(e)}")
            errors.append(str(e))
            errors = errors[-MAX_ERRORS_BUFFER:]
    if errors:
        result_msg += f" (Errors: {len(errors)})"
        await _send_log_async(f"❌ [Shodan] Completed with errors: {errors[0]}...")

    logger.info(f"🏁 [Shodan] Finished | Total Saved: {total_saved} | Errors: {len(errors)}")
    await _send_log_async(f"🏁 [Shodan] Finished. Saved {total_saved} new credentials.")
    return result_msg


@app.task(name="scanner.scan_urlscan")
def scan_urlscan(query: str = None, country_code: str = None):
    return _run_sync(_scan_urlscan_async(query, country_code))


async def _scan_urlscan_async(query: str = None, country_code: str = None):

    COMMON_QUERIES = [
        "api.telegram.org/bot",
        "bot_token",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_TOKEN",
        "Telegram Bot",
        "https://api.telegram.org",
    ]

    queries = [query] if query else COMMON_QUERIES

    selected_country = country_code
    if country_code == "RANDOM":
        selected_country = random.choice(settings.TARGET_COUNTRIES)

    logger.info(
        f"🔍 [URLScan] Starting Scan | Queries: {len(queries)} | Country: {selected_country or 'Global'}"
    )

    # Check Pause State
    if redis_client.get("system:paused"):
        logger.warning("⏸️ [URLScan] System is PAUSED. Skipping scan.")
        return "System Paused"

    await _send_log_async(
        f"🔍 [URLScan] Starting scan with {len(queries)} queries (Country: {selected_country})"
    )

    total_saved = 0
    errors = []

    for q in queries:
        try:
            logger.info(f"    🔎 [URLScan] Query: {q}")
            results = await _cb("urlscan").call(urlscan.search)(q, country_code=selected_country)
            logger.info(f"    ✅ [URLScan] Returned {len(results)} results.")

            saved = await _save_credentials_async(results, "urlscan")
            total_saved += saved
            await asyncio.sleep(2)  # Rate limit protection
        except Exception as e:
            logger.error(f"    ❌ [URLScan] Query failed: {e}")
            errors.append(str(e))
            errors = errors[-MAX_ERRORS_BUFFER:]

    result_msg = f"URLScan scan finished. Saved {total_saved} new credentials."
    if errors:
        result_msg += f" (Errors: {len(errors)})"

    logger.info(f"🏁 [URLScan] Finished | Saved: {total_saved} | Errors: {len(errors)}")
    await _send_log_async(f"🏁 [URLScan] Finished. Saved {total_saved} new credentials.")
    return result_msg


@app.task(name="scanner.scan_github")
def scan_github(query: str = None):
    return _run_sync(_scan_github_async(query))


async def _scan_github_async(query: str = None):
    # High-signal dorks only. Ordered by expected yield:
    # 1. .env files with explicit var names — fewest false positives
    # 2. Config files — moderate signal
    # 3. Direct api.telegram.org/bot in source by extension — higher volume, lower precision
    #
    # Removed:
    # - language:python/js/php/go patterns → too broad, GitHub secondary rate-limits on these fast
    # - Generic "https://api.telegram.org/bot" without filename anchor → enormous result sets, slow
    # - Duplicate token var names that resolve to same result pool
    #
    # GITHUB_DORK_CAP (env, default 10): max dorks per run. Increase carefully.
    # GITHUB_QUERY_SLEEP (env, default 8): seconds between queries.
    # Both exist so you can tune without code changes.
    default_dorks = [
        # Tier 1 — highest precision: explicit var name in .env files
        'filename:.env "TELEGRAM_BOT_TOKEN"',
        'filename:.env "TG_BOT_TOKEN"',
        'filename:.env "BOT_TOKEN"',
        # Tier 2 — config files
        'filename:config.json "bot_token"',
        'filename:docker-compose.yml "TELEGRAM_BOT_TOKEN"',
        'filename:.env.production "TELEGRAM"',
        'filename:.env.local "TELEGRAM"',
        # Tier 3 — direct token pattern in source, scoped by extension
        '"api.telegram.org/bot" extension:py',
        '"api.telegram.org/bot" extension:js',
        '"api.telegram.org/bot" extension:php',
    ]

    DORK_CAP = int(os.getenv("GITHUB_DORK_CAP", 10))
    QUERY_SLEEP = float(os.getenv("GITHUB_QUERY_SLEEP", 8))

    queries = [query] if query else default_dorks[:DORK_CAP]

    total_saved = 0
    errors = []

    # Check Pause State
    if redis_client.get("system:paused"):
        logger.warning("⏸️ [GitHub] System is PAUSED. Skipping scan.")
        return "System Paused"

    logger.info(f"🐱 [GitHub] Starting scan with {len(queries)} dorks (cap={DORK_CAP}, sleep={QUERY_SLEEP}s)...")
    await _send_log_async(f"🐱 [GitHub] Starting scan with {len(queries)} dorks...")

    for q in queries:
        logger.info(f"    🔎 [GitHub] Dorking: {q}")
        try:
            results = await _cb("github").call(github.search)(q)
            logger.info(f"    ✅ [GitHub] Dork returned {len(results)} matches.")

            saved = await _save_credentials_async(results, "github")
            total_saved += saved

        except Exception as e:
            err_str = str(e)
            logger.error(f"    ❌ [GitHub] Dork failed: {err_str}")
            errors.append(err_str)
            errors = errors[-MAX_ERRORS_BUFFER:]
            # Secondary rate limit — back off harder
            if "secondary" in err_str.lower() or "rate limit" in err_str.lower():
                logger.warning("    ⏳ [GitHub] Secondary rate limit hit — sleeping 60s")
                await asyncio.sleep(60)
                continue

        await asyncio.sleep(QUERY_SLEEP)

    if errors:
        logger.warning(f"⚠️ [GitHub] Completed with {len(errors)} errors.")

    result_msg = f"GitHub scan finished. Saved {total_saved} unique credentials."
    if errors:
        result_msg += f" (Encountered {len(errors)} errors)"
    await _send_log_async(f"🏁 [GitHub] Finished. Saved {total_saved} unique credentials.")
    return result_msg


@app.task(
    name="scanner.scan_fofa",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def scan_fofa(self, query: str = None, country_code: str = None):
    return _run_sync(_scan_fofa_async(self, query, country_code))


async def _scan_fofa_async(task_self, query: str = None, country_code: str = None):
    queries = [query] if query else FOFA_DEFAULT_QUERIES

    selected_country = country_code
    if country_code == "RANDOM":
        selected_country = random.choice(settings.TARGET_COUNTRIES)
        logger.info(f"    [FOFA] Randomly selected country: {selected_country}")

    logger.info(
        f"🔍 [FOFA] Starting Scan | Queries: {len(queries)} | Country: {selected_country or 'Global'}"
    )

    # Check Pause State
    if redis_client.get("system:paused"):
        logger.warning("⏸️ [FOFA] System is PAUSED. Skipping scan.")
        return "System Paused"

    await _send_log_async(
        f"🔍 [FOFA] Starting scan with {len(queries)} queries (Country: {selected_country})..."
    )

    total_saved = 0
    errors = []

    for q in queries:
        try:
            logger.info(f"    🔎 [FOFA] Executing query: {q}")
            results = await _cb("fofa").call(fofa.search)(query=q, country_code=selected_country)
            logger.info(f"    ✅ [FOFA] Query returned {len(results)} raw results.")

            saved = await _save_credentials_async(results, "fofa")
            total_saved += saved
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"    ❌ [FOFA] Query failed: {str(e)}")
            errors.append(str(e))
            errors = errors[-MAX_ERRORS_BUFFER:]
            if task_self.request.retries < task_self.max_retries:
                # We can't use self.retry inside async without care?
                # Celery retry raises an exception.
                # Just log for now to avoid complexity in this async wrapper
                pass

    result_msg = f"FOFA scan finished. Saved {total_saved} new credentials."
    if errors:
        result_msg += f" (Errors: {len(errors)})"
        await _send_log_async(f"❌ [FOFA] Completed with errors: {errors[0]}...")
    else:
        await _send_log_async(f"🏁 [FOFA] Finished. Saved {total_saved} new credentials.")

    logger.info(f"🏁 [FOFA] Finished | Total Saved: {total_saved} | Errors: {len(errors)}")
    return result_msg


@app.task(name="scanner.scan_gitlab", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_gitlab(query: str = None):
    return _run_sync(_scan_gitlab_async(query))


async def _scan_gitlab_async(query: str = None):
    return await _run_scanner(
        label="GitLab",
        search_fn=gitlab_srv.search,
        source_name="gitlab",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
        cooldown_key="cooldown:scanner:gitlab_api_broken",
        cooldown_env="GITLAB_BROKEN_COOLDOWN_SECS",
    )
@app.task(
    name="scanner.scan_bitbucket", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_bitbucket(query: str = None):
    return _run_sync(_scan_bitbucket_async(query))


async def _scan_bitbucket_async(query: str = None):
    return await _run_scanner(
        label="Bitbucket",
        search_fn=bitbucket_srv.search,
        source_name="bitbucket",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
@app.task(name="scanner.scan_gist", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_gist(query: str = None):
    return _run_sync(_scan_gist_async(query))


async def _scan_gist_async(query: str = None):
    return await _run_scanner(
        label="Gist",
        search_fn=gist_srv.search,
        source_name="gist",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
@app.task(
    name="scanner.scan_grepapp", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_grepapp(query: str = None):
    return _run_sync(_scan_grepapp_async(query))


async def _scan_grepapp_async(query: str = None):
    return await _run_scanner(
        label="GrepApp",
        search_fn=grepapp_srv.search,
        source_name="grepapp",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
@app.task(
    name="scanner.scan_publicwww", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_publicwww(query: str = None):
    return _run_sync(_scan_publicwww_async(query))


async def _scan_publicwww_async(query: str = None):
    return await _run_scanner(
        label="PublicWWW",
        search_fn=publicwww_srv.search,
        source_name="publicwww",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
@app.task(
    name="scanner.scan_pastebin", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_pastebin(query: str = None):
    return _run_sync(_scan_pastebin_async(query))


async def _scan_pastebin_async(query: str = None):
    return await _run_scanner(
        label="Pastebin",
        search_fn=pastebin_srv.search,
        source_name="pastebin",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )

@app.task(
    name="scanner.scan_rentry", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_rentry(query: str = None):
    return _run_sync(_scan_rentry_async(query))


async def _scan_rentry_async(query: str = None):
    from app.services.scanners_extension import RentryService
    rentry_srv = RentryService()
    return await _run_scanner(
        label="Rentry",
        search_fn=rentry_srv.search,
        source_name="rentry",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )

@app.task(
    name="scanner.scan_hastebin", autoretry_for=(Exception,), retry_backoff=True, max_retries=2
)
def scan_hastebin(query: str = None):
    return _run_sync(_scan_hastebin_async(query))


async def _scan_hastebin_async(query: str = None):
    from app.services.scanners_extension import HastebinService
    hastebin_srv = HastebinService()
    return await _run_scanner(
        label="Hastebin",
        search_fn=hastebin_srv.search,
        source_name="hastebin",
        save_fn=_save_credentials_async,
        send_log_fn=_send_log_async,
        redis_client=redis_client,
    )
@app.task(name="scanner.scan_exa", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_exa(query: str = None):
    """
    Paste-site scan via Exa neural search API.
    Replaces Serper — Exa returns full page content directly so no second
    HTTP fetch is needed. Scoped to pastebin/hastebin/rentry/ghostbin/paste.ee.
    """
    return _run_sync(_scan_exa_async(query))


async def _scan_exa_async(query: str = None):
    if redis_client.get("system:paused"):
        return "System Paused"

    # Guard: skip if Exa key is broken (401/403)
    EXA_COOLDOWN_KEY = "cooldown:scanner:exa_api_broken"
    EXA_COOLDOWN_TTL = int(os.getenv("EXA_BROKEN_COOLDOWN_SECS", 82800))  # 23h default
    if redis_client.get(EXA_COOLDOWN_KEY):
        ttl = redis_client.ttl(EXA_COOLDOWN_KEY)
        logger.info(f"⏭️ [Exa] API key on cooldown ({ttl}s remaining) — skipping scan.")
        return "Exa API key on cooldown — skipped."

    queries = [query] if query else [
        '"api.telegram.org/bot"',
        '"TELEGRAM_BOT_TOKEN"',
        '"bot_token" "telegram"',
    ]

    logger.info(f"🔍 [Exa] Starting paste-site scan ({len(queries)} queries)...")
    await _send_log_async(f"🔍 [Exa] Scanning paste sites via Exa ({len(queries)} queries)...")

    total_saved = 0
    errors = []

    for q in queries:
        try:
            results = await exa_srv.search(q)
            logger.info(f"    ✅ [Exa] Query '{q[:50]}' returned {len(results)} matches.")
            saved = await _save_credentials_async(results, "exa")
            total_saved += saved
        except Exception as e:
            err_str = str(e)
            logger.error(f"    ❌ [Exa] Query failed: {err_str}")
            errors.append(err_str)
            if "401" in err_str or "403" in err_str or "auth" in err_str.lower():
                redis_client.set(EXA_COOLDOWN_KEY, "1", ex=EXA_COOLDOWN_TTL)
                msg = f"⚠️ [Exa] Auth error — API key invalid or quota exhausted. Cooldown set for {EXA_COOLDOWN_TTL//3600}h. Check dashboard.exa.ai."
                logger.warning(msg)
                await _send_log_async(msg)
                break
        await asyncio.sleep(2)

    msg = f"Exa scan finished. Saved {total_saved} new credentials."
    await _send_log_async(f"🏁 [Exa] Finished. Saved {total_saved} new credentials.")
    return msg



@app.task(name="scanner.retry_cold")
def retry_cold():
    return _run_sync(_retry_cold_async())


async def _retry_cold_async():
    if redis_client.get("system:paused"):
        return "System Paused"

    from datetime import datetime, timedelta

    from app.workers.tasks.flow_tasks import async_execute, enrich_credential

    logger.info("🔄 [RetryCold] Starting retry for low-viability but valid tokens...")
    await _send_log_async("🔄 [RetryCold] Starting chat discovery retry for cold valid tokens...")

    threshold = datetime.now(UTC) - timedelta(hours=6)
    threshold_str = threshold.strftime("%Y-%m-%dT%H:%M:%S")

    res = await async_execute(
        db.table("discovered_credentials")
        .select("id, meta")
        .eq("meta->>retryable", "true")
        .filter("meta->>retry_reason", "in", '("low_viability","no_chat_evidence")')
        .lt("meta->>last_gate_at", threshold_str)
    )

    retried = 0
    for row in res.data:
        cred_id = row["id"]
        reason = (row.get("meta") or {}).get("retry_reason", "")
        try:
            logger.info(f"    🔁 [RetryCold] Retrying {cred_id} (reason: {reason})")
            # Fetch current meta first then merge — avoid overwriting topic_id, bot info, etc.
            cur = await async_execute(db.table("discovered_credentials").select("meta").eq("id", cred_id).single())
            existing_meta = (cur.data or {}).get("meta") or {}
            existing_meta["retryable"] = False
            existing_meta["last_retry_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            await async_execute(
                db.table("discovered_credentials")
                .update({"meta": existing_meta})
                .eq("id", cred_id)
            )
            enrich_credential.delay(cred_id)
            retried += 1
        except Exception as e:
            logger.error(f"    ❌ [RetryCold] Failed to retry {cred_id}: {e}")

    result_msg = f"RetryCold finished. Retried {retried} tokens."
    await _send_log_async(f"🏁 [RetryCold] Finished. Retried {retried} tokens.")
    return result_msg


@app.task(name="scanner.scan_google", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_google(query: str = None):
    return _run_sync(_scan_google_async(query))


async def _scan_google_async(query: str = None):
    if redis_client.get("system:paused"):
        return "System Paused"

    # Guard: skip entire run if Google CSE key is known-broken (403 = quota exhausted or key invalid).
    # A 403 on the first dork means all subsequent dorks will also fail — no point running them.
    # The cooldown (default 23h) prevents hammering a dead key every scan cycle.
    GOOGLE_COOLDOWN_KEY = "cooldown:scanner:google_api_broken"
    GOOGLE_COOLDOWN_TTL = int(os.getenv("GOOGLE_BROKEN_COOLDOWN_SECS", 82800))  # 23h default
    if redis_client.get(GOOGLE_COOLDOWN_KEY):
        ttl = redis_client.ttl(GOOGLE_COOLDOWN_KEY)
        logger.info(f"⏭️ [Google] API key on cooldown ({ttl}s remaining) — skipping scan.")
        return "Google API key on cooldown — skipped."

    dorks = [
        'site:pastebin.com "api.telegram.org/bot"',
        'site:github.com "TELEGRAM_BOT_TOKEN" filetype:env',
        'site:gitlab.com "api.telegram.org/bot"',
        'site:hastebin.com "api.telegram.org/bot"',
        'site:rentry.co "api.telegram.org/bot"',
        '"api.telegram.org/bot" filetype:py',
        '"api.telegram.org/bot" filetype:js',
        '"api.telegram.org/bot" filetype:env',
    ]
    if query:
        dorks = [query]

    logger.info(f"🔍 [Google] Starting scan with {len(dorks)} dorks...")
    await _send_log_async(f"🔍 [Google] Starting scan with {len(dorks)} dorks...")

    total_saved = 0
    errors = []

    for dork in dorks:
        try:
            results = await google_srv.search(dork)
            logger.info(f"    ✅ [Google] Dork returned {len(results)} results.")
            saved = await _save_credentials_async(results, "google_dork")
            total_saved += saved
        except Exception as e:
            err_str = str(e)
            logger.error(f"    ❌ [Google] Dork failed: {err_str}")
            errors.append(err_str)
            errors = errors[-MAX_ERRORS_BUFFER:]
            # 403 = quota exhausted or key invalid — all remaining dorks will also fail.
            # Set cooldown and abort early rather than burning time on dead requests.
            if "403" in err_str or "Forbidden" in err_str:
                redis_client.set(GOOGLE_COOLDOWN_KEY, "1", ex=GOOGLE_COOLDOWN_TTL)
                msg = f"⚠️ [Google] 403 Forbidden — API key quota exhausted or invalid. Cooldown set for {GOOGLE_COOLDOWN_TTL//3600}h. Check billing/quota at console.cloud.google.com."
                logger.warning(msg)
                await _send_log_async(msg)
                break
        await asyncio.sleep(2)

    result_msg = f"Google scan finished. Saved {total_saved} new credentials."
    if errors:
        result_msg += f" (Errors: {len(errors)})"
    await _send_log_async(f"🏁 [Google] Finished. Saved {total_saved} new credentials.")
    return result_msg


@app.task(name="scanner.scan_shodan_c2", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_shodan_c2():
    """
    Dedicated Shodan scan targeting Telegram-based C2/RAT infrastructure.
    These are live infected hosts using Telegram as a command-and-control channel —
    highest token yield because the token is actively in use on a running server.
    """
    return _run_sync(_scan_shodan_c2_async())


async def _scan_shodan_c2_async():
    if redis_client.get("system:paused"):
        return "System Paused"

    # Each query is a focused slice of the compound C2 query.
    # Shodan doesn't support deeply nested OR/AND in a single query reliably,
    # so we split into targeted sub-queries and deduplicate results.
    C2_QUERIES = [
        # Header-based detection — most precise, server is actively serving bot API
        'http.headers:"X-Telegram-Bot-Api"',
        # Malware category keywords paired with Telegram bot pattern
        'http.body:"api.telegram.org/bot" http.body:"malware" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"rat" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"remote access" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"spyware" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"stealer" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"keylogger" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"c2 server" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"command and control" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"exploit" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"bypass" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"inject" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"persistence" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"privilege escalation" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        # /start command with payload patterns — bot is receiving C2 commands
        'http.body:"api.telegram.org/bot" http.body:"/start payload=" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start token=" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start cmd=" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start c2=" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start key=" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start id=" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start /bin/bash" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start /powershell" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start download" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start /exec" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start /run" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start /invoke" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start /script" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start http://" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
        'http.body:"api.telegram.org/bot" http.body:"/start https://" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    ]

    logger.info(f"🎯 [Shodan-C2] Starting C2 scan with {len(C2_QUERIES)} targeted queries...")
    await _send_log_async(f"🎯 [Shodan-C2] Starting Telegram C2 infrastructure scan ({len(C2_QUERIES)} queries)...")

    total_saved = 0
    errors = []

    for q in C2_QUERIES:
        if not q:
            continue
        try:
            logger.info(f"    🔎 [Shodan-C2] Query: {q[:80]}...")
            results = await shodan.search(q)
            logger.info(f"    ✅ [Shodan-C2] Returned {len(results)} results")
            saved = await _save_credentials_async(results, "shodan_c2")
            total_saved += saved
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"    ❌ [Shodan-C2] Query failed: {e}")
            errors.append(str(e))
            errors = errors[-MAX_ERRORS_BUFFER:]

    result_msg = f"Shodan C2 scan finished. Saved {total_saved} new credentials."
    if errors:
        result_msg += f" (Errors: {len(errors)})"
    await _send_log_async(f"🏁 [Shodan-C2] Finished. Saved {total_saved} credentials from C2 infrastructure.")
    return result_msg


# Query constants extracted to _scanner/queries.py — re-exported here so
# all existing references in this file continue to work unchanged.
# Generic scanner base — used by the 5 structurally identical simple scanners.
import contextlib

from app.workers.tasks._scanner.base import _run_scanner
from app.workers.tasks._scanner.queries import (  # noqa: F401
    FOFA_DEFAULT_QUERIES,
    NETLAS_QUERIES,
    SHODAN_DEFAULT_QUERIES,
    _shodan_body_query,
)


@app.task(name="scanner.scan_netlas", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_netlas(query: str = None):
    """
    Netlas scan — runs once daily, respects per-account request limits.
    Rotates between account 1 (45 req/day) and account 2 (90 req/day).
    Queries are ordered by yield; stops when daily budget is exhausted.
    """
    return _run_sync(_scan_netlas_async(query))


async def _scan_netlas_async(query: str = None):
    if redis_client.get("system:paused"):
        return "System Paused"

    queries = [query] if query else NETLAS_QUERIES

    # Log current budget before starting
    usage = await netlas_srv.get_usage_summary()
    remaining = sum(v["remaining"] for v in usage.values())
    if remaining == 0:
        msg = "⏸️ [Netlas] Daily budget exhausted for all accounts — skipping"
        logger.warning(msg)
        await _send_log_async(msg)
        return "Daily limit reached"

    logger.info(f"🔍 [Netlas] Starting scan | Budget remaining: {remaining} requests | Queries: {len(queries)}")
    await _send_log_async(
        f"🔍 [Netlas] Starting scan ({remaining} requests remaining today, {len(queries)} queries queued)..."
    )

    total_saved = 0
    errors = []
    queries_run = 0

    for q in queries:
        # Re-check budget before each query
        usage = await netlas_srv.get_usage_summary()
        remaining = sum(v["remaining"] for v in usage.values())
        if remaining == 0:
            logger.warning("    [Netlas] Budget exhausted mid-scan — stopping early")
            await _send_log_async(f"⏸️ [Netlas] Budget exhausted after {queries_run} queries.")
            break

        try:
            logger.info(f"    🔎 [Netlas] Query: {q[:70]}...")
            results = await netlas_srv.search(q, size=20)
            logger.info(f"    ✅ [Netlas] Returned {len(results)} results")
            saved = await _save_credentials_async(results, "netlas")
            total_saved += saved
            queries_run += 1
            await asyncio.sleep(1)  # gentle rate limiting
        except Exception as e:
            logger.error(f"    ❌ [Netlas] Query failed: {e}")
            errors.append(str(e))
            errors = errors[-MAX_ERRORS_BUFFER:]

    # Final usage summary
    usage = await netlas_srv.get_usage_summary()
    usage_str = " | ".join(
        f"Acct#{k.split('_')[1]}: {v['used']}/{v['limit']}"
        for k, v in usage.items()
    )

    result_msg = f"Netlas scan finished. Ran {queries_run} queries, saved {total_saved} credentials."
    if errors:
        result_msg += f" (Errors: {len(errors)})"

    await _send_log_async(
        f"🏁 [Netlas] Finished. Saved {total_saved} credentials. Usage today: {usage_str}"
    )
    return result_msg


@app.task(name="scanner.scan_wayback", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_wayback(query: str = None):
    """
    Wayback Machine historical URL scanner.
    Free, no API key, no cooldown gating needed (CDX is free + permissive).
    """
    return _run_sync(_scan_wayback_async(query))


async def _scan_wayback_async(query: str = None):
    if redis_client.get("system:paused"):
        return "System Paused"

    logger.info("🔍 [Wayback] Starting historical URL scan...")
    await _send_log_async("🔍 [Wayback] Scanning Internet Archive for token leaks...")

    try:
        limit = int(os.getenv("WAYBACK_LIMIT", 500))
        results = await wayback_srv.search(
            query_pattern=query or "api.telegram.org",
            limit=limit,
        )

        if results:
            saved = await _save_credentials_async(results, "wayback")
            msg = f"Wayback scan finished. Enqueued {saved} tokens for validation."
        else:
            msg = "Wayback scan finished. 0 matches."

        await _send_log_async(f"🏁 [Wayback] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[Wayback] Error: {e}", exc_info=True)
        raise


@app.task(name="scanner.scan_telegram_search", autoretry_for=(Exception,), retry_backoff=True, max_retries=1)
def scan_telegram_search(query: str = None):
    """
    Telegram MTProto self-search via UserAgent session.

    Different result space from any web scanner — catches token leaks discussed
    in public channels but never indexed by Google/Exa/etc. Bound by
    UserAgent FloodWait cooldown discipline.
    """
    return _run_sync(_scan_telegram_search_async(query))


async def _scan_telegram_search_async(query: str = None):
    if redis_client.get("system:paused"):
        return "System Paused"

    # Per-session cooldown gate — don't even start if all sessions are fried
    from app.services.scanners import _is_valid_token
    from app.services.user_agent_srv import user_agent

    logger.info("🔍 [TelegramSearch] Starting MTProto global search...")
    await _send_log_async("🔍 [TelegramSearch] Querying Telegram public channels...")

    queries = [query] if query else [
        "api.telegram.org/bot",
        "TELEGRAM_BOT_TOKEN",
        "bot_token leaked",
    ]

    # Strict token regex — same shape as TOKEN_PATTERN but inline-safe
    token_re = re.compile(r"\b\d{8,15}:[A-Za-z0-9_-]{35}\b")

    all_results = []
    try:
        for q in queries:
            try:
                messages = await user_agent.search_messages(q, limit=100)
            except Exception as e:
                logger.error(f"    ❌ [TelegramSearch] Query '{q}' failed: {e}")
                continue

            for msg in messages:
                text = msg.get("text", "")
                if not text:
                    continue
                tokens = token_re.findall(text)
                for tok in tokens:
                    if not _is_valid_token(tok):
                        continue
                    all_results.append({
                        "token": tok,
                        "chat_id": msg.get("chat_id"),
                        "meta": {
                            "telegram_chat_name": msg.get("chat_name"),
                            "telegram_message_id": msg.get("message_id"),
                            "telegram_date": msg.get("date"),
                            "telegram_query": q,
                        }
                    })
            # 5s inter-query courtesy delay (search_messages already adds 3s INTER_SLEEP)
            await asyncio.sleep(5)

        if all_results:
            saved = await _save_credentials_async(all_results, "telegram_search")
            msg = f"TelegramSearch finished. Enqueued {saved} tokens for validation."
        else:
            msg = "TelegramSearch finished. 0 matches."

        await _send_log_async(f"🏁 [TelegramSearch] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[TelegramSearch] Error: {e}", exc_info=True)
        raise


@app.task(name="scanner.scan_commoncrawl", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_commoncrawl():
    return _run_sync(_scan_commoncrawl_async())


async def _scan_commoncrawl_async():
    if redis_client.get("system:paused"):
        return "System Paused"
    logger.info("🔍 [CommonCrawl] starting...")
    await _send_log_async("🔍 [CommonCrawl] querying latest crawl index...")
    try:
        limit = int(os.getenv("COMMONCRAWL_LIMIT", 500))
        results = await commoncrawl_srv.search(limit=limit)
        if results:
            saved = await _save_credentials_async(results, "commoncrawl")
            msg = f"CommonCrawl: enqueued {saved} tokens"
        else:
            msg = "CommonCrawl: 0 matches"
        await _send_log_async(f"🏁 [CommonCrawl] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[CommonCrawl] {e}", exc_info=True)
        raise


@app.task(name="scanner.scan_sourcegraph", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_sourcegraph(query: str = None):
    return _run_sync(_scan_sourcegraph_async(query))


async def _scan_sourcegraph_async(query: str = None):
    if redis_client.get("system:paused"):
        return "System Paused"
    logger.info("🔍 [Sourcegraph] starting...")
    await _send_log_async("🔍 [Sourcegraph] streaming search across public repos...")
    try:
        results = await sourcegraph_srv.search(query)
        if results:
            saved = await _save_credentials_async(results, "sourcegraph")
            msg = f"Sourcegraph: enqueued {saved} tokens"
        else:
            msg = "Sourcegraph: 0 matches"
        await _send_log_async(f"🏁 [Sourcegraph] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[Sourcegraph] {e}", exc_info=True)
        raise


@app.task(name="scanner.scan_replit", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_replit():
    return _run_sync(_scan_replit_async())


async def _scan_replit_async():
    if redis_client.get("system:paused"):
        return "System Paused"
    logger.info("🔍 [Replit] starting public repl scan...")
    await _send_log_async("🔍 [Replit] scanning public repls for token leaks...")
    try:
        results = await replit_srv.search()
        if results:
            saved = await _save_credentials_async(results, "replit")
            msg = f"Replit: enqueued {saved} tokens"
        else:
            msg = "Replit: 0 matches"
        await _send_log_async(f"🏁 [Replit] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[Replit] {e}", exc_info=True)
        raise


@app.task(name="scanner.scan_postman", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_postman():
    return _run_sync(_scan_postman_async())


async def _scan_postman_async():
    if redis_client.get("system:paused"):
        return "System Paused"
    logger.info("🔍 [Postman] starting public workspace scan...")
    await _send_log_async("🔍 [Postman] scanning public collections for token leaks...")
    try:
        results = await postman_srv.search()
        if results:
            saved = await _save_credentials_async(results, "postman")
            msg = f"Postman: enqueued {saved} tokens"
        else:
            msg = "Postman: 0 matches"
        await _send_log_async(f"🏁 [Postman] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[Postman] {e}", exc_info=True)
        raise

@app.task(name="scanner.scan_dockerhub", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_dockerhub():
    """
    Search Docker Hub for 'telegram-bot' images and inspect their manifests/config
    for hardcoded ENV variables containing bot tokens.
    """
    return _run_sync(_scan_dockerhub_async())

async def _scan_dockerhub_async():
    if redis_client.get("system:paused"):
        return "System Paused"

    import httpx

    from app.services.scanners import TOKEN_PATTERN, _is_valid_token

    logger.info("🐳 [DockerHub] starting image manifest scan...")
    await _send_log_async("🐳 [DockerHub] scanning public image manifests for ENV leaks...")

    results = []
    queries = ["telegram bot", "telegram-bot", "tgbot", "tg-bot"]
    seen_images = set()

    # Free, unauthenticated Docker registry API endpoints
    SEARCH_URL = "https://hub.docker.com/v2/search/repositories"
    AUTH_URL = "https://auth.docker.io/token"
    REGISTRY_URL = "https://registry-1.docker.io/v2"

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        # Step 1: Find images via Search API
        for q in queries:
            try:
                res = await client.get(SEARCH_URL, params={"query": q, "page_size": 20})
                if res.status_code != 200: continue
                data = res.json()
                for repo in data.get("results", []):
                    img_name = repo.get("repo_name")
                    if img_name:
                        seen_images.add(img_name)
            except Exception as e:
                logger.warning(f"    [DockerHub] search failed for {q}: {e}")

        logger.info(f"    [DockerHub] Found {len(seen_images)} repositories to inspect.")

        # Step 2: Fetch manifest config for each image
        for img_name in list(seen_images)[:50]:  # Cap at 50 per run to respect pull rate limits
            if "/" not in img_name:
                img_name = f"library/{img_name}" # Official images

            # Dedup check
            seen_key = f"dockerhub:seen:{img_name}"
            try:
                if redis_client.exists(seen_key): continue
            except Exception: pass

            try:
                # 2a. Get anonymous bearer token for the specific repo
                auth_res = await client.get(AUTH_URL, params={"service": "registry.docker.io", "scope": f"repository:{img_name}:pull"})
                if auth_res.status_code != 200: continue
                token = auth_res.json().get("token")
                if not token: continue

                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json"
                }

                # 2b. Get manifest for the 'latest' tag
                manifest_res = await client.get(f"{REGISTRY_URL}/{img_name}/manifests/latest", headers=headers)
                if manifest_res.status_code != 200: continue
                manifest_data = manifest_res.json()

                # 2c. Extract config digest
                config_digest = manifest_data.get("config", {}).get("digest")
                if not config_digest: continue

                # 2d. Fetch the config blob
                blob_res = await client.get(f"{REGISTRY_URL}/{img_name}/blobs/{config_digest}", headers=headers)
                if blob_res.status_code != 200: continue
                config_data = blob_res.json()

                # 2e. Search the 'Env' and 'Cmd' arrays in the container config
                container_config = config_data.get("config", {})
                env_vars = container_config.get("Env", [])
                cmds = container_config.get("Cmd", [])

                text_to_scan = "\n".join(env_vars + cmds)
                found = TOKEN_PATTERN.findall(text_to_scan)

                for t in found:
                    if _is_valid_token(t):
                        results.append({
                            "token": t,
                            "meta": {"source": "dockerhub", "image": img_name}
                        })

                with contextlib.suppress(Exception):
                    redis_client.setex(seen_key, 7 * 86400, "1")

            except Exception as e:
                logger.debug(f"    [DockerHub] manifest pull failed for {img_name}: {e}")

            await asyncio.sleep(1) # Courtesy delay

    if results:
        saved = await _save_credentials_async(results, "dockerhub")
        msg = f"DockerHub: enqueued {saved} tokens"
    else:
        msg = "DockerHub: 0 matches"

    await _send_log_async(f"🏁 [DockerHub] {msg}")
    return msg


@app.task(name="scanner.scan_searchcode", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_searchcode(query: str = None):
    return _run_sync(_scan_searchcode_async(query))


async def _scan_searchcode_async(query: str = None):
    if redis_client.get("system:paused"):
        return "System Paused"
    logger.info("🔍 [Searchcode] starting public code search...")
    await _send_log_async("🔍 [Searchcode] scanning searchcode.com for token leaks...")
    try:
        results = await searchcode_srv.search(query)
        if results:
            saved = await _save_credentials_async(results, "searchcode")
            msg = f"Searchcode: enqueued {saved} tokens"
        else:
            msg = "Searchcode: 0 matches"
        await _send_log_async(f"🏁 [Searchcode] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[Searchcode] {e}", exc_info=True)
        raise
