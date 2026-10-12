"""Scanner task implementations for shodan."""

from ..common import *

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
    C2_QUERIES = [  # noqa: N806 — function-scoped constant
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
