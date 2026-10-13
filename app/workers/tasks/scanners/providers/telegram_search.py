"""Scanner task implementations for telegram search."""

from ..common import *

@app.task(name="scanner.scan_telegram_search", autoretry_for=(Exception,), retry_backoff=True, max_retries=1)
def scan_telegram_search(query: str = None):
    """
    Telegram MTProto self-search via UserAgent session.

    Different result space from any web scanner — catches token leaks discussed
    in public channels but never indexed by Google/Exa/etc. Bound by
    UserAgent FloodWait cooldown discipline.
    """
    return _run_sync(_scan_telegram_search_async(query))

async def _scan_telegram_search_async(query: str = None):
    if redis_client.get("system:paused"):
        return "System Paused"

    # Search errors are raised only for this scanner. Other Telegram session
    # users keep the service's existing empty-list behavior.
    from telethon.errors import FloodWaitError
    from app.services._user_agent.session_lifecycle import UserAgentSessionUnavailableError
    from app.services.scanners import _is_valid_token
    from app.services.user_agent_srv import user_agent

    async def report_failure(message: str, *, warning: bool = False) -> None:
        if warning:
            logger.warning(message)
        else:
            logger.error(message)
        try:
            await _send_log_async(message)
        except Exception as log_error:
            logger.error(
                f"[TelegramSearch] Could not send the search failure log: {log_error}. "
                f"The worker log has the failure details."
            )

    logger.info("🔍 [TelegramSearch] Starting MTProto global search...")
    await _send_log_async("🔍 [TelegramSearch] Querying Telegram public channels...")

    queries = [query] if query else [
        "api.telegram.org/bot",
        "TELEGRAM_BOT_TOKEN",
        "bot_token leaked",
    ]

    # Strict token regex — same shape as TOKEN_PATTERN but inline-safe
    token_re = re.compile(r"\b\d{8,15}:[A-Za-z0-9_-]{35}\b")

    all_results = []
    try:
        for query_index, q in enumerate(queries):
            try:
                messages = await user_agent.search_messages(
                    q, limit=100, raise_errors=True
                )
            except FloodWaitError as e:
                wait_seconds = max(0, int(e.seconds))
                await report_failure(
                    f"🛑 [TelegramSearch] Search {q!r} hit Telegram's FloodWait limit. "
                    f"Waiting the full {wait_seconds}s, then stopping this run; "
                    "this search will be tried again on the next scan.",
                    warning=True,
                )
                if wait_seconds:
                    await asyncio.sleep(wait_seconds)
                return (
                    f"TelegramSearch paused after FloodWait for {wait_seconds}s; "
                    "remaining searches will run on the next scan."
                )
            except UserAgentSessionUnavailableError as e:
                await report_failure(
                    f"⚠️ [TelegramSearch] Search {q!r} has no usable Telegram session: {e}. "
                    "Stopping this run; the search list will be tried again on the next scan.",
                    warning=True,
                )
                return "TelegramSearch stopped because no usable Telegram session is available."
            except Exception as e:
                await report_failure(
                    f"❌ [TelegramSearch] Search {q!r} failed: {type(e).__name__}: {e}. "
                    "Moving to the next search; this search will be tried again on the next scan."
                )
                messages = None

            if messages is not None:
                if not messages:
                    logger.info(f"[TelegramSearch] Search {q!r} returned 0 messages.")
                try:
                    for msg in messages:
                        text = msg.get("text", "")
                        if not text:
                            continue
                        tokens = token_re.findall(text)
                        for tok in tokens:
                            if not _is_valid_token(tok):
                                continue
                            all_results.append({
                                "token": tok,
                                "chat_id": msg.get("chat_id"),
                                "meta": {
                                    "telegram_chat_name": msg.get("chat_name"),
                                    "telegram_message_id": msg.get("message_id"),
                                    "telegram_date": msg.get("date"),
                                    "telegram_query": q,
                                }
                            })
                except Exception as e:
                    await report_failure(
                        f"❌ [TelegramSearch] Could not read results for search {q!r}: "
                        f"{type(e).__name__}: {e}. Moving to the next search; "
                        "this search will be tried again on the next scan."
                    )

            if query_index < len(queries) - 1:
                # Keep spacing between searches after errors as well as successes.
                await asyncio.sleep(5)

        if all_results:
            saved = await _save_credentials_async(all_results, "telegram_search")
            msg = f"TelegramSearch finished. Enqueued {saved} tokens for validation."
        else:
            msg = "TelegramSearch finished. 0 matches."

        await _send_log_async(f"🏁 [TelegramSearch] {msg}")
        return msg
    except Exception as e:
        logger.error(f"[TelegramSearch] Error: {e}", exc_info=True)
        raise
