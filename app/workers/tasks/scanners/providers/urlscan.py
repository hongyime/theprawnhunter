"""Scanner task implementations for urlscan."""

from ..common import *

@app.task(name="scanner.scan_urlscan")
def scan_urlscan(query: str = None, country_code: str = None):
    return _run_sync(_scan_urlscan_async(query, country_code))

async def _scan_urlscan_async(query: str = None, country_code: str = None):

    COMMON_QUERIES = [  # noqa: N806 — function-scoped constant
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
