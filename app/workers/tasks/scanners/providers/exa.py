"""Scanner task implementations for exa."""

from ..common import *
from app.infrastructure.scanners.providers.exa import (
    ExaKeyFailure,
    ExaQuotaFailure,
    ExaRateLimitFailure,
    ExaSearchFailure,
)


EXA_RETRY_QUEUE = "scanner:exa:pending_queries"
EXA_DEFAULT_QUERIES = [
    '"api.telegram.org/bot"',
    '"TELEGRAM_BOT_TOKEN"',
    '"bot_token" "telegram"',
]


def _decode_exa_query(value) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


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

    new_queries = [query] if query else EXA_DEFAULT_QUERIES
    queue_error = None
    try:
        pending_queries = [_decode_exa_query(value) for value in redis_client.lrange(EXA_RETRY_QUEUE, 0, -1)]
    except Exception as error:
        pending_queries = []
        queue_error = error
        logger.error(
            "[Exa] Could not read saved searches before this scan: %s. Running current searches and asking Celery to retry.",
            error,
        )

    queries = list(dict.fromkeys(pending_queries + new_queries))
    logger.info("🔍 [Exa] Starting paste-site scan (%s searches)...", len(queries))

    async def send_status(message: str):
        try:
            await _send_log_async(message)
        except Exception as error:
            logger.error("[Exa] Could not send scan status: %s. The scan will continue.", error)

    async def retain_queries(searches: list[str]):
        nonlocal queue_error
        for search in searches:
            try:
                saved = {_decode_exa_query(value) for value in redis_client.lrange(EXA_RETRY_QUEUE, 0, -1)}
                if search not in saved:
                    redis_client.rpush(EXA_RETRY_QUEUE, search)
            except Exception as error:
                queue_error = queue_error or error
                logger.error(
                    "[Exa] Could not keep search %r for the next scan: %s. Celery will retry this task.",
                    search,
                    error,
                )

    async def clear_completed(search: str):
        nonlocal queue_error
        try:
            redis_client.lrem(EXA_RETRY_QUEUE, 0, search)
        except Exception as error:
            queue_error = queue_error or error
            logger.error(
                "[Exa] Search %r completed, but its saved retry entry could not be cleared: %s. Celery will retry this task.",
                search,
                error,
            )

    await send_status(f"🔍 [Exa] Scanning paste sites via Exa ({len(queries)} searches)...")
    total_saved = 0
    failed_searches = 0

    for index, search in enumerate(queries):
        try:
            results = await exa_srv.search(search, raise_on_error=True)
            if results:
                logger.info("[Exa] Search %r returned %s token matches.", search, len(results))
            else:
                logger.info("[Exa] Search %r completed with no token matches.", search)
            saved = await _save_credentials_async(results, "exa")
            total_saved += saved
            await clear_completed(search)
        except (ExaKeyFailure, ExaQuotaFailure, ExaRateLimitFailure) as error:
            failed_searches += 1
            remaining = queries[index:]
            await retain_queries(remaining)
            reason = "no usable key" if isinstance(error, ExaKeyFailure) else "provider limit reached"
            message = (
                f"⚠️ [Exa] Search {search!r} stopped: {error}. Kept {len(remaining)} search(es) "
                f"for the next scan and stopped this run cleanly ({reason})."
            )
            logger.warning(message)
            await send_status(message)
            break
        except ExaSearchFailure as error:
            failed_searches += 1
            await retain_queries([search])
            message = f"❌ [Exa] Search {search!r} failed: {error}. Kept it for the next scan; continuing with other searches."
            logger.error(message)
            await send_status(message)
        except Exception as error:
            failed_searches += 1
            await retain_queries([search])
            message = f"❌ [Exa] Search {search!r} failed: {error}. Kept it for the next scan; continuing with other searches."
            logger.exception(message)
            await send_status(message)

        if index < len(queries) - 1:
            await asyncio.sleep(2)

    if queue_error:
        raise RuntimeError(
            "Exa could not update its saved searches. Celery will retry this scan."
        ) from queue_error

    msg = f"Exa scan finished. Saved {total_saved} new credentials."
    if failed_searches:
        msg += f" Kept {failed_searches} failed search(es) for the next scan."
    await send_status(f"🏁 [Exa] {msg}")
    return msg
