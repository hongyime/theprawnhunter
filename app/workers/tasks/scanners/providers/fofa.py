"""Scanner task implementations for fofa."""

from ..common import *

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
