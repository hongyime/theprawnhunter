"""Scanner task implementations for exa."""

from ..common import *

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

    # Guard: skip if Exa key is broken (401/403)
    EXA_COOLDOWN_KEY = "cooldown:scanner:exa_api_broken"  # noqa: N806 — function-scoped constant
    EXA_COOLDOWN_TTL = int(os.getenv("EXA_BROKEN_COOLDOWN_SECS", 82800))  # noqa: N806 — function-scoped constant  # 23h default
    if redis_client.get(EXA_COOLDOWN_KEY):
        ttl = redis_client.ttl(EXA_COOLDOWN_KEY)
        logger.info(f"⏭️ [Exa] API key on cooldown ({ttl}s remaining) — skipping scan.")
        return "Exa API key on cooldown — skipped."

    queries = [query] if query else [
        '"api.telegram.org/bot"',
        '"TELEGRAM_BOT_TOKEN"',
        '"bot_token" "telegram"',
    ]

    logger.info(f"🔍 [Exa] Starting paste-site scan ({len(queries)} queries)...")
    await _send_log_async(f"🔍 [Exa] Scanning paste sites via Exa ({len(queries)} queries)...")

    total_saved = 0
    errors = []

    for q in queries:
        try:
            results = await exa_srv.search(q)
            logger.info(f"    ✅ [Exa] Query '{q[:50]}' returned {len(results)} matches.")
            saved = await _save_credentials_async(results, "exa")
            total_saved += saved
        except Exception as e:
            err_str = str(e)
            logger.error(f"    ❌ [Exa] Query failed: {err_str}")
            errors.append(err_str)
            if "401" in err_str or "403" in err_str or "auth" in err_str.lower():
                redis_client.set(EXA_COOLDOWN_KEY, "1", ex=EXA_COOLDOWN_TTL)
                msg = f"⚠️ [Exa] Auth error — API key invalid or quota exhausted. Cooldown set for {EXA_COOLDOWN_TTL//3600}h. Check dashboard.exa.ai."
                logger.warning(msg)
                await _send_log_async(msg)
                break
        await asyncio.sleep(2)

    msg = f"Exa scan finished. Saved {total_saved} new credentials."
    await _send_log_async(f"🏁 [Exa] Finished. Saved {total_saved} new credentials.")
    return msg
