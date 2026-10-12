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

    # Per-session cooldown gate — don't even start if all sessions are fried
    from app.services.scanners import _is_valid_token
    from app.services.user_agent_srv import user_agent

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
        for q in queries:
            try:
                messages = await user_agent.search_messages(q, limit=100)
            except Exception as e:
                logger.error(f"    ❌ [TelegramSearch] Query '{q}' failed: {e}")
                continue

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
            # 5s inter-query courtesy delay (search_messages already adds 3s INTER_SLEEP)
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
