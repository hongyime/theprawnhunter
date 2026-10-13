"""Scanner task implementations for netlas."""

from ..common import *

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
            results = await netlas_srv.search(q)
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
