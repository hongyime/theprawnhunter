"""Scanner task implementations for urlscan."""

import json

from ..common import *


URLSCAN_QUERY_RETRY_KEY = "scanner:urlscan:retry_queries"
URLSCAN_SCAN_RETRY_KEY = "scanner:urlscan:retry_scans"


def _urlscan_query_value(query: str, country_code: str) -> str:
    return json.dumps(
        {"query": query, "country_code": country_code},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_redis_value(value) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _load_urlscan_queries() -> list[dict]:
    try:
        values = redis_client.smembers(URLSCAN_QUERY_RETRY_KEY)
    except Exception as error:
        raise RuntimeError(f"could not read saved URLScan searches: {error}") from error

    saved = []
    for value in values:
        raw = _decode_redis_value(value)
        try:
            record = json.loads(raw)
            if not isinstance(record, dict) or not record.get("query"):
                raise ValueError("saved search has no query")
        except Exception as error:
            logger.error(
                "[URLScan] Saved search entry is not readable: %s (%s). "
                "It will stay in retry storage.",
                raw,
                error,
            )
            continue
        record["_retry_value"] = raw
        saved.append(record)
    return saved


def _load_urlscan_scans() -> list[dict]:
    try:
        values = redis_client.smembers(URLSCAN_SCAN_RETRY_KEY)
    except Exception as error:
        raise RuntimeError(f"could not read saved URLScan scan lookups: {error}") from error

    saved = []
    for value in values:
        raw = _decode_redis_value(value)
        try:
            record = json.loads(raw)
            if not (
                isinstance(record, dict)
                and record.get("query")
                and isinstance(record.get("item"), dict)
            ):
                raise ValueError("saved scan lookup has no query or scan item")
        except Exception as error:
            logger.error(
                "[URLScan] Saved scan lookup is not readable: %s (%s). "
                "It will stay in retry storage.",
                raw,
                error,
            )
            continue
        record["_retry_value"] = raw
        saved.append(record)
    return saved


def _save_urlscan_query(query: str, country_code: str) -> bool:
    value = _urlscan_query_value(query, country_code)
    try:
        redis_client.sadd(URLSCAN_QUERY_RETRY_KEY, value)
        return True
    except Exception as error:
        logger.error(
            "[URLScan] Could not save search %r for the next scan: %s. "
            "Built-in searches will repeat; a one-off search could not be saved.",
            query,
            error,
        )
        return False


def _remove_urlscan_query(query: str, country_code: str, retry_value=None):
    value = retry_value or _urlscan_query_value(query, country_code)
    try:
        redis_client.srem(URLSCAN_QUERY_RETRY_KEY, value)
    except Exception as error:
        logger.error(
            "[URLScan] Could not clear completed search %r from retry storage: %s. "
            "It will be checked again on the next scan.",
            query,
            error,
        )


def _apply_urlscan_scan_updates(outcome) -> None:
    for record in outcome.pending_scans:
        value = json.dumps(
            {
                "query": record["query"],
                "country_code": record.get("country_code"),
                "item": record["item"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            redis_client.sadd(URLSCAN_SCAN_RETRY_KEY, value)
        except Exception as error:
            raise RuntimeError(
                f"could not save scan {record.get('item', {}).get('_id')} "
                f"for search {record.get('query')!r}: {error}"
            ) from error

    for record in outcome.completed_scans:
        value = json.dumps(
            {
                "query": record["query"],
                "country_code": record.get("country_code"),
                "item": record["item"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            redis_client.srem(URLSCAN_SCAN_RETRY_KEY, value)
        except Exception as error:
            logger.error(
                "[URLScan] Could not clear completed scan %s for search %r: %s. "
                "It will be checked again on the next scan.",
                record.get("item", {}).get("_id"),
                record.get("query"),
                error,
            )


async def _send_urlscan_log(message: str):
    try:
        await _send_log_async(message)
    except Exception as error:
        logger.warning(
            "[URLScan] Could not send the scan status message: %s. "
            "The scan will continue.",
            error,
        )


@app.task(name="scanner.scan_urlscan")
def scan_urlscan(query: str = None, country_code: str = None):
    return _run_sync(_scan_urlscan_async(query, country_code))


async def _scan_urlscan_async(query: str = None, country_code: str = None):
    common_queries = [
        "api.telegram.org/bot",
        "bot_token",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_TOKEN",
        "Telegram Bot",
        "https://api.telegram.org",
    ]
    selected_country = country_code
    if country_code == "RANDOM":
        selected_country = random.choice(settings.TARGET_COUNTRIES)

    logger.info(
        "[URLScan] Starting scan. Queries: %s | Country: %s",
        1 if query else len(common_queries),
        selected_country or "Global",
    )
    try:
        if redis_client.get("system:paused"):
            logger.warning("[URLScan] System is paused; skipping this scan.")
            return "System Paused"
    except Exception as error:
        logger.error(
            "[URLScan] Could not read the pause setting: %s. "
            "Stopping this run; the next scheduled scan will try again.",
            error,
        )
        return "Could not read pause setting"

    await _send_urlscan_log(
        f"[URLScan] Starting scan with {1 if query else len(common_queries)} queries "
        f"(Country: {selected_country or 'Global'})"
    )

    if not urlscan.api_key:
        logger.warning(
            "[URLScan] API key is missing or empty; skipping this run. "
            "The built-in searches will run again on the next scheduled scan."
        )
        if query:
            _save_urlscan_query(query, selected_country)
        await _send_urlscan_log(
            "[URLScan] API key is missing or empty; searches were not sent. "
            "They will be tried again on a later scan."
        )
        return "URLScan API key missing; scan skipped"

    try:
        saved_queries = _load_urlscan_queries()
        saved_scans = _load_urlscan_scans()
    except Exception as error:
        logger.error(
            "[URLScan] Could not read retry storage: %s. "
            "Stopping this run; the next scheduled scan will try again.",
            error,
        )
        return "URLScan retry storage unavailable"

    queries = []
    seen_queries = set()
    for record in saved_queries:
        pair = (record["query"], record.get("country_code"))
        if pair not in seen_queries:
            queries.append(
                {
                    "query": pair[0],
                    "country_code": pair[1],
                    "retry_value": record["_retry_value"],
                }
            )
            seen_queries.add(pair)

    for search_query in ([query] if query else common_queries):
        pair = (search_query, selected_country)
        if pair not in seen_queries:
            queries.append(
                {
                    "query": search_query,
                    "country_code": selected_country,
                    "retry_value": None,
                }
            )
            seen_queries.add(pair)

    total_saved = 0
    error_count = 0
    attempted_scan_ids = {
        record.get("item", {}).get("_id")
        for record in saved_scans
        if record.get("item", {}).get("_id")
    }
    rate_limited = False

    if saved_scans:
        try:
            pending_outcome = await urlscan.process_pending_scans(saved_scans)
            try:
                saved_count = await _save_credentials_async(pending_outcome, "urlscan")
                total_saved += saved_count
                _apply_urlscan_scan_updates(pending_outcome)
                attempted_scan_ids.update(pending_outcome.attempted_scan_ids)
            except Exception as error:
                logger.error(
                    "[URLScan] Could not save results from saved scan lookups: %s. "
                    "Those lookups remain saved for the next scan.",
                    error,
                )
                error_count += 1
            rate_limited = pending_outcome.rate_limited
        except Exception as error:
            logger.error(
                "[URLScan] Saved scan lookup batch failed: %s. "
                "The saved scans remain for the next scan; other searches will continue.",
                error,
            )
            error_count += 1

    if rate_limited:
        logger.warning(
            "[URLScan] A saved scan lookup hit a rate limit. "
            "This run will stop; remaining saved work will be checked next scan."
        )
        for record in queries:
            _save_urlscan_query(record["query"], record["country_code"])
        return f"URLScan scan paused by rate limit. Saved {total_saved} new credentials."

    for index, record in enumerate(queries):
        search_query = record["query"]
        search_country = record["country_code"]
        retry_value = record["retry_value"]
        logger.info("[URLScan] Search query: %r", search_query)

        try:
            results = await _cb("urlscan").call(urlscan.search)(
                search_query,
                country_code=search_country,
                exclude_scan_ids=attempted_scan_ids,
            )
            logger.info(
                "[URLScan] Search %r returned %s credentials.",
                search_query,
                len(results),
            )
            saved_count = await _save_credentials_async(results, "urlscan")
            total_saved += saved_count
            try:
                _apply_urlscan_scan_updates(results)
            except Exception as error:
                _save_urlscan_query(search_query, search_country)
                raise RuntimeError(
                    f"could not save scan lookups for search {search_query!r}: {error}"
                ) from error

            attempted_scan_ids.update(results.attempted_scan_ids)
            if results.retry_query:
                _save_urlscan_query(search_query, search_country)
            else:
                _remove_urlscan_query(search_query, search_country, retry_value)

            if results.rate_limited:
                rate_limited = True
                logger.warning(
                    "[URLScan] Search %r reached a URLScan request limit. "
                    "Stopping this run and saving the remaining searches for next time.",
                    search_query,
                )
                for remaining in queries[index + 1 :]:
                    _save_urlscan_query(remaining["query"], remaining["country_code"])
                break
        except Exception as error:
            error_count += 1
            _save_urlscan_query(search_query, search_country)
            if getattr(error, "rate_limited", False):
                rate_limited = True
                logger.warning(
                    "[URLScan] Search %r hit a request limit: %s. "
                    "Stopping this run; this search and the remaining searches "
                    "are saved for next time.",
                    search_query,
                    error,
                )
                for remaining in queries[index + 1 :]:
                    _save_urlscan_query(remaining["query"], remaining["country_code"])
                break
            logger.error(
                "[URLScan] Search %r failed: %s. Saved it for the next scan; "
                "continuing with the other searches.",
                search_query,
                error,
            )
        finally:
            await asyncio.sleep(2)

    result_msg = f"URLScan scan finished. Saved {total_saved} new credentials."
    if error_count:
        result_msg += f" ({error_count} searches or scan lookups need another try)"
    if rate_limited:
        result_msg = f"URLScan scan paused by rate limit. Saved {total_saved} new credentials."

    logger.info(
        "[URLScan] Finished. Saved: %s | Errors: %s | Rate limited: %s",
        total_saved,
        error_count,
        rate_limited,
    )
    await _send_urlscan_log(
        f"[URLScan] Finished. Saved {total_saved} new credentials. "
        f"Errors: {error_count}; rate limited: {rate_limited}."
    )
    return result_msg
