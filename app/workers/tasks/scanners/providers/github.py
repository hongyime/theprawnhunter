"""Scanner task implementations for github."""

import math

from ..common import *
from app.infrastructure.scanners.providers.github import GithubRateLimitError


RETRY_QUERY_KEY = "scanner:github:search:retry"
MAX_CODE_SEARCHES_PER_RUN = 10
MIN_CODE_SEARCH_GAP_SECONDS = 6.1


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
    # GitHub code search allows up to 10 requests per minute.
    # GITHUB_DORK_CAP can lower the per-run cap; it cannot raise it.
    # GITHUB_QUERY_SLEEP can add more time between searches, but not less than 6.1s.
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

    try:
        requested_cap = int(os.getenv("GITHUB_DORK_CAP", 10))
    except ValueError:
        logger.warning("[GitHub] Bad GITHUB_DORK_CAP value; using the 10-search limit.")
        requested_cap = MAX_CODE_SEARCHES_PER_RUN
    DORK_CAP = max(0, min(requested_cap, MAX_CODE_SEARCHES_PER_RUN))  # noqa: N806

    try:
        requested_sleep = float(os.getenv("GITHUB_QUERY_SLEEP", 8))
    except ValueError:
        logger.warning("[GitHub] Bad GITHUB_QUERY_SLEEP value; using 8 seconds.")
        requested_sleep = 8.0
    if not math.isfinite(requested_sleep):
        logger.warning("[GitHub] GITHUB_QUERY_SLEEP must be finite; using 8 seconds.")
        requested_sleep = 8.0
    QUERY_SLEEP = max(requested_sleep, MIN_CODE_SEARCH_GAP_SECONDS)  # noqa: N806
    if requested_sleep < MIN_CODE_SEARCH_GAP_SECONDS:
        logger.warning(
            "[GitHub] GITHUB_QUERY_SLEEP was %.1fs; using %.1fs to stay under code search limits.",
            requested_sleep,
            QUERY_SLEEP,
        )

    try:
        saved_queries = redis_client.smembers(RETRY_QUERY_KEY)
        retry_queries = [
            item.decode() if isinstance(item, bytes) else str(item)
            for item in saved_queries
        ]
    except Exception as error:
        logger.error(
            "[GitHub] Could not read saved searches for retry: %s. Current searches will still run.",
            error,
        )
        retry_queries = []

    if query:
        queued_queries = [query, *retry_queries]
    else:
        queued_queries = [*retry_queries, *default_dorks[:DORK_CAP]]
    queries = list(dict.fromkeys(queued_queries))[:DORK_CAP]

    total_saved = 0
    errors = []

    def keep_for_retry(search_query: str, reason: str) -> None:
        try:
            redis_client.sadd(RETRY_QUERY_KEY, search_query)
        except Exception as storage_error:
            logger.error(
                "[GitHub] Search %r failed (%s), and Redis could not save it for the next scan: %s.",
                search_query,
                reason,
                storage_error,
            )

    # Check Pause State
    if redis_client.get("system:paused"):
        logger.warning("⏸️ [GitHub] System is PAUSED. Skipping scan.")
        return "System Paused"

    if not github.has_token():
        if query:
            keep_for_retry(query, "login token is missing")
        logger.warning(
            "[GitHub] Login token is missing or empty; skipped searches %r. "
            "Saved searches remain queued for a later scan. Set GITHUB_TOKEN or GITHUB_TOKENS.",
            queries,
        )
        return "GitHub login token missing"

    logger.info(f"🐱 [GitHub] Starting scan with {len(queries)} dorks (cap={DORK_CAP}, sleep={QUERY_SLEEP}s)...")
    await _send_log_async(f"🐱 [GitHub] Starting scan with {len(queries)} dorks...")

    # Code search has no pushed-date qualifier, so the service filters matches
    # using repository push dates from this rolling cutoff.
    pushed_since = github.recent_push_cutoff()

    for index, q in enumerate(queries):
        logger.info(f"    🔎 [GitHub] Dorking: {q}")
        try:
            results = await _cb("github").call(github.search)(q, pushed_since=pushed_since)
            logger.info(f"    ✅ [GitHub] Dork returned {len(results)} matches.")

            saved = await _save_credentials_async(results, "github")
            total_saved += saved

            try:
                redis_client.srem(RETRY_QUERY_KEY, q)
            except Exception as error:
                logger.warning(
                    "[GitHub] Search %r succeeded, but its saved retry entry could not be cleared: %s. "
                    "It may run again on a later scan.",
                    q,
                    error,
                )

        except GithubRateLimitError as error:
            err_str = str(error)
            keep_for_retry(q, err_str)
            logger.error(
                "[GitHub] Search %r hit a rate limit: %s. Waiting %.1fs, then stopping this scan; "
                "the failed search is saved for a later scan.",
                q,
                err_str,
                error.wait_seconds,
            )
            errors.append(f"{q}: {err_str}")
            await asyncio.sleep(error.wait_seconds)
            break
        except Exception as e:
            err_str = str(e)
            keep_for_retry(q, err_str)
            logger.error(
                "[GitHub] Search %r failed: %s. Saved it for a later scan; continuing with the other searches.",
                q,
                err_str,
            )
            errors.append(f"{q}: {err_str}")
            errors = errors[-MAX_ERRORS_BUFFER:]

        if index < len(queries) - 1:
            await asyncio.sleep(QUERY_SLEEP)

    if errors:
        logger.warning(
            "[GitHub] Completed with %d failed searches; saved for later scans: %s",
            len(errors),
            "; ".join(errors[:3]),
        )

    result_msg = f"GitHub scan finished. Saved {total_saved} unique credentials."
    if errors:
        result_msg += f" (Encountered {len(errors)} errors)"
    await _send_log_async(
        f"🏁 [GitHub] Finished. Saved {total_saved} unique credentials; "
        f"{len(errors)} searches failed and were saved for later scans."
    )
    return result_msg
