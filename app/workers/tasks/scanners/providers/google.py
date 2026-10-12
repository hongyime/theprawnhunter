"""Scanner task implementations for google."""

from ..common import *

@app.task(name="scanner.scan_google", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_google(query: str = None):
    return _run_sync(_scan_google_async(query))

async def _scan_google_async(query: str = None):
    if redis_client.get("system:paused"):
        return "System Paused"

    # Guard: skip entire run if Google CSE key is known-broken (403 = quota exhausted or key invalid).
    # A 403 on the first dork means all subsequent dorks will also fail — no point running them.
    # The cooldown (default 23h) prevents hammering a dead key every scan cycle.
    GOOGLE_COOLDOWN_KEY = "cooldown:scanner:google_api_broken"  # noqa: N806 — function-scoped constant
    GOOGLE_COOLDOWN_TTL = int(os.getenv("GOOGLE_BROKEN_COOLDOWN_SECS", 82800))  # noqa: N806 — function-scoped constant  # 23h default
    if redis_client.get(GOOGLE_COOLDOWN_KEY):
        ttl = redis_client.ttl(GOOGLE_COOLDOWN_KEY)
        logger.info(f"⏭️ [Google] API key on cooldown ({ttl}s remaining) — skipping scan.")
        return "Google API key on cooldown — skipped."

    dorks = [
        'site:pastebin.com "api.telegram.org/bot"',
        'site:github.com "TELEGRAM_BOT_TOKEN" filetype:env',
        'site:gitlab.com "api.telegram.org/bot"',
        'site:hastebin.com "api.telegram.org/bot"',
        'site:rentry.co "api.telegram.org/bot"',
        '"api.telegram.org/bot" filetype:py',
        '"api.telegram.org/bot" filetype:js',
        '"api.telegram.org/bot" filetype:env',
    ]
    if query:
        dorks = [query]

    logger.info(f"🔍 [Google] Starting scan with {len(dorks)} dorks...")
    await _send_log_async(f"🔍 [Google] Starting scan with {len(dorks)} dorks...")

    total_saved = 0
    errors = []

    for dork in dorks:
        try:
            results = await google_srv.search(dork)
            logger.info(f"    ✅ [Google] Dork returned {len(results)} results.")
            saved = await _save_credentials_async(results, "google_dork")
            total_saved += saved
        except Exception as e:
            err_str = str(e)
            logger.error(f"    ❌ [Google] Dork failed: {err_str}")
            errors.append(err_str)
            errors = errors[-MAX_ERRORS_BUFFER:]
            # 403 = quota exhausted or key invalid — all remaining dorks will also fail.
            # Set cooldown and abort early rather than burning time on dead requests.
            if "403" in err_str or "Forbidden" in err_str:
                redis_client.set(GOOGLE_COOLDOWN_KEY, "1", ex=GOOGLE_COOLDOWN_TTL)
                msg = f"⚠️ [Google] 403 Forbidden — API key quota exhausted or invalid. Cooldown set for {GOOGLE_COOLDOWN_TTL//3600}h. Check billing/quota at console.cloud.google.com."
                logger.warning(msg)
                await _send_log_async(msg)
                break
        await asyncio.sleep(2)

    result_msg = f"Google scan finished. Saved {total_saved} new credentials."
    if errors:
        result_msg += f" (Errors: {len(errors)})"
    await _send_log_async(f"🏁 [Google] Finished. Saved {total_saved} new credentials.")
    return result_msg
