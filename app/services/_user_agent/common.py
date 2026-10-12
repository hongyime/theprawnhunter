import asyncio

import contextlib

import logging

import os

import time

import uuid

from dataclasses import dataclass

from datetime import UTC, datetime, timedelta

from telethon import TelegramClient, errors, types

from telethon.errors import SecurityError

from app.core.config import settings

from app.domain.media import telethon_media_info

from app.infrastructure.database import execute_async as _execute_async_shared

from app.services._scraper.lifecycle import TelegramClientLifecycle

logger = logging.getLogger("user_agent")

_MTPROTO_CONFLICT_BACKOFF = 10

_MTPROTO_MAX_RETRIES = 3

ARCHIVE_TMP_DIR = "/tmp"

ARCHIVE_TMP_PREFIX = "archive_"

async def _async_execute(query_builder):
    return await _execute_async_shared(query_builder)

def _username_from_meta(meta: object) -> str | None:
    if not isinstance(meta, dict):
        return None
    username = meta.get("bot_username")
    if not isinstance(username, str):
        return None
    username = username.strip()
    if not username:
        return None
    return username if username.startswith("@") else f"@{username}"

def _credential_lookup_filter_for_source(source: int | str) -> str | None:
    source_text = str(source).strip()
    if source_text.lstrip("-").isdigit():
        return f"chat_id.eq.{source_text}"
    try:
        uuid.UUID(source_text)
    except (TypeError, ValueError):
        return None
    return f"id.eq.{source_text}"

def _is_terminal_invite_error(exc: Exception) -> bool:
    text = str(exc).lower()
    class_name = exc.__class__.__name__.lower()
    terminal_terms = (
        "too many bots",
        "bots in this chat",
        "userbotinvalid",
        "user_bot_invalid",
        "userprivac",
        "privacy",
        "chatadminrequired",
        "chat_admin_required",
        "forbidden",
    )
    return any(term in text or term in class_name for term in terminal_terms)

def _telethon_media_info(message) -> tuple[str, dict]:
    return telethon_media_info(
        message,
        event_logger=logger,
        photo_type=types.MessageMediaPhoto,
        document_type=types.MessageMediaDocument,
    )

USER_AGENT_SOURCE_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "../user_agent_srv.py"))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(USER_AGENT_SOURCE_FILE))))

SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")

SESSION_NAME = os.getenv("USER_SESSION_NAME", "user_session")

SESSION_FILE = os.path.join(BASE_DIR, f"{SESSION_NAME}.session")

@dataclass
class ArchiveMediaResult:
    ok: bool
    code: str = "ok"
    detail: str = ""
    size_bytes: int | None = None

    def __bool__(self) -> bool:
        return self.ok

    @property
    def size_mb(self) -> float | None:
        if self.size_bytes is None:
            return None
        return self.size_bytes / 1024 / 1024

def _is_session_file_healthy(path: str) -> bool:
    """
    Lightweight integrity check for a Telethon .session file (SQLite database).

    Telethon sessions are SQLite files. A corrupt or partial file will cause
    TelegramClient to raise struct.unpack / DatabaseError on first use, crashing
    the worker process. This check opens the file as SQLite and reads the sessions
    table, which is enough to confirm the file is not corrupt.

    On failure, renames the file to .session.corrupt.{timestamp} so it won't
    be picked up on subsequent scans, and logs a warning.
    """
    import sqlite3
    import time as _time

    if not os.path.exists(path):
        return False

    try:
        conn = sqlite3.connect(path, timeout=5)
        cursor = conn.cursor()
        # Telethon writes a 'sessions' table; reading it verifies file integrity
        cursor.execute("SELECT dc_id FROM sessions LIMIT 1")
        conn.close()
        return True
    except Exception as e:
        # Corrupt / truncated / not a Telethon session file
        logger.warning(
            f"    [UserAgent] Session file appears corrupt: {path} ({e}) -- "
            f"renaming to .corrupt and skipping."
        )
        try:
            corrupt_path = f"{path}.corrupt.{int(_time.time())}"
            os.rename(path, corrupt_path)
            logger.warning(f"    [UserAgent] Moved corrupt session to: {corrupt_path}")
        except Exception as e_rename:
            logger.error(f"    [UserAgent] Could not rename corrupt session {path}: {e_rename}")
        return False

__all__ = ['ARCHIVE_TMP_DIR', 'ARCHIVE_TMP_PREFIX', 'ArchiveMediaResult', 'BASE_DIR', 'SESSIONS_DIR', 'SESSION_FILE', 'SESSION_NAME', 'SecurityError', 'TelegramClient', 'TelegramClientLifecycle', 'USER_AGENT_SOURCE_FILE', 'UTC', '_MTPROTO_CONFLICT_BACKOFF', '_MTPROTO_MAX_RETRIES', '_async_execute', '_credential_lookup_filter_for_source', '_execute_async_shared', '_is_session_file_healthy', '_is_terminal_invite_error', '_telethon_media_info', '_username_from_meta', 'asyncio', 'contextlib', 'dataclass', 'datetime', 'errors', 'logger', 'logging', 'os', 'settings', 'telethon_media_info', 'time', 'timedelta', 'types', 'uuid']
