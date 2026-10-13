"""Scanner task implementations for shodan."""

import json

from ..common import *

SHODAN_RETRY_QUERY_KEY = "scanner:shodan:retry_queries"


def _shodan_retry_query_value(query: str, country_code: str = None) -> str:
    return json.dumps(
        {"query": query, "country_code": country_code},
        separators=(",", ":"),
        sort_keys=True,
    )


def _load_shodan_retry_queries() -> list[tuple[str, str | None]]:
    try:
        values = redis_client.smembers(SHODAN_RETRY_QUERY_KEY)
    except Exception as error:
        logger.error(
            "[Shodan] Could not read saved retry searches (%s); continuing with this run's search list",
            type(error).__name__,
        )
        return []

    queries = []
    for value in values:
        try:
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            item = json.loads(value)
            if isinstance(item, dict) and isinstance(item.get("query"), str) and item["query"]:
                queries.append((item["query"], item.get("country_code")))
            else:
                raise ValueError("saved search has no query")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            logger.warning(
                "[Shodan] Could not read a saved retry search (%s); removing the unreadable entry",
                type(error).__name__,
            )
            try:
                redis_client.srem(SHODAN_RETRY_QUERY_KEY, value)
            except Exception as remove_error:
                logger.error(
                    "[Shodan] Could not remove an unreadable retry entry (%s)",
                    type(remove_error).__name__,
                )
    return queries


def _remember_shodan_retry_query(query: str, country_code: str = None):
    try:
        redis_client.sadd(SHODAN_RETRY_QUERY_KEY, _shodan_retry_query_value(query, country_code))
    except Exception as error:
        logger.error(
            "[Shodan] Could not save search %r for the next run (%s)",
            query,
            type(error).__name__,
        )


def _clear_shodan_retry_query(query: str, country_code: str = None):
    try:
        redis_client.srem(SHODAN_RETRY_QUERY_KEY, _shodan_retry_query_value(query, country_code))
    except Exception as error:
        logger.error(
            "[Shodan] Search %r succeeded, but its saved retry entry could not be cleared (%s)",
            query,
            type(error).__name__,
        )


async def _send_shodan_log(message: str):
    try:
        await _send_log_async(message)
    except Exception as error:
        logger.warning(
            "[Shodan] Could not send the system log (%s); the worker log has the message",
            type(error).__name__,
        )


def _shodan_key_is_ready() -> bool:
    if str(getattr(shodan, "api_key", "") or "").strip():
        return True
    logger.warning(
        "[Shodan] API key is missing or empty; no searches were sent. "
        "Set SHODAN_KEY and the next scheduled scan will try again."
    )
    return False


def _shodan_error_text(error: Exception) -> str:
    message = str(error)
    api_key = str(getattr(shodan, "api_key", "") or "").strip()
    if api_key:
        message = message.replace(api_key, "[hidden]")
    return message or type(error).__name__


async def _log_shodan_search_failure(query: str, error: Exception, label: str = "Shodan") -> bool:
    stop_run = bool(getattr(error, "stop_scan", False))
    next_step = (
        "stopping this run to avoid more provider requests"
        if stop_run
        else "keeping this search for the next scheduled scan and continuing with the other searches"
    )
    message = f"[{label}] Search {query!r} failed: {_shodan_error_text(error)}; {next_step}."
    logger.error(message)
    await _send_shodan_log(f"❌ {message}")
    return stop_run


async def _run_shodan_search(query: str, country_code: str = None):
    return await _cb("shodan").call(shodan.search)(query, country_code=country_code)


@app.task(name="scanner.scan_shodan")
def scan_shodan(query: str = None, country_code: str = None):
    return _run_sync(_scan_shodan_async(query, country_code))

async def _scan_shodan_async(query: str = None, country_code: str = None):
    default_queries = SHODAN_DEFAULT_QUERIES

    queries = [query] if query else default_queries

    selected_country = country_code
    if country_code == "RANDOM":
        selected_country = random.choice(settings.TARGET_COUNTRIES)

    retry_queries = _load_shodan_retry_queries()
    work = list(dict.fromkeys(retry_queries + [(q, selected_country) for q in queries]))
    logger.info(
        f"🌎 [Shodan] Starting Scan | Queries: {len(work)} | Country: {selected_country or 'Global'}"
    )

    # Check Pause State
    if redis_client.get("system:paused"):
        logger.warning("⏸️ [Shodan] System is PAUSED. Skipping scan.")
        return "System Paused"

    if not _shodan_key_is_ready():
        if query:
            _remember_shodan_retry_query(query, selected_country)
        return "Shodan API key missing"

    await _send_shodan_log(
        f"🌎 [Shodan] Starting scan with {len(work)} queries (Country: {selected_country})..."
    )

    total_saved = 0
    errors = []
    result_msg = ""

    for q, query_country in work:
        stop_run = False
        query_label = f'{q} country:"{query_country}"' if query_country else q
        try:
            logger.info(f"    🔎 [Shodan] Executing query: {query_label}")
            results = await _run_shodan_search(q, country_code=query_country)
            logger.info(f"    ✅ [Shodan] Query returned {len(results)} raw results.")
            if not results:
                logger.info(
                    "[Shodan] Search %r returned no matches; it will run again on the next scheduled scan",
                    query_label,
                )

            saved = await _save_credentials_async(results, "shodan")
            total_saved += saved
            _clear_shodan_retry_query(q, query_country)
        except Exception as e:
            _remember_shodan_retry_query(q, query_country)
            stop_run = await _log_shodan_search_failure(query_label, e)
            errors.append(_shodan_error_text(e))
            errors = errors[-MAX_ERRORS_BUFFER:]
        finally:
            if not stop_run:
                await asyncio.sleep(1)  # Keep at least one second between searches.
        if stop_run:
            break
    if errors:
        result_msg += f" (Errors: {len(errors)})"
        await _send_shodan_log(f"❌ [Shodan] Completed with errors: {errors[0]}...")

    logger.info(f"🏁 [Shodan] Finished | Total Saved: {total_saved} | Errors: {len(errors)}")
    await _send_shodan_log(f"🏁 [Shodan] Finished. Saved {total_saved} new credentials.")
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

    if not _shodan_key_is_ready():
        return "Shodan API key missing"

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
    await _send_shodan_log(f"🎯 [Shodan-C2] Starting Telegram C2 infrastructure scan ({len(C2_QUERIES)} queries)...")

    total_saved = 0
    errors = []

    for q in C2_QUERIES:
        if not q:
            continue
        stop_run = False
        try:
            logger.info(f"    🔎 [Shodan-C2] Query: {q[:80]}...")
            results = await _run_shodan_search(q)
            logger.info(f"    ✅ [Shodan-C2] Returned {len(results)} results")
            if not results:
                logger.info(
                    "[Shodan-C2] Search %r returned no matches; it will run again on the next scheduled scan",
                    q,
                )
            saved = await _save_credentials_async(results, "shodan_c2")
            total_saved += saved
        except Exception as e:
            stop_run = await _log_shodan_search_failure(q, e, label="Shodan-C2")
            errors.append(_shodan_error_text(e))
            errors = errors[-MAX_ERRORS_BUFFER:]
        finally:
            if not stop_run:
                await asyncio.sleep(2)  # Keep at least two seconds between C2 searches.
        if stop_run:
            break

    result_msg = f"Shodan C2 scan finished. Saved {total_saved} new credentials."
    if errors:
        result_msg += f" (Errors: {len(errors)})"
    await _send_shodan_log(f"🏁 [Shodan-C2] Finished. Saved {total_saved} credentials from C2 infrastructure.")
    return result_msg
