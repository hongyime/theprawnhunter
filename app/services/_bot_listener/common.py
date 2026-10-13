import asyncio

import logging

import os

import shutil

import signal

import sys

import uuid

from typing import Any

import redis.asyncio as redis

from telegram import Update

from telegram.constants import ParseMode

from telegram.error import Conflict

from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from telegram.helpers import escape_markdown

from telegram.request import HTTPXRequest

from app.infrastructure.database import execute_async as _execute_async_shared

AUTO_DELETE_SECONDS = 30

async def _auto_delete_msg(msg, delay=AUTO_DELETE_SECONDS):
    """Delete a sent Telegram message after delay seconds."""
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass

async def _reply_and_delete(update, text, delay=AUTO_DELETE_SECONDS, **kwargs):
    """Reply to a message and schedule auto-deletion."""
    sent = await update.message.reply_text(text, **kwargs)
    asyncio.create_task(_auto_delete_msg(sent, delay))
    return sent

async def _send_and_delete(bot, chat_id, text, delay=AUTO_DELETE_SECONDS, **kwargs):
    """Send a message via bot and schedule auto-deletion."""
    sent = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    asyncio.create_task(_auto_delete_msg(sent, delay))
    return sent

from telethon import TelegramClient

from telethon.errors import SessionPasswordNeededError

BOT_LISTENER_SOURCE_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "../bot_listener.py"))

import contextlib  # noqa: E402 — intentional deferred import

from app.core.config import settings  # noqa: E402 — intentional deferred import

from app.core.constants import (  # noqa: E402 — intentional deferred import
    LOCK_TTL_SECONDS,
    SESSION_FILE_PERMISSIONS,
    TELEGRAM_SERVICE_NOTIFICATIONS_ID,
)

from app.core.database import db  # noqa: E402 — intentional deferred import

INSTANCE_ID = str(uuid.uuid4())

WAIT_PHONE, WAIT_CODE, WAIT_PASSWORD = range(3)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logging.getLogger("httpx").setLevel(logging.WARNING)

logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("bot_listener")

logger.info(f"🆔 Process Instance ID: {INSTANCE_ID}")

redis_client: redis.Redis = None

PAUSE_KEY = "system:paused"

ANONYMOUS_ADMIN_ID = settings.ANONYMOUS_ADMIN_ID

stop_event = asyncio.Event()

_bot_usernames: dict[str, str] = {}

_locked_bots: set[str] = set()

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
    non_numeric = set(whitelist) - numeric_whitelist
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

INDICATOR_TYPES = ("network_domain", "canonical_url", "wallet_address")

__all__ = ['ANONYMOUS_ADMIN_ID', 'AUTO_DELETE_SECONDS', 'Any', 'Application', 'ApplicationBuilder', 'BOT_LISTENER_SOURCE_FILE', 'ChatMemberHandler', 'CommandHandler', 'Conflict', 'ContextTypes', 'ConversationHandler', 'HTTPXRequest', 'INDICATOR_TYPES', 'INSTANCE_ID', 'LOCK_TTL_SECONDS', 'MessageHandler', 'PAUSE_KEY', 'ParseMode', 'SESSION_FILE_PERMISSIONS', 'SessionPasswordNeededError', 'TELEGRAM_SERVICE_NOTIFICATIONS_ID', 'TelegramClient', 'Update', 'WAIT_CODE', 'WAIT_PASSWORD', 'WAIT_PHONE', '_auto_delete_msg', '_bot_usernames', '_execute_async_shared', '_get_all_bot_usernames_except', '_get_other_bot_usernames', '_get_whitelisted_usernames', '_locked_bots', '_reply_and_delete', '_send_and_delete', '_subject_label', 'asyncio', 'contextlib', 'db', 'escape_markdown', 'filters', 'is_admin', 'logging', 'logger', 'os', 'redis', 'redis_client', 'settings', 'shutil', 'signal', 'stop_event', 'sys', 'uuid']
