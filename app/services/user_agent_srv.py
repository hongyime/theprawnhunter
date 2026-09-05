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
from app.services._scraper.lifecycle import TelegramClientLifecycle

logger = logging.getLogger("user_agent")

# MTProto conflict backoff (seconds) -- kept short since connections are brief
_MTPROTO_CONFLICT_BACKOFF = 10
_MTPROTO_MAX_RETRIES = 3
ARCHIVE_TMP_DIR = "/tmp"
ARCHIVE_TMP_PREFIX = "archive_"


async def _async_execute(query_builder):
    return await asyncio.to_thread(query_builder.execute)


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
    if not getattr(message, "media", None):
        return "text", {}

    file_meta = {}
    try:
        from telethon import utils as telethon_utils

        file_id = telethon_utils.pack_bot_file_id(message.media)
        if file_id:
            file_meta["file_id"] = file_id
    except Exception:
        pass

    if isinstance(message.media, types.MessageMediaPhoto):
        photo = getattr(message.media, "photo", None)
        file_meta["wc"] = "photo"
        file_meta["id"] = getattr(photo, "id", 0)
        return "photo", file_meta

    if isinstance(message.media, types.MessageMediaDocument):
        document = getattr(message.media, "document", None)
        mime = getattr(document, "mime_type", None) or getattr(getattr(message, "file", None), "mime_type", None)
        if mime:
            file_meta["mime"] = mime
        file_name = getattr(getattr(message, "file", None), "name", None)
        if file_name:
            file_meta["file_name"] = file_name
        doc_id = getattr(document, "id", None)
        if doc_id is not None:
            file_meta["id"] = doc_id
        if isinstance(mime, str) and mime.startswith("video/"):
            return "video", file_meta
        if isinstance(mime, str) and mime.startswith("audio/"):
            return "audio", file_meta
        return "document", file_meta

    return "other", file_meta


# Determine absolute path to project root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")

# Support multiple accounts via Env Var (default: user_session)
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


class UserAgentService:
    """
    Service acting as a real Telegram User (not a bot).
    Used for actions bots cannot perform, like inviting other bots to groups.
    """
    def __init__(self):
        self.api_id = settings.TELEGRAM_API_ID
        self.api_hash = settings.TELEGRAM_API_HASH
        self.client = None
        self.lock = asyncio.Lock()

        # Rotation Logic
        self.sessions = [] # List of session paths
        self.current_index = 0
        self.current_session_name = "unknown"
        self._refresher_task = None
        self._ensure_task = None
        self._session_lock_key = None
        # Instance ID must be stable across container restarts.
        # Using a fixed process name (derived from env or hardcoded fallback) instead of hostname
        # because Docker generates new hostnames on each container recreate.
        import os as _os
        self._instance_id = _os.getenv("WORKER_INSTANCE_ID", "worker-scrape")  # Override via env if needed
        # Unique lock owner for fencing — hostname:pid:uuid means release_lock
        # is a CAS on THIS instance, preventing cross-worker steals when TTL
        # expires mid-session.
        import uuid as _uuid
        self._lock_owner = f"{self._instance_id}:{_os.getpid()}:{_uuid.uuid4().hex[:8]}"
        self._current_phone = None
        self._tmp_archive_sweep_done = False
        # In-memory session cooldown diagnostics. Redis remains the cross-worker
        # source of truth; these dicts give operators a per-process view and
        # drive the 5min/30min escalating local cooldown enforced in start().
        # Keyed by absolute session_path.
        self._session_cooldowns: dict = {}          # session_path -> datetime (UTC) expiry
        self._session_failure_history: dict = {}    # session_path -> list[datetime] within last hour
        self._session_cooldown_started: dict = {}   # session_path -> datetime when cooldown began
        self._last_failure_reason: str | None = None
        self._all_on_cooldown_warned_at = None      # datetime | None, for warning rate-limit

    def _discover_sessions(self):
        """Scans BASE_DIR and telegram_accounts DB for valid .session files."""
        new_sessions = set() # Use set to avoid duplicates

        # 1. Check Env Var Override first (Single Session Mode)
        env_session = os.getenv("USER_SESSION_NAME")
        if env_session:
            path = os.path.join(SESSIONS_DIR, f"{env_session}.session")
            if os.path.exists(path):
                new_sessions.add(path)
                self.sessions = sorted(new_sessions)
                return

        # 2. Scan Directory (sessions/)
        if not os.path.exists(SESSIONS_DIR):
            os.makedirs(SESSIONS_DIR, exist_ok=True)
        try:
            for f in os.listdir(SESSIONS_DIR):
                if f.endswith(".session"):
                    if f in ["anon.session", "journal.session"]:
                        continue
                    if f.startswith("bot_"):
                        continue
                    full_path = os.path.abspath(os.path.join(SESSIONS_DIR, f))
                    if _is_session_file_healthy(full_path):
                        new_sessions.add(full_path)
        except Exception as e:
            logger.error(f"    ❌ [UserAgent] Directory scan failed for {SESSIONS_DIR}: {e}")

        # 3. Discover via Database (Requirement-aligned tracking)
        try:
            from app.core.database import db
            res = db.table("telegram_accounts").select("session_path").eq("status", "active").execute()
            for row in res.data:
                path = row.get("session_path")
                if path:
                    # Double check existence
                    if os.path.exists(path):
                        new_sessions.add(os.path.abspath(path))
                    else:
                        # Maybe it was relative?
                        rel_path = os.path.join(BASE_DIR, os.path.basename(path))
                        if os.path.exists(rel_path):
                            new_sessions.add(os.path.abspath(rel_path))
        except Exception as e:
            logger.warning(f"[UserAgent] DB session discovery failed: {e}")

        # Fallback to default if nothing found (legacy support)
        if not new_sessions:
            default_path = os.path.abspath(os.path.join(SESSIONS_DIR, "user_session.session"))
            new_sessions.add(default_path)

        final_list = sorted(new_sessions)

        # Log only if the session list has changed
        if final_list != self.sessions:
            logger.info(f"    🔄 [UserAgent] Discovered {len(final_list)} session(s): {[os.path.basename(s) for s in final_list]}")

        self.sessions = final_list

    # ------------------------------------------------------------------
    # Session cooldown diagnostics (in-memory pool status).
    # Redis-backed cooldowns handle cross-worker coordination; the local
    # dict below is per-process and drives get_pool_status(), the
    # "all sessions on cooldown" WARNING, and recovery INFO logs.
    # ------------------------------------------------------------------
    def _mark_session_failed(self, session_path: str, reason: str) -> None:
        """Add session_path to the local cooldown dict with escalating duration.

        Baseline cooldown is 5 minutes. If another failure for the same session
        was recorded within the last hour, escalate to 30 minutes. Cooldown is
        extend-only -- an existing longer cooldown is preserved.
        """
        from datetime import datetime, timedelta
        now = datetime.now(UTC)
        one_hour_ago = now - timedelta(hours=1)

        history = [
            ts for ts in self._session_failure_history.get(session_path, [])
            if ts >= one_hour_ago
        ]
        history.append(now)
        self._session_failure_history[session_path] = history

        minutes = 30 if len(history) > 1 else 5
        expires_at = now + timedelta(minutes=minutes)

        # Extend-only: never shorten an existing local cooldown
        existing = self._session_cooldowns.get(session_path)
        if existing is None or expires_at > existing:
            self._session_cooldowns[session_path] = expires_at
        # Preserve original cooldown-start for accurate recovery duration
        self._session_cooldown_started.setdefault(session_path, now)

        session_name = os.path.splitext(os.path.basename(session_path))[0]
        self._last_failure_reason = f"{session_name}: {reason[:180]}"
        logger.info(
            f"    ⏳ [UserAgent] Session '{session_name}' local cooldown {minutes}m "
            f"(failures in last hour: {len(history)}, until "
            f"{self._session_cooldowns[session_path].isoformat()})"
        )

    def _is_session_on_local_cooldown(self, session_path: str) -> bool:
        """Return True if session_path is currently in local cooldown."""
        from datetime import datetime
        expires_at = self._session_cooldowns.get(session_path)
        if not expires_at:
            return False
        if datetime.now(UTC) >= expires_at:
            # Expired -- clean up so it doesn't skew get_pool_status()
            self._session_cooldowns.pop(session_path, None)
            return False
        return True

    def _sync_redis_cooldown_to_local(self, session_path: str, cooldown_key: str) -> None:
        """Mirror the Redis-backed cooldown TTL into the local dict (extend-only).

        Called from start() before Redis is consulted so get_pool_status()
        reflects cross-worker cooldowns (FloodWait, MTProto backoff, etc.)
        set outside of _mark_session_failed().
        """
        from datetime import datetime, timedelta
        try:
            from app.core.redis_srv import redis_srv
            ttl = redis_srv.get_cooldown_remaining(cooldown_key)
        except Exception:
            return
        if not ttl or ttl <= 0:
            return
        now = datetime.now(UTC)
        new_expiry = now + timedelta(seconds=int(ttl))
        existing = self._session_cooldowns.get(session_path)
        if existing is None or new_expiry > existing:
            self._session_cooldowns[session_path] = new_expiry
            self._session_cooldown_started.setdefault(session_path, now)

    def _log_recovery_if_applicable(self, session_path: str) -> None:
        """Emit an INFO log with duration-on-cooldown if this session was previously cooled down."""
        from datetime import datetime
        started_at = self._session_cooldown_started.pop(session_path, None)
        if started_at is None:
            return
        duration_s = (datetime.now(UTC) - started_at).total_seconds()
        session_name = os.path.splitext(os.path.basename(session_path))[0]
        logger.info(
            f"    🟢 [UserAgent] Session '{session_name}' recovered after "
            f"{duration_s:.0f}s on cooldown."
        )
        # Clear cooldown state on successful connect
        self._session_cooldowns.pop(session_path, None)
        self._session_failure_history.pop(session_path, None)

    def _emit_all_on_cooldown_warning(self) -> None:
        """When every known session is on local cooldown, log a WARNING with the earliest recovery time."""
        from datetime import datetime
        if not self.sessions:
            return
        now = datetime.now(UTC)
        active = {
            p: self._session_cooldowns[p]
            for p in self.sessions
            if p in self._session_cooldowns and self._session_cooldowns[p] > now
        }
        if len(active) < len(self.sessions):
            return  # At least one session still available in local view

        # Rate-limit warning to once per 60s to avoid log spam on tight retry loops
        if self._all_on_cooldown_warned_at is not None:
            if (now - self._all_on_cooldown_warned_at).total_seconds() < 60:
                return
        self._all_on_cooldown_warned_at = now

        earliest_path = min(active, key=active.get)
        earliest_expiry = active[earliest_path]
        remaining_s = max(0.0, (earliest_expiry - now).total_seconds())
        earliest_name = os.path.splitext(os.path.basename(earliest_path))[0]
        logger.warning(
            f"    🛑 [UserAgent] ALL {len(self.sessions)} session(s) on cooldown. "
            f"Earliest recovery: '{earliest_name}' at {earliest_expiry.isoformat()} "
            f"(~{remaining_s:.0f}s from now). Last failure: "
            f"{self._last_failure_reason or 'unknown'}"
        )

    def get_pool_status(self) -> dict:
        """Return current pool status for /health endpoints and admin diagnostics.

        Reflects the local (this-process) cooldown state. Redis-backed cooldowns
        used for cross-worker coordination are mirrored into the local dict on
        every start() attempt so this stays a useful diagnostic surface.
        """
        from datetime import datetime
        now = datetime.now(UTC)
        on_cooldown_expiries: list = []
        for session_path in self.sessions:
            expires = self._session_cooldowns.get(session_path)
            if expires and expires > now:
                on_cooldown_expiries.append(expires)

        oldest_expires_at = min(on_cooldown_expiries) if on_cooldown_expiries else None

        return {
            "total_sessions": len(self.sessions),
            "available_now": len(self.sessions) - len(on_cooldown_expiries),
            "on_cooldown_count": len(on_cooldown_expiries),
            "oldest_cooldown_expires_at": oldest_expires_at.isoformat() if oldest_expires_at else None,
            "current_session_name": self.current_session_name,
            "last_failure_reason": self._last_failure_reason,
        }

    async def _session_refresher_loop(self):
        """Background loop to periodically scan for new .session files."""
        while True:
            await asyncio.sleep(60)
            self._discover_sessions()

    async def start(self):
        """
        Starts the user client.
        Rotates through available sessions to find a usable one.
        On-demand pattern: caller MUST call _disconnect() when done.
        """
        if not self._tmp_archive_sweep_done:
            await asyncio.to_thread(self._cleanup_stale_tmp_archives)
            self._tmp_archive_sweep_done = True

        if not self.sessions:
            self._discover_sessions()

        # Start background refresher if not already running
        if self._refresher_task is None:
            self._refresher_task = asyncio.create_task(self._session_refresher_loop())

        if self._ensure_task is None or self._ensure_task.done():
            from app.core.redis_srv import redis_srv
            if not redis_srv.is_on_cooldown("user_agent:ensure_membership"):
                self._ensure_task = asyncio.create_task(self._ensure_monitor_bots_membership())
                redis_srv.set_cooldown("user_agent:ensure_membership", 6 * 3600)

        # Try up to N times (where N = number of sessions) to find a usable one
        from app.core.redis_srv import redis_srv

        attempts = len(self.sessions)
        for _ in range(attempts):
            # 1. Round Robin Selection (Global Redis Counter)
            global_idx = redis_srv.get_next_rotation_index("user_agent", attempts)

            session_path = self.sessions[global_idx]
            session_name = os.path.splitext(os.path.basename(session_path))[0]

            # Update local reference
            self.current_index = global_idx

            # 2. Check Cooldown for THIS session
            # Use distinct key namespaces: cooldown vs lock.
            # Previously both used `user_agent:{session_name}` which caused
            # release_lock() to also clear the cooldown -- meaning FloodWait
            # cooldowns were silently wiped on every disconnect.
            #
            # Local in-memory cooldown gate is checked first (fast path) and
            # mirrors Redis state so get_pool_status() stays accurate for the
            # operator-facing diagnostic surface.
            if self._is_session_on_local_cooldown(session_path):
                from datetime import datetime as _dt
                _exp = self._session_cooldowns[session_path]
                _remaining = int((_exp - _dt.now(UTC)).total_seconds())
                logger.info(
                    f"    ⏳ [UserAgent] Session '{session_name}' local cooldown "
                    f"({_remaining}s, until {_exp.isoformat()}). Rotating..."
                )
                continue

            cooldown_key = f"user_agent:cooldown:{session_name}"
            # Mirror Redis cooldown TTL into the local dict so cross-worker
            # cooldowns (FloodWait, MTProto backoff) are visible via get_pool_status().
            self._sync_redis_cooldown_to_local(session_path, cooldown_key)
            if redis_srv.is_on_cooldown(cooldown_key):
                 ttl = redis_srv.get_cooldown_remaining(cooldown_key)
                 logger.info(f"    ⏳ [UserAgent] Session '{session_name}' on cooldown ({ttl}s). Rotating...")
                 continue

            lock_key = f"user_agent:lock:{session_name}"
            if not redis_srv.acquire_lock(lock_key, 600, owner=self._lock_owner):
                logger.info(f"    🔒 [UserAgent] Session '{session_name}' locked by another worker. Rotating...")
                continue
            self._session_lock_key = lock_key
            self._current_phone = None
            if not await self._acquire_db_lease(session_path):
                if self._session_lock_key:
                    redis_srv.release_lock(self._session_lock_key, owner=self._lock_owner)
                    self._session_lock_key = None
                continue


            # 3. Check if already connected is THIS session
            if self.client and self.client.is_connected():
                if getattr(self.client.session, 'filename', '') == session_path:
                    self.current_session_name = session_name # Update tracker
                    return True

                # Disconnect old
                session_filename = getattr(self.client.session, 'filename', None)
                await self.client.disconnect()
                if session_filename:
                    self._cleanup_temp_session(session_filename)
                if self._session_lock_key:
                    redis_srv.release_lock(self._session_lock_key, owner=self._lock_owner)
                    self._session_lock_key = None
                await self._release_db_lease()

            # 4. Initialize & Connect
            import shutil
            import sqlite3
            TEMP_SESSION_PATH = f"/tmp/{session_name}" # Unique tmp path per session

            try:
                if os.path.exists(session_path):
                    shutil.copy2(session_path, f"{TEMP_SESSION_PATH}.session")

                conn = sqlite3.connect(f"{TEMP_SESSION_PATH}.session")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=20000")
                conn.close()

                self.client = TelegramClient(TEMP_SESSION_PATH, self.api_id, self.api_hash)
                await self.client.connect()

                if not await self.client.is_user_authorized():
                    logger.warning(f"    ⚠️ [UserAgent] Session '{session_name}' invalid/expired. Skipping.")
                    await self.client.disconnect()
                    self._cleanup_temp_session(f"{TEMP_SESSION_PATH}.session")
                    redis_srv.incr_key(f"user_agent_fail:{session_name}", 3600)
                    self._mark_session_failed(session_path, "not authorized (invalid/expired session)")
                    if self._session_lock_key:
                        redis_srv.release_lock(self._session_lock_key, owner=self._lock_owner)
                        self._session_lock_key = None
                    await self._release_db_lease()
                    continue

                self.current_session_name = session_name
                redis_srv.reset_key(f"user_agent_fail:{session_name}")
                self._log_recovery_if_applicable(session_path)
                logger.info(f"    ✅ [UserAgent] Connected with session: {session_name}")
                return True

            except SecurityError as e:
                if "Too many messages had to be ignored" in str(e):
                    logger.warning(
                        f"    🔴 [UserAgent] MTProto conflict detected for '{session_name}': {e}. "
                        f"Backing off for {_MTPROTO_CONFLICT_BACKOFF}s..."
                    )
                    with contextlib.suppress(Exception):
                        await self.client.disconnect()
                    self._cleanup_temp_session(f"{TEMP_SESSION_PATH}.session")
                    redis_srv.incr_key(f"user_agent_fail:{session_name}", 3600)
                    redis_srv.set_cooldown(cooldown_key, _MTPROTO_CONFLICT_BACKOFF + 5)
                    self._mark_session_failed(session_path, f"MTProto conflict: {str(e)[:120]}")
                    if self._session_lock_key:
                        redis_srv.release_lock(self._session_lock_key, owner=self._lock_owner)
                        self._session_lock_key = None
                    await self._release_db_lease()
                    await asyncio.sleep(_MTPROTO_CONFLICT_BACKOFF)
                    continue
                raise
            except Exception as e:
                logger.warning(f"    ⚠️ [UserAgent] Failed to connect '{session_name}': {e}")
                self._cleanup_temp_session(f"{TEMP_SESSION_PATH}.session")
                fail_count = redis_srv.incr_key(f"user_agent_fail:{session_name}", 3600)
                if fail_count >= _MTPROTO_MAX_RETRIES:
                    redis_srv.set_cooldown(cooldown_key, 120)
                self._mark_session_failed(session_path, f"connect error: {str(e)[:120]}")
                if self._session_lock_key:
                    redis_srv.release_lock(self._session_lock_key, owner=self._lock_owner)
                    self._session_lock_key = None
                await self._release_db_lease()
                continue

        logger.error("    ❌ [UserAgent] All sessions failed or on cooldown.")
        self._emit_all_on_cooldown_warning()
        return False

    def _cleanup_stale_tmp_archives(self) -> int:
        """Remove orphaned transient archive files left by ungraceful shutdowns."""
        removed = 0
        now = time.time()
        max_age = settings.ARCHIVE_STALE_TMP_MAX_AGE_SECONDS
        try:
            names = os.listdir(ARCHIVE_TMP_DIR)
        except FileNotFoundError:
            return 0
        except Exception as e:
            logger.warning(f"    ⚠️ [UserAgent] Could not scan archive temp dir: {e}")
            return 0

        for name in names:
            if not name.startswith(ARCHIVE_TMP_PREFIX):
                continue
            path = os.path.join(ARCHIVE_TMP_DIR, name)
            try:
                if os.path.isfile(path) and now - os.path.getmtime(path) > max_age:
                    os.remove(path)
                    removed += 1
            except Exception as e:
                logger.warning(f"    ⚠️ [UserAgent] Could not remove stale archive temp file {path}: {e}")

        if removed:
            logger.info(f"    [UserAgent] Removed {removed} stale transient archive file(s).")
        return removed

    async def _disconnect(self):
        async def _disconnect_client():
            if self.client and self.client.is_connected():
                session_filename = getattr(self.client.session, 'filename', None)
                await self.client.disconnect()
                if session_filename:
                    self._cleanup_temp_session(session_filename)
        try:
            lifecycle = TelegramClientLifecycle(
                disconnect=_disconnect_client,
                disconnect_timeout=settings.TELEGRAM_CLIENT_DISCONNECT_TIMEOUT_SECONDS,
                label="user_agent",
                logger=logger,
            )
            await lifecycle.disconnect_safely()
        finally:
            try:
                if self._session_lock_key:
                    from app.core.redis_srv import redis_srv
                    redis_srv.release_lock(self._session_lock_key, owner=self._lock_owner)
                    self._session_lock_key = None
                await self._release_db_lease()
            except Exception as e2:
                logger.warning(f"    ⚠️ [UserAgent] Error releasing locks: {e2}")

    async def _acquire_db_lease(self, session_path: str) -> bool:
        try:
            from app.core.database import db
            abs_path = os.path.abspath(session_path)
            res = await asyncio.to_thread(
                lambda: db.table("telegram_accounts").select("phone,locked_by,locked_until").eq("session_path", abs_path).limit(1).execute()
            )
            if not res.data:
                return True
            row = res.data[0]
            phone = row.get("phone")
            if not phone:
                return True

            # Check if we already hold this lease (same instance_id and not expired)
            current_holder = row.get("locked_by")
            current_until = row.get("locked_until")
            if current_holder == self._instance_id and current_until:
                # We already hold it -- just refresh the TTL
                lease_until = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
                await asyncio.to_thread(
                    lambda: db.table("telegram_accounts")
                        .update({"locked_until": lease_until})
                        .eq("phone", phone)
                        .eq("locked_by", self._instance_id)
                        .execute()
                )
                self._current_phone = phone
                return True

            # Try to acquire fresh lease (only if unlocked or expired)
            lease_until = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
            updated = await asyncio.to_thread(
                lambda: db.table("telegram_accounts")
                    .update({"locked_by": self._instance_id, "locked_until": lease_until})
                    .eq("phone", phone)
                    .or_("locked_until.is.null,locked_until.lt.now()")
                    .execute()
            )
            if updated.data:
                self._current_phone = phone
                return True
            return False
        except Exception as e:
            logger.warning(f"    ⚠️ [UserAgent] DB lease failed: {e}")
            return True

    async def _release_db_lease(self):
        if not self._current_phone:
            return
        try:
            from app.core.database import db
            await asyncio.to_thread(
                lambda: db.table("telegram_accounts")
                    .update({"locked_by": None, "locked_until": None})
                    .eq("phone", self._current_phone)
                    .eq("locked_by", self._instance_id)
                    .execute()
            )
        except Exception as e:
            logger.warning(f"    ⚠️ [UserAgent] DB lease release failed: {e}")
        finally:
            self._current_phone = None

    async def stop(self):
        """Graceful shutdown -- disconnect and cancel background tasks."""
        async with self.lock:
            await self._disconnect()
            if self._refresher_task and not self._refresher_task.done():
                self._refresher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._refresher_task
                self._refresher_task = None

    def _cleanup_temp_session(self, filename: str):
        """Removes the temporary session files from /tmp/"""
        if not filename or not filename.startswith("/tmp/"): return
        try:
            if os.path.exists(filename): os.remove(filename)
            if os.path.exists(filename + "-wal"): os.remove(filename + "-wal")
            if os.path.exists(filename + "-shm"): os.remove(filename + "-shm")
        except OSError as e:
            logger.warning(f"    ⚠️ [UserAgent] Failed to cleanup {filename}: {e}")

    async def invite_bot_to_group(self, bot_username: str, group_id: int | str) -> bool:
        """
        Invites a bot to the specified group (chat/channel).
        """
        async with self.lock:
            if not await self.start():
                return False

            try:
                bot_entity = await self.client.get_entity(bot_username)
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                group_entity = await self.client.get_entity(target)

                logger.info(f"    🚀 [UserAgent] Inviting {bot_username} to group...")
                from telethon.tl.functions.channels import InviteToChannelRequest
                from telethon.tl.functions.messages import AddChatUserRequest

                try:
                    await self.client(InviteToChannelRequest(channel=group_entity, users=[bot_entity]))
                    logger.info("    ✅ [UserAgent] Invite successful (Channel/Supergroup).")
                    return True
                except errors.UserAlreadyParticipantError:
                    logger.info("    ℹ️ [UserAgent] Bot is already inside the group.")
                    return True
                except errors.FloodWaitError:
                    raise
                except Exception as e_channel:
                    if _is_terminal_invite_error(e_channel):
                        logger.error(f"    ❌ [UserAgent] Terminal invite failure: {e_channel}")
                        return False
                    logger.debug(f"    ℹ️ [UserAgent] InviteToChannelRequest skipped ({e_channel}), trying AddChatUserRequest...")
                    try:
                        await self.client(AddChatUserRequest(chat_id=group_entity.id, user_id=bot_entity, fwd_limit=0))
                        logger.info("    ✅ [UserAgent] Invite successful (Basic Chat).")
                        return True
                    except errors.UserAlreadyParticipantError:
                        logger.info("    ℹ️ [UserAgent] Bot is already inside the group.")
                        return True
                    except errors.FloodWaitError:
                        raise
                    except Exception as e_chat:
                        logger.error(f"    ❌ [UserAgent] Invite failed (Channel err: {e_channel} | Chat err: {e_chat})")
                        return False
            except errors.FloodWaitError as e:
                await self._handle_flood_error(e)
                return False
            except Exception as e:
                logger.error(f"    ❌ [UserAgent] Error: {e}")
                return False
            finally:
                await self._disconnect()

    async def kick_bot_from_group(self, bot_username: str, group_id: int | str) -> bool:
        """
        Kicks/bans then unbans a bot from the monitor group.
        Used after Matkap-style forwarding to remove the victim bot
        so it cannot see further group messages (OPSEC cleanup).
        """
        async with self.lock:
            if not await self.start():
                return False
            try:
                bot_entity = await self.client.get_entity(bot_username)
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                group_entity = await self.client.get_entity(target)

                from datetime import datetime, timedelta

                from telethon.tl.functions.channels import EditBannedRequest
                from telethon.tl.types import ChatBannedRights

                # Ban (kicks non-admin bots immediately)
                await self.client(EditBannedRequest(
                    channel=group_entity,
                    participant=bot_entity,
                    banned_rights=ChatBannedRights(
                        until_date=datetime.now(UTC) + timedelta(seconds=30),
                        view_messages=True,
                    )
                ))
                # Unban so the bot can be re-invited in the future if needed
                await self.client(EditBannedRequest(
                    channel=group_entity,
                    participant=bot_entity,
                    banned_rights=ChatBannedRights(until_date=None)
                ))
                logger.info(f"    ✅ [UserAgent] Kicked @{bot_username} from group (ban+unban).")
                return True
            except errors.FloodWaitError as e:
                await self._handle_flood_error(e)
                return False
            except Exception as e:
                logger.warning(f"    ⚠️ [UserAgent] kick_bot_from_group failed for @{bot_username}: {e}")
                return False
            finally:
                await self._disconnect()

    async def _handle_flood_error(self, e):
        """Logs and sets persistent cooldown for FloodWaitError (Per Session)."""
        from app.core.redis_srv import redis_srv
        wait_seconds = e.seconds
        current_session = getattr(self, 'current_session_name', 'unknown')
        # Must use the same namespace as the cooldown check in start()
        cooldown_key = f"user_agent:cooldown:{current_session}"
        if wait_seconds > 300:
            logger.warning(f"\n🛑 [UserAgent] SEVERE FLOOD WAIT for '{current_session}': {wait_seconds}s.")
            redis_srv.set_cooldown(cooldown_key, wait_seconds + 60)
        else:
            logger.warning(f"    🛑 [UserAgent] FLOOD WAIT for '{current_session}': {wait_seconds}s.")
            redis_srv.set_cooldown(cooldown_key, wait_seconds + 10)

    async def find_topic_id(self, group_id: int | str, topic_name: str) -> int | None:
        async with self.lock:
            if not await self.start(): return None
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                from telethon.tl.functions.channels import GetForumTopicsRequest
                res = await self.client(GetForumTopicsRequest(channel=entity, q=topic_name, offset_date=0, offset_id=0, offset_topic=0, limit=10))
                if res.topics:
                    for topic in res.topics:
                        if topic.title == topic_name:
                            logger.info(f"    🔍 [UserAgent] Found existing topic: {topic.title} ({topic.id})")
                            return topic.id
                return None
            except Exception as e:
                logger.warning(f"    ⚠️ [UserAgent] Find topic failed: {e}")
                return None
            finally: await self._disconnect()

    async def check_membership(self, group_id: int | str, user_identifier: str | int) -> dict | None:
        async with self.lock:
            if not await self.start(): return None
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                group_entity = await self.client.get_entity(target)
                if str(user_identifier).lstrip('-').isdigit(): user_target = int(user_identifier)
                else: user_target = user_identifier
                try: user_entity = await self.client.get_entity(user_target)
                except Exception: return None
                from telethon.tl.functions.channels import GetParticipantRequest
                try:
                    result = await self.client(GetParticipantRequest(channel=group_entity, participant=user_entity))
                    return {
                        "id": getattr(user_entity, 'id', 0),
                        "username": getattr(user_entity, 'username', None),
                        "is_admin": hasattr(result.participant, 'admin_rights') and result.participant.admin_rights is not None
                    }
                except Exception as e:
                    if "USER_NOT_PARTICIPANT" in str(e) or "400" in str(e): return None
                    return None
            except Exception: return None
            finally: await self._disconnect()

    async def promote_to_admin(self, group_id: int | str, user_identifier: str | int, title: str = "Admin", anonymous: bool = True) -> bool:
        async with self.lock:
            if not await self.start(): return False
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                group_entity = await self.client.get_entity(target)
                if str(user_identifier).lstrip('-').isdigit(): user_target = int(user_identifier)
                else: user_target = user_identifier
                user_entity = await self.client.get_entity(user_target)
                from telethon.tl.functions.channels import EditAdminRequest
                from telethon.tl.types import ChatAdminRights
                admin_rights = ChatAdminRights(
                    change_info=True, post_messages=True, edit_messages=True, delete_messages=True,
                    ban_users=True, invite_users=True, pin_messages=True, manage_call=True,
                    other=True, manage_topics=True, anonymous=anonymous
                )
                await self.client(EditAdminRequest(channel=group_entity, user_id=user_entity, admin_rights=admin_rights, rank=title))
                logger.info(
                    "    👑 [UserAgent] Promoted account to admin (anon=%s) in group.",
                    anonymous,
                )
                return True
            except errors.FloodWaitError as e:
                await self._handle_flood_error(e)
                return False
            except Exception as e:
                logger.error(
                    "    ❌ [UserAgent] Promote failed: %s",
                    type(e).__name__,
                )
                return False
            finally: await self._disconnect()

    async def _connect_to_session(self, session_path: str) -> bool:
        """Internal helper to connect to a specific session file."""
        session_name = os.path.splitext(os.path.basename(session_path))[0]
        import shutil
        import sqlite3
        TEMP_SESSION_PATH = f"/tmp/setup_{session_name}"
        try:
            if os.path.exists(session_path):
                shutil.copy2(session_path, f"{TEMP_SESSION_PATH}.session")
            conn = sqlite3.connect(f"{TEMP_SESSION_PATH}.session")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.close()
            self.client = TelegramClient(TEMP_SESSION_PATH, self.api_id, self.api_hash)
            await self.client.connect()
            return await self.client.is_user_authorized()
        except Exception: return False

    async def _ensure_monitor_bots_membership(self):
        """Checks and ensures all broadcaster bots and user accounts are in the monitor group."""
        try:
            tokens = settings.bot_tokens
            group_id = settings.MONITOR_GROUP_ID
            if not tokens or not group_id: return
            logger.info("    🐶 [UserAgent] Syncing Hub memberships and permissions...")
            for token in tokens:
                try:
                    bot_id = int(token.split(':')[0])
                    member = await self.check_membership(group_id, bot_id)
                    if not member:
                        from telegram import Bot
                        temp_bot = Bot(token)
                        me = await temp_bot.get_me()
                        if await self.invite_bot_to_group(me.username, group_id):
                            await self.promote_to_admin(group_id, me.username, anonymous=False)
                    elif not member.get("is_admin"):
                        await self.promote_to_admin(group_id, bot_id, anonymous=False)
                except Exception: pass
            if not self.sessions: self._discover_sessions()
            for session_path in self.sessions:
                if not await self._connect_to_session(session_path): continue
                try:
                    me = await self.client.get_me()
                    await self._disconnect()
                    member = await self.check_membership(group_id, me.id)
                    if member:
                        if not member.get("is_admin"):
                            await self.promote_to_admin(group_id, me.id, anonymous=True)
                    else:
                        logger.warning(f"    ⚠️ User @{me.username} is NOT in Hub. Please add manually.")
                except Exception: pass
        except Exception as e: logger.error(f"    ❌ [UserAgent] Membership sync fatal error: {e}")

    async def send_message(self, target: int | str, message: str, thread_id: int | None = None) -> bool:
        """Sends a text message to a target (group/user) as the User Agent."""
        async with self.lock:
            if not await self.start(): return False
            try:
                entity = int(target) if str(target).lstrip("-").isdigit() else target
                await self.client.send_message(entity, message, reply_to=thread_id)
                logger.info(f"    🗣️ [UserAgent] Sent (session={self.current_session_name}): '{message[:30]}...'")
                return True
            except Exception as e:
                logger.error(f"    ❌ [UserAgent] Send failed: {e}")
                return False
            finally: await self._disconnect()

    @staticmethod
    def _coerce_chat_ref(value: int | str) -> int | str:
        return int(value) if str(value).lstrip("-").isdigit() else value

    @staticmethod
    def _archive_temp_path(message, message_id: int) -> str:
        original_name = os.path.basename(str(getattr(getattr(message, "file", None), "name", "") or ""))
        filename = f"{ARCHIVE_TMP_PREFIX}{uuid.uuid4().hex[:8]}_{message_id}"
        filename = f"{filename}_{original_name}" if original_name else f"{filename}.bin"
        archive_dir = ARCHIVE_TMP_DIR.rstrip("/\\")
        return f"{archive_dir}/{filename}"

    @staticmethod
    def _archive_size_result(message) -> ArchiveMediaResult | None:
        size_bytes = getattr(getattr(message, "file", None), "size", None)
        if size_bytes is None:
            return None

        max_bytes = settings.MAX_ARCHIVE_SIZE_MB * 1024 * 1024
        if size_bytes <= max_bytes:
            return None

        size_mb = size_bytes / 1024 / 1024
        detail = (
            f"Attachment is {size_mb:.1f} MB; "
            f"limit is {settings.MAX_ARCHIVE_SIZE_MB} MB."
        )
        return ArchiveMediaResult(
            ok=False,
            code="too_large",
            detail=detail,
            size_bytes=size_bytes,
        )

    @staticmethod
    def _should_retry_missing_entity(source: int | str, exc: Exception) -> bool:
        return (
            "Could not find the input entity" in str(exc)
            or str(source).lstrip("-").isdigit()
        )

    async def _resolve_archive_username_from_credentials(self, source: int | str) -> str | None:
        lookup_filter = _credential_lookup_filter_for_source(source)
        if not lookup_filter:
            return None

        try:
            from app.core.database import db

            res = await _async_execute(
                db.table("discovered_credentials")
                .select("meta")
                .or_(lookup_filter)
                .limit(1)
            )
        except Exception as exc:
            logger.warning(
                f"    ⚠️ [UserAgent] Failed credential username lookup for source={source}: {exc}"
            )
            return None

        for row in res.data or []:
            username = _username_from_meta(row.get("meta"))
            if username:
                return username
        return None

    async def _get_archive_message_with_entity_fallback(
        self,
        source: int | str,
        message_id: int,
    ):
        try:
            return await self.client.get_messages(source, ids=message_id)
        except (ValueError, TypeError) as entity_err:
            if not self._should_retry_missing_entity(source, entity_err):
                raise

            username_source = await self._resolve_archive_username_from_credentials(source)
            if not username_source:
                detail = (
                    f"Missing Telethon access_hash for raw source {source}; "
                    "no discovered_credentials.meta.bot_username fallback was found."
                )
                logger.warning(f"    ⚠️ [UserAgent] {detail}")
                return ArchiveMediaResult(ok=False, code="missing_access_hash", detail=detail)

            logger.info(
                f"    ℹ️ [UserAgent] Retrying archive source {source} as {username_source}"
            )
            try:
                return await self.client.get_messages(username_source, ids=message_id)
            except (ValueError, TypeError) as retry_err:
                detail = (
                    f"Resolved {source} to {username_source}, but Telethon still could not "
                    f"load msg={message_id}: {retry_err}"
                )
                logger.warning(f"    ⚠️ [UserAgent] {detail}")
                return ArchiveMediaResult(ok=False, code="missing_access_hash", detail=detail[:200])

    async def _send_archived_file_with_retries(
        self,
        target_chat_id: int | str,
        temp_path: str,
        topic_id: int | None,
        caption: str,
    ) -> ArchiveMediaResult:
        attempts = max(1, settings.ARCHIVE_RETRY_ATTEMPTS)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                await asyncio.wait_for(
                    self.client.send_file(
                        target_chat_id,
                        temp_path,
                        caption=(caption or "")[:1024],
                        reply_to=topic_id if topic_id != 1 else None,
                    ),
                    timeout=settings.ARCHIVE_UPLOAD_TIMEOUT_SECONDS,
                )
                return ArchiveMediaResult(ok=True)
            except asyncio.TimeoutError as e:
                last_error = e
                code = "timeout"
            except Exception as e:
                last_error = e
                code = "upload_failed"

            if attempt + 1 < attempts:
                await asyncio.sleep(settings.ARCHIVE_RETRY_BACKOFF_SECONDS * (2 ** attempt))

        detail = str(last_error)[:200] if last_error else "Upload failed."
        if not detail and isinstance(last_error, asyncio.TimeoutError):
            detail = f"Upload exceeded {settings.ARCHIVE_UPLOAD_TIMEOUT_SECONDS}s."
        return ArchiveMediaResult(ok=False, code=code, detail=detail)

    async def archive_media_transiently(
        self,
        entity_or_chat_id: int | str,
        message_id: int,
        target_chat_id: int | str,
        topic_id: int | None = None,
        caption: str = "",
    ) -> ArchiveMediaResult:
        """
        Download a source attachment to a temporary file, re-upload it, then
        immediately remove the local file regardless of upload outcome.
        """
        temp_path = ""
        async with self.lock:
            if not await self.start():
                return ArchiveMediaResult(ok=False, code="session_unavailable", detail="No usable user session.")
            try:
                source = self._coerce_chat_ref(entity_or_chat_id)
                target = self._coerce_chat_ref(target_chat_id)
                message = await self._get_archive_message_with_entity_fallback(source, message_id)
                if isinstance(message, ArchiveMediaResult):
                    return message
                if not message or not getattr(message, "media", None):
                    detail = f"No archiveable media for chat={entity_or_chat_id} msg={message_id}"
                    logger.warning(f"    ⚠️ [UserAgent] {detail}")
                    return ArchiveMediaResult(ok=False, code="not_found", detail=detail)

                size_result = self._archive_size_result(message)
                if size_result is not None:
                    logger.warning(
                        f"    ⚠️ [UserAgent] Archive skipped for msg={message_id}: {size_result.detail}"
                    )
                    return size_result

                temp_path = self._archive_temp_path(message, message_id)
                try:
                    downloaded_path = await asyncio.wait_for(
                        self.client.download_media(message, file=temp_path),
                        timeout=settings.ARCHIVE_DOWNLOAD_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    return ArchiveMediaResult(
                        ok=False,
                        code="timeout",
                        detail=f"Download exceeded {settings.ARCHIVE_DOWNLOAD_TIMEOUT_SECONDS}s.",
                        size_bytes=getattr(getattr(message, "file", None), "size", None),
                    )

                if downloaded_path:
                    temp_path = str(downloaded_path)

                result = await self._send_archived_file_with_retries(
                    target,
                    temp_path,
                    topic_id,
                    caption,
                )
                result.size_bytes = getattr(getattr(message, "file", None), "size", None)
                if result.ok:
                    logger.info(
                        f"    📦 [UserAgent] Archived media msg={message_id} from {entity_or_chat_id} to {target_chat_id}"
                    )
                else:
                    logger.warning(
                        f"    ⚠️ [UserAgent] Archive upload failed for msg={message_id}: {result.code} {result.detail}"
                    )
                return result
            except Exception as e:
                logger.warning(
                    f"    ⚠️ [UserAgent] Transient media archive failed for chat={entity_or_chat_id} msg={message_id}: {e}"
                )
                return ArchiveMediaResult(ok=False, code="error", detail=str(e)[:200])
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except Exception as cleanup_err:
                        logger.warning(f"    ⚠️ [UserAgent] Temp archive cleanup failed: {cleanup_err}")
                await self._disconnect()

    async def clear_removed_users(self, group_id: int | str) -> int:
        async with self.lock:
            if not await self.start(): return 0
            cleared_count = 0
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                from telethon.tl.functions.channels import EditBannedRequest
                from telethon.tl.types import ChannelParticipantsKicked, ChatBannedRights
                async for user in self.client.iter_participants(entity, filter=ChannelParticipantsKicked()):
                    try:
                        await self.client(EditBannedRequest(channel=entity, participant=user, banned_rights=ChatBannedRights(until_date=None, view_messages=False)))
                        cleared_count += 1
                    except Exception: pass
                return cleared_count
            except Exception: return 0
            finally: await self._disconnect()

    async def delete_old_messages(self, group_id: int | str, age_hours: int, topic_id: int | None = None) -> int:
        async with self.lock:
            if not await self.start(): return 0
            import datetime

            from telethon.tl.types import Message
            deleted_count = 0
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                now = datetime.datetime.now(datetime.UTC)
                cutoff = now - datetime.timedelta(hours=age_hours)
                async for message in self.client.iter_messages(entity, reply_to=topic_id):
                    if not isinstance(message, Message): continue
                    if message.date < cutoff:
                        try:
                            await self.client.delete_messages(entity, [message.id])
                            deleted_count += 1
                        except Exception: pass
                return deleted_count
            except Exception: return 0
            finally: await self._disconnect()

    async def get_last_message_id(self, group_id: int | str, topic_id: int) -> int | None:
        async with self.lock:
            if not await self.start(): return None
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                messages = await self.client.get_messages(entity, limit=1, reply_to=topic_id)
                if messages: return messages[0].id
                return None
            except Exception: return None
            finally: await self._disconnect()

    async def get_history(self, group_id: int | str, limit: int) -> list[dict]:
        import os as _os

        from telethon.errors import FloodWaitError
        from telethon.tl.types import Message
        # Minimum sleep between successive get_history calls on the same session.
        # Prevents back-to-back MTProto requests across concurrent Celery tasks from
        # triggering Telegram FloodWait. Tune via MTPROTO_INTER_REQUEST_SLEEP (default 3s).
        INTER_SLEEP = float(_os.getenv("MTPROTO_INTER_REQUEST_SLEEP", 3.0))
        async with self.lock:
            if not await self.start(): return []
            msgs = []
            try:
                target = int(group_id) if str(group_id).lstrip("-").isdigit() else group_id
                entity = await self.client.get_entity(target)
                async for message in self.client.iter_messages(entity, limit=limit):
                    if not isinstance(message, Message): continue
                    content = message.text or ""
                    media_type, file_meta = _telethon_media_info(message)
                    sender_name = "Unknown"
                    if message.sender:
                        if hasattr(message.sender, 'username') and message.sender.username: sender_name = message.sender.username
                        elif hasattr(message.sender, 'first_name'): sender_name = message.sender.first_name
                    msgs.append({
                        "telegram_msg_id": message.id, "sender_name": sender_name, "content": content,
                        "media_type": media_type, "file_meta": file_meta, "chat_id": entity.id if hasattr(entity, 'id') else group_id
                    })
            except FloodWaitError as fwe:
                # Surface FloodWait so caller and logs know -- swallowing it hides the signal
                logger.warning(f"    🛑 [UserAgent] FloodWait in get_history for {group_id}: {fwe.seconds}s")
                from app.core.redis_srv import redis_srv
                session_name = self.current_session_name or "unknown"
                cooldown_key = f"user_agent:cooldown:{session_name}"
                wait = fwe.seconds + 60  # buffer
                if wait > 3600:
                    logger.error(f"    🛑 [UserAgent] SEVERE FLOOD WAIT for '{session_name}': {wait}s.")
                redis_srv.set_cooldown(cooldown_key, wait)
            except Exception as e:
                logger.debug(f"    ⚠️ [UserAgent] get_history error for {group_id}: {e}")
            finally:
                # Inter-request sleep INSIDE the lock so concurrent workers naturally queue
                # behind each other with spacing instead of all firing at once.
                await asyncio.sleep(INTER_SLEEP)
                await self._disconnect()
            return msgs

    async def search_messages(self, query: str, limit: int = 100) -> list[dict]:
        """
        Telegram global search via MTProto SearchGlobalRequest.

        Searches public channels Telegram has indexed (different result space
        from any web scanner). Same lock + cooldown discipline as get_history:
        FloodWait -> redis cooldown on the session, no client kept hot.

        Returns: list of {"text", "chat_id", "chat_name", "message_id", "date"}.
        Empty list if FloodWait, no sessions, or search disabled.
        """
        import os as _os

        from telethon.errors import FloodWaitError
        from telethon.tl.functions.messages import SearchGlobalRequest
        from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

        INTER_SLEEP = float(_os.getenv("MTPROTO_INTER_REQUEST_SLEEP", 3.0))
        results: list[dict] = []

        async with self.lock:
            if not await self.start():
                return []
            try:
                # SearchGlobalRequest needs an InputPeer for offset_peer; use empty.
                res = await self.client(SearchGlobalRequest(
                    q=query,
                    filter=InputMessagesFilterEmpty(),
                    min_date=None,
                    max_date=None,
                    offset_rate=0,
                    offset_peer=InputPeerEmpty(),
                    offset_id=0,
                    limit=limit,
                ))

                # Build chat_id -> chat_name map from res.chats
                chat_map = {}
                for chat in (getattr(res, "chats", []) or []):
                    cid = getattr(chat, "id", None)
                    if cid is None:
                        continue
                    chat_map[cid] = (
                        getattr(chat, "title", None)
                        or getattr(chat, "username", None)
                        or "unknown"
                    )

                for msg in (getattr(res, "messages", []) or []):
                    text = getattr(msg, "message", None)
                    if not text:
                        continue

                    chat_id = None
                    chat_name = None
                    pid = getattr(msg, "peer_id", None)
                    if pid is not None:
                        if hasattr(pid, "channel_id"):
                            raw_id = pid.channel_id
                            chat_id = -1000000000000 - raw_id  # supergroup convention
                            chat_name = chat_map.get(raw_id)
                        elif hasattr(pid, "chat_id"):
                            raw_id = pid.chat_id
                            chat_id = -raw_id
                            chat_name = chat_map.get(raw_id)
                        elif hasattr(pid, "user_id"):
                            chat_id = pid.user_id
                            chat_name = chat_map.get(pid.user_id)

                    results.append({
                        "text": text,
                        "chat_id": chat_id,
                        "chat_name": chat_name,
                        "message_id": getattr(msg, "id", None),
                        "date": str(getattr(msg, "date", None)) if getattr(msg, "date", None) else None,
                    })

                logger.info(f"    🔎 [UserAgent] SearchGlobal('{query[:40]}') -> {len(results)} messages")

            except FloodWaitError as fwe:
                logger.warning(f"    🛑 [UserAgent] FloodWait on search: {fwe.seconds}s -- marking session cooldown")
                from app.core.redis_srv import redis_srv
                session_name = self.current_session_name or "unknown"
                wait = fwe.seconds + 60
                redis_srv.set_cooldown(f"user_agent:cooldown:{session_name}", wait)
            except Exception as e:
                logger.error(f"    ❌ [UserAgent] search_messages failed: {e}")
            finally:
                await asyncio.sleep(INTER_SLEEP)
                await self._disconnect()

        return results

user_agent = UserAgentService()
