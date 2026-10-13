"""Scanner task implementations for netlas."""

from ..common import *
from app.infrastructure.scanners.providers.netlas import (
    NetlasDailyLimitReached,
    NetlasMissingApiKey,
    NetlasRateLimitError,
    NetlasRejectedApiKey,
)


NETLAS_REQUEST_WAIT_SECONDS = 1
NETLAS_RESULTS_PER_RUN = (
    NetlasService.MAX_REQUESTS_PER_RUN * NetlasService.RESULTS_PER_REQUEST
)


async def _log_netlas(level: str, message: str) -> None:
    getattr(logger, level)(message)
    try:
        await _send_log_async(message)
    except Exception as e:
        logger.warning(
            f"[Netlas] Could not send status log: {type(e).__name__}: {e}. "
            "The worker log remains available and the scan will continue."
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

    if not netlas_srv.has_api_keys:
        await _log_netlas(
            "warning",
            "⚠️ [Netlas] No API key is set. No searches were sent; check NETLAS_API_KEY_1 / _2. "
            "The queries will be tried again on the next scan.",
        )
        return "No Netlas API key configured"

    try:
        usage = await netlas_srv.get_usage_summary()
    except Exception as e:
        await _log_netlas(
            "error",
            f"❌ [Netlas] Could not read daily request usage: {type(e).__name__}: {e}. "
            "No searches were sent; this scan is stopping and the queries remain for the next scan.",
        )
        return "Netlas usage unavailable"

    remaining = sum(v["remaining"] for v in usage.values())
    if remaining == 0:
        await _log_netlas(
            "warning",
            "⏸️ [Netlas] Daily budget is exhausted for all accounts. No searches were sent; "
            "the queries will be tried again after the daily limit resets.",
        )
        return "Daily limit reached"

    await _log_netlas(
        "info",
        f"🔍 [Netlas] Starting scan ({remaining} requests remaining today, {len(queries)} queries queued)..."
    )

    total_saved = 0
    results_seen = 0
    errors = []
    queries_run = 0
    requests_attempted = 0
    rate_limit_waits = 0

    for q in queries[: netlas_srv.MAX_REQUESTS_PER_RUN]:
        try:
            usage = await netlas_srv.get_usage_summary()
        except Exception as e:
            await _log_netlas(
                "error",
                f"❌ [Netlas] Could not read daily request usage before search {q[:70]!r}: "
                f"{type(e).__name__}: {e}. This scan is stopping; unfinished searches stay for the next scan.",
            )
            break

        remaining = sum(v["remaining"] for v in usage.values())
        if remaining == 0:
            await _log_netlas(
                "warning",
                f"⏸️ [Netlas] Daily budget ran out before search {q[:70]!r}. "
                "This scan is stopping; unfinished searches will be tried after the daily limit resets.",
            )
            break

        try:
            requests_attempted += 1
            await _log_netlas("info", f"🔎 [Netlas] Search: {q[:70]}...")
            results = await netlas_srv.search(q)
            remaining_results = NETLAS_RESULTS_PER_RUN - results_seen
            results_seen += min(len(results), remaining_results)
            clipped_results = results[:remaining_results]
            await _log_netlas("info", f"✅ [Netlas] Search returned {len(results)} results")
            saved = await _save_credentials_async(clipped_results, "netlas")
            total_saved += saved
            queries_run += 1
            if len(clipped_results) < len(results):
                await _log_netlas(
                    "warning",
                    f"⚠️ [Netlas] Results-per-run limit ({NETLAS_RESULTS_PER_RUN}) reached at search "
                    f"{q[:70]!r}; later searches are left for the next scan.",
                )
                break
            if results_seen >= NETLAS_RESULTS_PER_RUN:
                await _log_netlas(
                    "warning",
                    f"⚠️ [Netlas] Results-per-run limit ({NETLAS_RESULTS_PER_RUN}) reached at search "
                    f"{q[:70]!r}; later searches are left for the next scan.",
                )
                break
            await asyncio.sleep(NETLAS_REQUEST_WAIT_SECONDS)
        except NetlasRateLimitError as e:
            errors.append(str(e))
            errors = errors[-MAX_ERRORS_BUFFER:]
            wait_seconds = max(1, e.retry_after)
            await _log_netlas(
                "error",
                f"❌ [Netlas] Search {q[:70]!r} was rate-limited: {e}. Waiting {wait_seconds} seconds, "
                "then continuing with the next search. The failed search will be tried again on the next scan.",
            )
            await asyncio.sleep(wait_seconds)
            rate_limit_waits += 1
        except NetlasRejectedApiKey as e:
            errors.append(str(e))
            errors = errors[-MAX_ERRORS_BUFFER:]
            await _log_netlas(
                "warning",
                f"⚠️ [Netlas] Search {q[:70]!r} was rejected because the API key was not accepted: {e}. "
                "This scan is stopping; check the key. The searches will be tried again on the next scan.",
            )
            break
        except NetlasMissingApiKey as e:
            await _log_netlas(
                "warning",
                f"⚠️ [Netlas] Search {q[:70]!r} cannot run: {e}. This scan is stopping; "
                "check the key settings, and the search will be tried again on the next scan.",
            )
            break
        except NetlasDailyLimitReached as e:
            await _log_netlas(
                "warning",
                f"⏸️ [Netlas] Search {q[:70]!r} was not sent: {e}. This scan is stopping; "
                "the search will be tried again after the daily limit resets.",
            )
            break
        except Exception as e:
            errors.append(f"{q[:70]}: {type(e).__name__}: {e}")
            errors = errors[-MAX_ERRORS_BUFFER:]
            await _log_netlas(
                "error",
                f"❌ [Netlas] Search {q[:70]!r} failed: {type(e).__name__}: {e}. "
                "Continuing with the next search; this failed search will be tried again on the next scan.",
            )
            await asyncio.sleep(NETLAS_REQUEST_WAIT_SECONDS)

    if (
        requests_attempted >= netlas_srv.MAX_REQUESTS_PER_RUN
        and len(queries) > requests_attempted
    ):
        await _log_netlas(
            "warning",
            f"⚠️ [Netlas] Request-per-run limit ({netlas_srv.MAX_REQUESTS_PER_RUN}) reached. "
            "Unsent searches will be tried again on the next scan.",
        )

    try:
        usage = await netlas_srv.get_usage_summary()
        usage_str = " | ".join(
            f"Acct#{k.split('_')[1]}: {v['used']}/{v['limit']}"
            for k, v in usage.items()
        ) or "unavailable"
    except Exception as e:
        usage_str = "unavailable"
        await _log_netlas(
            "error",
            f"❌ [Netlas] Searches finished, but final daily usage could not be read: "
            f"{type(e).__name__}: {e}. Searches will run again on the next scheduled scan.",
        )

    result_msg = (
        f"Netlas scan finished. Ran {queries_run} searches, saved {total_saved} credentials. "
        f"Requests sent: {requests_attempted}; results checked: {results_seen}."
    )
    if errors:
        result_msg += f" (Errors: {len(errors)})"

    if rate_limit_waits:
        result_msg += f" Waited the requested time for {rate_limit_waits} rate-limit replies."

    await _log_netlas(
        "info",
        f"🏁 [Netlas] Finished. Saved {total_saved} credentials. Usage today: {usage_str}"
    )
    return result_msg
