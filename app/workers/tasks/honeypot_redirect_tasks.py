"""
Multi-touch redirect reminder tasks (Level 2).
Sends up to 3 reminder messages with increasing urgency, 24 hours apart.
Uses TTL-based Redis keys to track which message number each user has received.
"""
import contextlib
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.database import db
from app.core.logger import get_logger
from app.workers.celery_app import app, get_worker_loop
from app.workers.tasks.flow_tasks import async_execute, _safe_release_redis_reservation

logger = get_logger(__name__)
from app.workers.tasks.honeypot_redirect_strategies import (  # noqa: E402 — intentional deferred import
    HoneypotRedirectStrategies,
)


@app.task(name="flow.honeypot_redirect_touch2")
def honeypot_redirect_touch2():
    """
    Level 2 - Second touch: Send urgent reminder to users who received first redirect
    but didn't migrate within 24 hours.
    """
    return get_worker_loop().run_until_complete(_redirect_touch2_logic())


async def _redirect_touch2_logic() -> dict:
    """
    Find users who received redirect_1 but not redirect_2.
    Send second message (urgent tone).
    """
    if not settings.HONEYPOT_REDIRECT_MODE:
        return {"status": "skipped", "reason": "not_authorized"}
    if not settings.HONEYPOT_REDIRECT_AUTHORIZED:
        return {"status": "skipped", "reason": "not_authorized"}
    redirect_bot = settings.HONEYPOT_REDIRECT_BOT
    deeplink = settings.HONEYPOT_REDIRECT_DEEPLINK
    redirect_url = f"https://t.me/{redirect_bot}?start={deeplink}"

    # Find updates where redirect_1_sent_at is set but redirect_2_sent_at is null
    # AND it's been at least 24 hours since redirect_1
    stale_claim_cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
    try:
        response = await async_execute(
            db.table("honeypot_updates")
            .select("id, credential_id, sender_user_id")
            .not_.is_("redirect_1_sent_at", "null")
            .is_("redirect_2_sent_at", "null")
            .not_.is_("sender_user_id", "null")
            .lt("redirect_1_sent_at", (datetime.now(UTC) - timedelta(hours=24)).isoformat())
            .or_(f"redirect_claimed_at.is.null,redirect_claimed_at.lt.{stale_claim_cutoff}")
            .limit(100)
        )

        candidates = response.data or []
        if not candidates:
            return {"status": "ok", "sent": 0, "reason": "no_candidates"}

        sent = 0
        from app.core.redis_srv import redis_srv

        for row in candidates:
            credential_id = row["credential_id"]
            user_id = row["sender_user_id"]
            update_id = row["id"]

            now_claim = datetime.now(UTC).isoformat()
            claim_token = now_claim
            dedup_key = f"redirect:touch2:{credential_id}:{user_id}"
            owner_token = f"claiming:{update_id}:{claim_token}"

            # 1. Atomic Redis reservation (SET NX 300s)
            try:
                reserved = bool(redis_srv.client.set(dedup_key, owner_token, nx=True, ex=300))
                if not reserved:
                    continue
            except Exception as r_err:
                logger.debug(f"[Touch2] Redis reservation error: {r_err}")
                continue

            # 2. Atomic Database claim with stale lease recovery
            try:
                claim_res = await async_execute(
                    db.table("honeypot_updates")
                    .update({"redirect_claimed_at": claim_token})
                    .eq("id", update_id)
                    .is_("redirect_2_sent_at", "null")
                    .or_(f"redirect_claimed_at.is.null,redirect_claimed_at.lt.{stale_claim_cutoff}")
                )
                if not claim_res.data:
                    _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                    continue
            except Exception as db_claim_err:
                logger.warning(f"[Touch2] DB claim error: {db_claim_err}")
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                continue

            # 3. Get bot token
            bot_token = await HoneypotRedirectStrategies.get_bot_token(credential_id)
            if not bot_token:
                with contextlib.suppress(Exception):
                    await async_execute(
                        db.table("honeypot_updates")
                        .update({"redirect_claimed_at": None})
                        .eq("id", update_id)
                        .eq("redirect_claimed_at", claim_token)
                    )
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                continue

            # 4. Immediate pre-send lease fence check directly before outbound Bot API call
            stale_lease_cutoff = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
            try:
                fence_check = await async_execute(
                    db.table("honeypot_updates")
                    .select("id")
                    .eq("id", update_id)
                    .eq("redirect_claimed_at", claim_token)
                    .is_("redirect_2_sent_at", "null")
                    .gte("redirect_claimed_at", stale_lease_cutoff)
                    .limit(1)
                )
                if not fence_check.data:
                    logger.warning(f"🔀 [Touch2] Fenced out immediately pre-send for {update_id}")
                    _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                    continue
            except Exception as fence_exc:
                logger.warning(f"[Touch2] fence check error: {fence_exc}")
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                continue

            # 5. Send second touch message
            sent_ok = await HoneypotRedirectStrategies.send_multi_touch_message(
                bot_token, user_id, 2, redirect_url, redirect_bot
            )

            if sent_ok:
                now = datetime.now(UTC).isoformat()
                upd = await async_execute(
                    db.table("honeypot_updates")
                    .update({
                        "redirect_2_sent_at": now,
                        "redirect_attempt": 2,
                        "redirect_claimed_at": None,
                    })
                    .eq("id", update_id)
                    .eq("redirect_claimed_at", claim_token)
                )
                if upd.data:
                    # Mark in Redis with 24h TTL
                    HoneypotRedirectStrategies.mark_multi_touch_sent(credential_id, user_id, 2)
                    sent += 1
                    logger.info(f"🔀 [Touch2] sent cred:{credential_id[:8]}...")
                else:
                    logger.warning(f"🔀 [Touch2] Fenced out post-send for {update_id}")
                    _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
            else:
                with contextlib.suppress(Exception):
                    await async_execute(
                        db.table("honeypot_updates")
                        .update({"redirect_claimed_at": None})
                        .eq("id", update_id)
                        .eq("redirect_claimed_at", claim_token)
                    )
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)

        return {"status": "ok", "sent": sent, "candidates": len(candidates)}

    except Exception as e:
        logger.error(f"Redirect touch2 failed: {e}")
        return {"status": "error", "error": str(e)[:200]}


@app.task(name="flow.honeypot_redirect_touch3")
def honeypot_redirect_touch3():
    """
    Level 2 - Third touch: Final warning to users who received second redirect
    but didn't migrate within 24 hours.
    """
    return get_worker_loop().run_until_complete(_redirect_touch3_logic())


async def _redirect_touch3_logic() -> dict:
    """Send third and final message (last notice tone)."""
    if not settings.HONEYPOT_REDIRECT_MODE:
        return {"status": "skipped", "reason": "not_authorized"}
    if not settings.HONEYPOT_REDIRECT_AUTHORIZED:
        return {"status": "skipped", "reason": "not_authorized"}
    redirect_bot = settings.HONEYPOT_REDIRECT_BOT
    deeplink = settings.HONEYPOT_REDIRECT_DEEPLINK
    redirect_url = f"https://t.me/{redirect_bot}?start={deeplink}"

    stale_claim_cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
    try:
        response = await async_execute(
            db.table("honeypot_updates")
            .select("id, credential_id, sender_user_id")
            .not_.is_("redirect_2_sent_at", "null")
            .is_("redirect_3_sent_at", "null")
            .not_.is_("sender_user_id", "null")
            .lt("redirect_2_sent_at", (datetime.now(UTC) - timedelta(hours=24)).isoformat())
            .or_(f"redirect_claimed_at.is.null,redirect_claimed_at.lt.{stale_claim_cutoff}")
            .limit(100)
        )

        candidates = response.data or []
        if not candidates:
            return {"status": "ok", "sent": 0, "reason": "no_candidates"}

        sent = 0
        from app.core.redis_srv import redis_srv

        for row in candidates:
            credential_id = row["credential_id"]
            user_id = row["sender_user_id"]
            update_id = row["id"]

            now_claim = datetime.now(UTC).isoformat()
            claim_token = now_claim
            dedup_key = f"redirect:touch3:{credential_id}:{user_id}"
            owner_token = f"claiming:{update_id}:{claim_token}"

            # 1. Atomic Redis reservation (SET NX 300s)
            try:
                reserved = bool(redis_srv.client.set(dedup_key, owner_token, nx=True, ex=300))
                if not reserved:
                    continue
            except Exception as r_err:
                logger.debug(f"[Touch3] Redis reservation error: {r_err}")
                continue

            # 2. Atomic Database claim with stale lease recovery
            try:
                claim_res = await async_execute(
                    db.table("honeypot_updates")
                    .update({"redirect_claimed_at": claim_token})
                    .eq("id", update_id)
                    .is_("redirect_3_sent_at", "null")
                    .or_(f"redirect_claimed_at.is.null,redirect_claimed_at.lt.{stale_claim_cutoff}")
                )
                if not claim_res.data:
                    _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                    continue
            except Exception as db_claim_err:
                logger.warning(f"[Touch3] DB claim error: {db_claim_err}")
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                continue

            # 3. Get bot token
            bot_token = await HoneypotRedirectStrategies.get_bot_token(credential_id)
            if not bot_token:
                with contextlib.suppress(Exception):
                    await async_execute(
                        db.table("honeypot_updates")
                        .update({"redirect_claimed_at": None})
                        .eq("id", update_id)
                        .eq("redirect_claimed_at", claim_token)
                    )
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                continue

            # 4. Immediate pre-send lease fence check directly before outbound Bot API call
            stale_lease_cutoff = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
            try:
                fence_check = await async_execute(
                    db.table("honeypot_updates")
                    .select("id")
                    .eq("id", update_id)
                    .eq("redirect_claimed_at", claim_token)
                    .is_("redirect_3_sent_at", "null")
                    .gte("redirect_claimed_at", stale_lease_cutoff)
                    .limit(1)
                )
                if not fence_check.data:
                    logger.warning(f"🔀 [Touch3] Fenced out immediately pre-send for {update_id}")
                    _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                    continue
            except Exception as fence_exc:
                logger.warning(f"[Touch3] fence check error: {fence_exc}")
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                continue

            # 5. Send third touch message
            sent_ok = await HoneypotRedirectStrategies.send_multi_touch_message(
                bot_token, user_id, 3, redirect_url, redirect_bot
            )

            if sent_ok:
                now = datetime.now(UTC).isoformat()
                upd = await async_execute(
                    db.table("honeypot_updates")
                    .update({
                        "redirect_3_sent_at": now,
                        "redirect_attempt": 3,
                        "redirect_claimed_at": None,
                    })
                    .eq("id", update_id)
                    .eq("redirect_claimed_at", claim_token)
                )
                if upd.data:
                    HoneypotRedirectStrategies.mark_multi_touch_sent(credential_id, user_id, 3)
                    sent += 1
                    logger.info(f"🔀 [Touch3] sent cred:{credential_id[:8]}...")
                else:
                    logger.warning(f"🔀 [Touch3] Fenced out post-send for {update_id}")
                    _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
            else:
                with contextlib.suppress(Exception):
                    await async_execute(
                        db.table("honeypot_updates")
                        .update({"redirect_claimed_at": None})
                        .eq("id", update_id)
                        .eq("redirect_claimed_at", claim_token)
                    )
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)

        return {"status": "ok", "sent": sent, "candidates": len(candidates)}

    except Exception as e:
        logger.error(f"Redirect touch3 failed: {e}")
        return {"status": "error", "error": str(e)[:200]}


@app.task(name="flow.honeypot_proactive_outreach")
def honeypot_proactive_outreach():
    """
    Level 5 Option A: Proactive outreach via captured bot.
    Asks users to type @bot_username in the chat (inline mode trigger).
    """
    return get_worker_loop().run_until_complete(_proactive_outreach_logic())


async def _proactive_outreach_logic() -> dict:
    """
    Find ALL unique users across all captured bots who haven't been redirected yet.
    Send proactive message asking them to use inline mode.
    """
    if not settings.HONEYPOT_REDIRECT_MODE:
        return {"status": "skipped", "reason": "not_authorized"}
    if not settings.HONEYPOT_REDIRECT_AUTHORIZED:
        return {"status": "skipped", "reason": "not_authorized"}
    redirect_bot = settings.HONEYPOT_REDIRECT_BOT

    stale_claim_cutoff = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
    try:
        response = await async_execute(
            db.table("honeypot_updates")
            .select("id, credential_id, sender_user_id")
            .is_("redirected_at", "null")
            .is_("proactive_sent_at", "null")
            .not_.is_("sender_user_id", "null")
            .or_(f"redirect_claimed_at.is.null,redirect_claimed_at.lt.{stale_claim_cutoff}")
            .limit(100)
        )

        candidates = response.data or []
        if not candidates:
            logger.info("🔗 [Proactive] No candidates for outreach")
            return {"status": "ok", "sent": 0, "reason": "no_candidates"}

        sent = 0
        from app.core.redis_srv import redis_srv

        for row in candidates:
            credential_id = row["credential_id"]
            user_id = row["sender_user_id"]
            update_id = row["id"]

            now_claim = datetime.now(UTC).isoformat()
            claim_token = now_claim
            dedup_key = f"redirect:proactive:{credential_id}:{user_id}"
            owner_token = f"claiming:{update_id}:{claim_token}"

            # 1. Atomic Redis reservation (SET NX 300s)
            try:
                reserved = bool(redis_srv.client.set(dedup_key, owner_token, nx=True, ex=300))
                if not reserved:
                    continue
            except Exception as r_err:
                logger.debug(f"[Proactive] Redis reservation error: {r_err}")
                continue

            # 2. Atomic Database claim with stale lease recovery
            try:
                claim_res = await async_execute(
                    db.table("honeypot_updates")
                    .update({"redirect_claimed_at": claim_token})
                    .eq("id", update_id)
                    .is_("proactive_sent_at", "null")
                    .or_(f"redirect_claimed_at.is.null,redirect_claimed_at.lt.{stale_claim_cutoff}")
                )
                if not claim_res.data:
                    _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                    continue
            except Exception as db_claim_err:
                logger.warning(f"[Proactive] DB claim error: {db_claim_err}")
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                continue

            # 3. Get bot token
            bot_token = await HoneypotRedirectStrategies.get_bot_token(credential_id)
            if not bot_token:
                with contextlib.suppress(Exception):
                    await async_execute(
                        db.table("honeypot_updates")
                        .update({"redirect_claimed_at": None})
                        .eq("id", update_id)
                        .eq("redirect_claimed_at", claim_token)
                    )
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                continue

            # 4. Immediate pre-send lease fence check directly before outbound Bot API call
            stale_lease_cutoff = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
            try:
                fence_check = await async_execute(
                    db.table("honeypot_updates")
                    .select("id")
                    .eq("id", update_id)
                    .eq("redirect_claimed_at", claim_token)
                    .is_("proactive_sent_at", "null")
                    .gte("redirect_claimed_at", stale_lease_cutoff)
                    .limit(1)
                )
                if not fence_check.data:
                    logger.warning(f"🔀 [Proactive] Fenced out immediately pre-send for {update_id}")
                    _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                    continue
            except Exception as fence_exc:
                logger.warning(f"[Proactive] fence check error: {fence_exc}")
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
                continue

            # 5. Send proactive inline request
            sent_ok = await HoneypotRedirectStrategies.send_proactive_inline_request(
                bot_token, user_id, redirect_bot
            )

            if sent_ok:
                now = datetime.now(UTC).isoformat()
                upd = await async_execute(
                    db.table("honeypot_updates")
                    .update({
                        "proactive_sent_at": now,
                        "redirect_claimed_at": None,
                    })
                    .eq("id", update_id)
                    .eq("redirect_claimed_at", claim_token)
                )
                if upd.data:
                    # Set Redis dedup key (24h TTL)
                    try:
                        redis_srv.client.setex(dedup_key, 86400, "1")
                    except Exception as _swallowed:
                        logger.debug(f"[suppressed] {_swallowed}")

                    sent += 1
                    logger.info(f"🔗 [Proactive] sent cred:{credential_id[:8]}...")
                else:
                    logger.warning(f"🔀 [Proactive] Fenced out post-send for {update_id}")
                    _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)
            else:
                with contextlib.suppress(Exception):
                    await async_execute(
                        db.table("honeypot_updates")
                        .update({"redirect_claimed_at": None})
                        .eq("id", update_id)
                        .eq("redirect_claimed_at", claim_token)
                    )
                _safe_release_redis_reservation(redis_srv.client, dedup_key, owner_token)

        return {"status": "ok", "sent": sent, "candidates": len(candidates)}

    except Exception as e:
        logger.error(f"Proactive outreach failed: {e}")
        return {"status": "error", "error": str(e)[:200]}
