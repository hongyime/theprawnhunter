"""User-agent session lifecycle operations."""

from .common import *


class UserAgentSessionUnavailableError(RuntimeError):
    """Raised when an operation needs a user session but none is usable."""


class UserAgentSessionLifecycle:
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
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")
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
        if (
            self._all_on_cooldown_warned_at is not None
            and (now - self._all_on_cooldown_warned_at).total_seconds() < 60
        ):
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
            TEMP_SESSION_PATH = f"/tmp/{session_name}"  # noqa: N806 — function-scoped constant # Unique tmp path per session

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
        if not filename or not filename.startswith("/tmp/"):
            return
        try:
            if os.path.exists(filename):
                os.remove(filename)
            if os.path.exists(filename + "-wal"):
                os.remove(filename + "-wal")
            if os.path.exists(filename + "-shm"):
                os.remove(filename + "-shm")
        except OSError as e:
            logger.warning(f"    ⚠️ [UserAgent] Failed to cleanup {filename}: {e}")

    async def _connect_to_session(self, session_path: str) -> bool:
        """Internal helper to connect to a specific session file."""
        session_name = os.path.splitext(os.path.basename(session_path))[0]
        import shutil
        import sqlite3
        TEMP_SESSION_PATH = f"/tmp/setup_{session_name}"  # noqa: N806 — function-scoped constant
        try:
            if os.path.exists(session_path):
                shutil.copy2(session_path, f"{TEMP_SESSION_PATH}.session")
            conn = sqlite3.connect(f"{TEMP_SESSION_PATH}.session")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.close()
            self.client = TelegramClient(TEMP_SESSION_PATH, self.api_id, self.api_hash)
            await self.client.connect()
            return await self.client.is_user_authorized()
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")
            return False
