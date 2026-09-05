import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.services._scraper.lifecycle import TelegramClientLifecycle
from app.services._scraper.results import (
    ScrapeReason,
    ScrapeResultClassifier,
    StrategyAttempt,
)

logger = logging.getLogger("scraper")

MessageFormatter = Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]
MonitorCheck = Callable[[Any], bool]
HistoryReader = Callable[[str, int, int], Awaitable[list[dict[str, Any]]]]
IdReader = Callable[[str, int, int, int], Awaitable[list[dict[str, Any]]]]
ForwardReader = Callable[[str, int, int | str, int, int], Awaitable[list[dict[str, Any]]]]


@dataclass(slots=True)
class StrategyReadOutcome:
    messages: list[dict[str, Any]] = field(default_factory=list)
    attempt: StrategyAttempt | None = None
    anchor_id: int = 0
    last_update_id: int | None = None
    terminal: bool = False


@dataclass(slots=True)
class WebhookDecision:
    can_poll: bool
    attempt: StrategyAttempt
    webhook_url: str | None = None


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _reason_for_http_status(status_code: int) -> tuple[ScrapeReason, bool]:
    if status_code == 429:
        return ScrapeReason.FLOOD_WAIT, True
    if status_code in (401, 403):
        return ScrapeReason.FORBIDDEN, False
    if status_code == 400:
        return ScrapeReason.BAD_REQUEST, False
    if status_code == 409:
        return ScrapeReason.WEBHOOK_CONFLICT, False
    if 500 <= status_code <= 599:
        return ScrapeReason.NETWORK_DISCONNECT, True
    return ScrapeReason.BAD_REQUEST, False


class WebhookStateService:
    def __init__(
        self,
        *,
        allow_delete: bool = False,
        classifier: ScrapeResultClassifier | None = None,
    ):
        self.allow_delete = allow_delete
        self.classifier = classifier or ScrapeResultClassifier()

    async def prepare_polling(
        self,
        bot_token: str,
        client: httpx.AsyncClient,
        *,
        strategy: str = "bot_api_updates",
        credential_id: str | None = None,
    ) -> WebhookDecision:
        base_url = f"https://api.telegram.org/bot{bot_token}"
        try:
            response = await client.get(f"{base_url}/getWebhookInfo")
        except Exception as exc:
            return WebhookDecision(
                can_poll=False,
                attempt=self.classifier.classify_exception(exc, strategy=strategy),
            )

        evidence: dict[str, Any] = {"getWebhookInfo_status": response.status_code}
        if response.status_code == 401:
            return WebhookDecision(
                can_poll=False,
                attempt=StrategyAttempt(
                    name=strategy,
                    reason=ScrapeReason.FORBIDDEN,
                    retryable=False,
                    evidence=evidence,
                ),
            )

        if response.status_code != 200:
            reason, retryable = _reason_for_http_status(response.status_code)
            return WebhookDecision(
                can_poll=retryable,
                attempt=StrategyAttempt(
                    name=strategy,
                    reason=reason,
                    retryable=retryable,
                    evidence=evidence,
                ),
            )

        payload = _response_json(response)
        result = payload.get("result") if payload.get("ok") else {}
        result = result if isinstance(result, dict) else {}
        webhook_url = result.get("url") or None
        if not webhook_url:
            return WebhookDecision(
                can_poll=True,
                attempt=StrategyAttempt(
                    name=strategy,
                    success=True,
                    reason=ScrapeReason.NO_NEW_MESSAGES,
                    evidence={**evidence, "webhook_present": False},
                ),
            )

        evidence.update(
            {
                "webhook_present": True,
                "webhook_url": webhook_url,
                "last_error_message": result.get("last_error_message"),
                "pending_update_count": result.get("pending_update_count"),
            }
        )

        # Detect third-party re-takeover of OUR honeypot webhook.
        # If the found webhook_url is NOT ours but we're in honeypot mode
        # with this credential allowlisted, someone overwrote our registration.
        try:
            from app.core.config import settings as _hp_settings
            our_receiver = (_hp_settings.HONEYPOT_WEBHOOK_URL or "").rstrip("/")
            if (
                _hp_settings.HONEYPOT_MODE
                and our_receiver
                and credential_id
                and webhook_url
                and our_receiver not in webhook_url
            ):
                # Third party re-registered over us — log it as a counter-attack
                logger.warning(
                    f"🚨 [Webhook] COUNTER-TAKEOVER detected — third party re-registered "
                    f"webhook ({webhook_url}) over our honeypot for cred {credential_id[:8]}... "
                    f"Will delete + re-register ours."
                )
        except Exception:
            pass

        if not self.allow_delete:
            return WebhookDecision(
                can_poll=False,
                webhook_url=webhook_url,
                attempt=StrategyAttempt(
                    name=strategy,
                    reason=ScrapeReason.WEBHOOK_CONFLICT,
                    retryable=False,
                    evidence={**evidence, "delete_policy": "deny"},
                ),
            )

        # Pin the captured webhook URL to the credential's topic BEFORE deletion
        # so we still have a visible record after wiping the remote registration.
        # Fire-and-forget: never let this block or fail the scrape.
        try:
            from app.workers.celery_app import app as celery_app

            celery_app.send_task(
                "flow.pin_webhook_url",
                kwargs={
                    "credential_id": credential_id,
                    "bot_token": bot_token,
                    "webhook_url": webhook_url,
                    "evidence": {
                        k: v
                        for k, v in evidence.items()
                        if k
                        in (
                            "last_error_message",
                            "pending_update_count",
                            "getWebhookInfo_status",
                        )
                    },
                },
            )
        except Exception as pin_exc:
            logger.debug(f"[Webhook] pin dispatch skipped: {pin_exc}")

        try:
            delete_response = await client.post(f"{base_url}/deleteWebhook")
        except Exception as exc:
            attempt = self.classifier.classify_exception(
                exc,
                strategy=strategy,
                evidence={**evidence, "delete_policy": "allow"},
            )
            return WebhookDecision(can_poll=False, webhook_url=webhook_url, attempt=attempt)

        delete_payload = _response_json(delete_response)
        if delete_response.status_code == 200 and delete_payload.get("ok"):
            # Explicit visibility for successful third-party webhook takeover
            logger.info(
                f"🎯 [Webhook] TAKEOVER — deleted third-party webhook "
                f"({webhook_url}) — resuming polling"
            )
            try:
                from app.core.audit import AuditEvent, AuditLogger

                AuditLogger.log(
                    AuditEvent.WEBHOOK_TAKEOVER,
                    credential_id=credential_id,
                    details={
                        "webhook_url": webhook_url,
                        "strategy": strategy,
                        "pending_updates": evidence.get("pending_update_count"),
                        "last_error": evidence.get("last_error_message"),
                    },
                )
            except Exception as audit_exc:
                logger.debug(f"[Webhook] takeover audit skipped: {audit_exc}")

            # Honeypot mode — after successful takeover, optionally register OUR
            # webhook so we observe what the C2 was expecting. Fully gated on
            # env vars; only active if credential is in allowlist (or blanket
            # mode with empty allowlist). Fire-and-forget; failure never
            # blocks the scrape.
            try:
                from app.core.config import settings as _settings

                if (
                    _settings.HONEYPOT_MODE
                    and _settings.HONEYPOT_WEBHOOK_URL
                    and _settings.HONEYPOT_SECRET
                    and credential_id
                ):
                    _hp_allowlist_raw = (_settings.HONEYPOT_ALLOWLIST or "").strip()
                    _hp_allowed = (
                        _hp_allowlist_raw.upper() == "AUTO"
                        or credential_id in {
                            c.strip()
                            for c in _hp_allowlist_raw.split(",")
                            if c.strip()
                        }
                    ) if _hp_allowlist_raw else False

                    if _hp_allowed:
                        honeypot_url = (
                            f"{_settings.HONEYPOT_WEBHOOK_URL.rstrip('/')}/"
                            f"receive/{credential_id}"
                        )
                        # Retry setWebhook up to 3 times — Telegram can 429 or
                        # transiently fail. Short exponential backoff.
                        set_ok = False
                        for _attempt in range(3):
                            try:
                                set_resp = await client.post(
                                    f"{base_url}/setWebhook",
                                    data={
                                        "url": honeypot_url,
                                        "secret_token": _settings.HONEYPOT_SECRET,
                                        "allowed_updates": '["message","callback_query","edited_message","channel_post","inline_query"]',
                                        "drop_pending_updates": "false",
                                    },
                                )
                                set_ok = (
                                    set_resp.status_code == 200
                                    and (_response_json(set_resp) or {}).get("ok") is True
                                )
                                if set_ok:
                                    break
                                # 429 or transient — wait and retry
                                if set_resp.status_code == 429:
                                    import asyncio as _aio
                                    await _aio.sleep(2 ** (_attempt + 1))
                                else:
                                    break  # non-retryable HTTP error
                            except Exception:
                                import asyncio as _aio
                                await _aio.sleep(2 ** _attempt)

                        if set_ok:
                            logger.info(
                                f"🍯 [Honeypot] setWebhook succeeded for "
                                f"{credential_id[:8]}... → observing"
                            )
                        else:
                            logger.warning(
                                f"🍯 [Honeypot] setWebhook FAILED after 3 attempts "
                                f"for {credential_id[:8]}..."
                            )
            except Exception as hp_exc:
                logger.debug(f"[Honeypot] setWebhook dispatch skipped: {hp_exc}")

            return WebhookDecision(
                can_poll=True,
                webhook_url=webhook_url,
                attempt=StrategyAttempt(
                    name=strategy,
                    success=True,
                    reason=ScrapeReason.NO_NEW_MESSAGES,
                    evidence={
                        **evidence,
                        "delete_policy": "allow",
                        "deleteWebhook_status": delete_response.status_code,
                    },
                ),
            )

        # deleteWebhook returned non-OK — log visibly
        logger.warning(
            f"⚠️ [Webhook] deleteWebhook failed with status "
            f"{delete_response.status_code}: {str(delete_payload)[:200]}"
        )
        return WebhookDecision(
            can_poll=False,
            webhook_url=webhook_url,
            attempt=StrategyAttempt(
                name=strategy,
                reason=ScrapeReason.WEBHOOK_CONFLICT,
                retryable=False,
                evidence={
                    **evidence,
                    "delete_policy": "allow",
                    "deleteWebhook_status": delete_response.status_code,
                    "deleteWebhook_body": delete_payload,
                },
            ),
        )


class BotApiUpdateReader:
    def __init__(
        self,
        *,
        webhook_service: WebhookStateService,
        media_formatter: MessageFormatter,
        is_monitor_bot: Callable[[str], bool],
        is_monitor_group: MonitorCheck,
        classifier: ScrapeResultClassifier | None = None,
    ):
        self.webhook_service = webhook_service
        self.media_formatter = media_formatter
        self.is_monitor_bot = is_monitor_bot
        self.is_monitor_group = is_monitor_group
        self.classifier = classifier or ScrapeResultClassifier()

    async def read(self, bot_token: str, *, limit: int = 100, credential_id: str | None = None) -> StrategyReadOutcome:
        # If credential_id not passed, derive from token_hash (needed for honeypot setWebhook)
        if not credential_id:
            import hashlib
            _token_hash = hashlib.sha256(bot_token.encode()).hexdigest()
            try:
                from app.core.database import db
                _lookup = db.table("discovered_credentials").select("id").eq("token_hash", _token_hash).limit(1).execute()
                if _lookup.data:
                    credential_id = _lookup.data[0]["id"]
            except Exception:
                pass
        self._current_credential_id = credential_id
        strategy = "bot_api_updates"
        if self.is_monitor_bot(bot_token):
            return StrategyReadOutcome(
                attempt=StrategyAttempt(
                    name=strategy,
                    reason=ScrapeReason.NO_ACCESSIBLE_UPDATES,
                    retryable=False,
                    evidence={"skipped": "monitor_bot"},
                ),
                terminal=True,
            )

        base_url = f"https://api.telegram.org/bot{bot_token}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            webhook = await self.webhook_service.prepare_polling(
                bot_token,
                client,
                strategy=strategy,
                credential_id=self._current_credential_id,
            )
            if not webhook.can_poll:
                return StrategyReadOutcome(attempt=webhook.attempt, terminal=True)

            try:
                response = await client.get(f"{base_url}/getUpdates", params={"limit": limit})
            except Exception as exc:
                return StrategyReadOutcome(
                    attempt=self.classifier.classify_exception(exc, strategy=strategy),
                    terminal=False,
                )

        evidence: dict[str, Any] = {"getUpdates_status": response.status_code}
        if response.status_code == 409:
            return StrategyReadOutcome(
                attempt=StrategyAttempt(
                    name=strategy,
                    reason=ScrapeReason.WEBHOOK_CONFLICT,
                    retryable=False,
                    evidence=evidence,
                ),
                terminal=True,
            )

        if response.status_code != 200:
            reason, retryable = _reason_for_http_status(response.status_code)
            return StrategyReadOutcome(
                attempt=StrategyAttempt(
                    name=strategy,
                    reason=reason,
                    retryable=retryable,
                    evidence=evidence,
                ),
                terminal=not retryable,
            )

        data = _response_json(response)
        if not data.get("ok"):
            text = getattr(response, "text", "") or str(data)
            if "webhook" in text.lower():
                reason = ScrapeReason.WEBHOOK_CONFLICT
                retryable = False
            else:
                reason = ScrapeReason.BAD_REQUEST
                retryable = False
            return StrategyReadOutcome(
                attempt=StrategyAttempt(
                    name=strategy,
                    reason=reason,
                    retryable=retryable,
                    evidence={**evidence, "body": data},
                ),
                terminal=True,
            )

        messages: list[dict[str, Any]] = []
        anchor_id = 0
        last_update_id = None
        updates = data.get("result") or []
        for update in updates:
            if isinstance(update, dict):
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    last_update_id = max(last_update_id or update_id, update_id)
            target = (
                update.get("message")
                or update.get("channel_post")
                or update.get("edited_message")
                or update.get("edited_channel_post")
                if isinstance(update, dict)
                else None
            )
            if not isinstance(target, dict):
                continue

            chat = target.get("chat") if isinstance(target.get("chat"), dict) else {}
            if self.is_monitor_group(chat.get("id")):
                continue

            message_id = target.get("message_id")
            if isinstance(message_id, int):
                anchor_id = max(anchor_id, message_id)

            sender = target.get("from") if isinstance(target.get("from"), dict) else {}
            media_type, file_meta = self.media_formatter(target)
            messages.append(
                {
                    "telegram_msg_id": message_id,
                    "sender_name": sender.get("username")
                    or sender.get("first_name")
                    or "Unknown",
                    "sender_user_id": sender.get("id"),
                    "content": target.get("text") or target.get("caption") or "",
                    "media_type": media_type,
                    "file_meta": file_meta,
                    "chat_id": chat.get("id"),
                }
            )

        attempt = StrategyAttempt(
            name=strategy,
            success=True,
            message_count=len(messages),
            reason=ScrapeReason.SUCCESS if messages else ScrapeReason.NO_NEW_MESSAGES,
            evidence={**evidence, "update_count": len(updates), "last_update_id": last_update_id},
        )
        return StrategyReadOutcome(
            messages=messages,
            attempt=attempt,
            anchor_id=anchor_id,
            last_update_id=last_update_id,
            terminal=False,
        )


class UserAgentJoinService:
    def __init__(
        self,
        *,
        classifier: ScrapeResultClassifier | None = None,
    ):
        self.classifier = classifier or ScrapeResultClassifier()

    async def resolve_bot_username(self, bot_token: str) -> tuple[str | None, dict[str, Any]]:
        evidence: dict[str, Any] = {}

        # Cache lookup — bot username/id are stable per token; cache getMe for 1h
        # to skip a full round-trip on every scrape. Token format: '<bot_id>:<hash>'.
        cache_bot_id = bot_token.split(":", 1)[0] if bot_token and ":" in bot_token else None
        if cache_bot_id:
            try:
                from app.core.redis_srv import get_cached_getme

                cached = await get_cached_getme(cache_bot_id)
            except Exception:
                cached = None
            if isinstance(cached, dict) and cached.get("ok"):
                result_obj = cached.get("result") or {}
                username = result_obj.get("username")
                if username:
                    evidence["getMe_status"] = 200
                    evidence["bot_id"] = result_obj.get("id")
                    evidence["cache_hit"] = "getme"
                    return username, evidence

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"https://api.telegram.org/bot{bot_token}/getMe")
        except Exception as exc:
            evidence.update(
                {
                    "exception_type": exc.__class__.__name__,
                    "exception": str(exc)[:300],
                }
            )
            return None, evidence

        evidence["getMe_status"] = response.status_code
        data = _response_json(response)
        if response.status_code == 200 and data.get("ok"):
            username = (data.get("result") or {}).get("username")
            evidence["bot_id"] = (data.get("result") or {}).get("id")
            # Cache successful response for 1h — reduces repeated getMe hits
            # on the same bot across scrape retries and re-scrape loops.
            if cache_bot_id and username:
                try:
                    from app.core.redis_srv import set_cached_getme

                    await set_cached_getme(cache_bot_id, data, ttl=3600)
                except Exception:
                    pass
            return username, evidence
        evidence["getMe_body"] = data
        return None, evidence

    async def invite_discovered_bot(
        self,
        bot_token: str,
        group_id: int | str,
        *,
        chat_type: str | None = None,
    ) -> StrategyAttempt:
        username, evidence = await self.resolve_bot_username(bot_token)
        evidence["target_chat_type"] = chat_type
        if not username:
            return StrategyAttempt(
                name="user_agent_invite",
                reason=ScrapeReason.USER_AGENT_INVITE_FAILED,
                retryable=False,
                evidence={**evidence, "stage": "resolve_bot_username"},
            )

        try:
            from app.core.redis_srv import redis_srv

            if redis_srv.is_on_cooldown("user_agent"):
                ttl = redis_srv.get_cooldown_remaining("user_agent")
                return StrategyAttempt(
                    name="user_agent_invite",
                    reason=ScrapeReason.USER_AGENT_INVITE_FAILED,
                    retryable=True,
                    evidence={**evidence, "bot_username": username, "cooldown_seconds": ttl},
                )
        except Exception as exc:
            evidence["cooldown_check_error"] = str(exc)[:300]

        try:
            from app.services.user_agent_srv import user_agent

            invited = await user_agent.invite_bot_to_group(username, group_id)
        except Exception as exc:
            return self.classifier.classify_exception(
                exc,
                strategy="user_agent_invite",
                evidence={**evidence, "bot_username": username},
            )

        if invited:
            return StrategyAttempt(
                name="user_agent_invite",
                success=True,
                reason=ScrapeReason.SUCCESS,
                evidence={**evidence, "bot_username": username},
            )

        return StrategyAttempt(
            name="user_agent_invite",
            reason=ScrapeReason.USER_AGENT_INVITE_FAILED,
            retryable=False,
            evidence={**evidence, "bot_username": username},
        )


class BotPreflightService:
    def __init__(
        self,
        *,
        is_monitor_bot: Callable[[str], bool],
        join_service: UserAgentJoinService,
        classifier: ScrapeResultClassifier | None = None,
    ):
        self.is_monitor_bot = is_monitor_bot
        self.join_service = join_service
        self.classifier = classifier or ScrapeResultClassifier()

    async def ensure_bot_in_chat(self, bot_token: str, chat_id: int | str) -> StrategyAttempt:
        strategy = "bot_preflight"
        if self.is_monitor_bot(bot_token):
            return StrategyAttempt(
                name=strategy,
                success=True,
                reason=ScrapeReason.SUCCESS,
                evidence={"skipped": "monitor_bot"},
            )

        base_url = f"https://api.telegram.org/bot{bot_token}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{base_url}/getChat", params={"chat_id": chat_id})
        except Exception as exc:
            return self.classifier.classify_exception(exc, strategy=strategy)

        evidence: dict[str, Any] = {"getChat_status": response.status_code, "chat_id": chat_id}
        data = _response_json(response)
        if response.status_code == 200 and data.get("ok"):
            chat = data.get("result") if isinstance(data.get("result"), dict) else {}
            return StrategyAttempt(
                name=strategy,
                success=True,
                reason=ScrapeReason.SUCCESS,
                evidence={
                    **evidence,
                    "chat_type": chat.get("type"),
                    "chat_title": chat.get("title") or chat.get("username"),
                },
            )

        body_text = str(data).lower()
        if "too many bots" in body_text or "bots in this chat" in body_text:
            return StrategyAttempt(
                name=strategy,
                reason=ScrapeReason.TOO_MANY_BOTS,
                retryable=False,
                evidence={**evidence, "body": data},
            )

        if response.status_code in (400, 401, 403):
            invite_attempt = await self.join_service.invite_discovered_bot(
                bot_token,
                chat_id,
                chat_type=(data.get("result") or {}).get("type")
                if isinstance(data.get("result"), dict)
                else None,
            )
            invite_attempt.evidence.setdefault("getChat_status", response.status_code)
            return invite_attempt

        reason, retryable = _reason_for_http_status(response.status_code)
        return StrategyAttempt(
            name=strategy,
            reason=reason,
            retryable=retryable,
            evidence={**evidence, "body": data},
        )


class TelethonHistoryReader:
    def __init__(
        self,
        read_func: HistoryReader,
        *,
        classifier: ScrapeResultClassifier | None = None,
        timeout: float | None = None,
    ):
        self.read_func = read_func
        self.classifier = classifier or ScrapeResultClassifier()
        self.timeout = timeout

    async def read(self, bot_token: str, chat_id: int, limit: int) -> StrategyReadOutcome:
        strategy = "telethon_history"
        lifecycle = TelegramClientLifecycle(timeout=self.timeout, label=strategy, logger=logger)
        try:
            messages = await lifecycle.run(
                lambda: self.read_func(bot_token, chat_id, limit),
                timeout=self.timeout,
                label=strategy,
            )
        except Exception as exc:
            return StrategyReadOutcome(
                attempt=self.classifier.classify_exception(exc, strategy=strategy),
                terminal=False,
            )

        return StrategyReadOutcome(
            messages=messages or [],
            attempt=StrategyAttempt(
                name=strategy,
                success=True,
                message_count=len(messages or []),
                reason=ScrapeReason.SUCCESS if messages else ScrapeReason.NO_NEW_MESSAGES,
            ),
        )


class MessageIdReader:
    def __init__(
        self,
        read_func: IdReader,
        *,
        classifier: ScrapeResultClassifier | None = None,
        timeout: float | None = None,
    ):
        self.read_func = read_func
        self.classifier = classifier or ScrapeResultClassifier()
        self.timeout = timeout

    async def read(self, bot_token: str, chat_id: int, anchor_id: int, limit: int) -> StrategyReadOutcome:
        strategy = "message_id_reader"
        lifecycle = TelegramClientLifecycle(timeout=self.timeout, label=strategy, logger=logger)
        try:
            messages = await lifecycle.run(
                lambda: self.read_func(bot_token, chat_id, anchor_id, limit),
                timeout=self.timeout,
                label=strategy,
            )
        except Exception as exc:
            return StrategyReadOutcome(
                attempt=self.classifier.classify_exception(exc, strategy=strategy),
                terminal=False,
            )

        return StrategyReadOutcome(
            messages=messages or [],
            attempt=StrategyAttempt(
                name=strategy,
                success=True,
                message_count=len(messages or []),
                reason=ScrapeReason.SUCCESS if messages else ScrapeReason.NO_NEW_MESSAGES,
                evidence={"anchor_id": anchor_id},
            ),
        )


class ForwardingArchiveReader:
    def __init__(
        self,
        read_func: ForwardReader,
        *,
        join_service: UserAgentJoinService,
        classifier: ScrapeResultClassifier | None = None,
    ):
        self.read_func = read_func
        self.join_service = join_service
        self.classifier = classifier or ScrapeResultClassifier()

    async def read(
        self,
        bot_token: str,
        from_chat_id: int,
        *,
        anchor_id: int,
        limit: int = 20,
    ) -> StrategyReadOutcome:
        strategy = "forwarding_archive"
        dest_chat_id = settings.MONITOR_GROUP_ID
        if not dest_chat_id:
            return StrategyReadOutcome(
                attempt=StrategyAttempt(
                    name=strategy,
                    reason=ScrapeReason.BAD_REQUEST,
                    retryable=False,
                    evidence={"missing": "MONITOR_GROUP_ID"},
                ),
                terminal=True,
            )

        victim_username: str | None = None
        cleanup_key: str | None = None
        try:
            victim_username, evidence = await self.join_service.resolve_bot_username(bot_token)
            if not victim_username:
                return StrategyReadOutcome(
                    attempt=StrategyAttempt(
                        name=strategy,
                        reason=ScrapeReason.USER_AGENT_INVITE_FAILED,
                        retryable=False,
                        evidence={**evidence, "stage": "resolve_bot_username"},
                    ),
                    terminal=True,
                )

            from app.services.user_agent_srv import user_agent

            whitelist = [x.strip() for x in settings.WHITELISTED_BOT_IDS.split(",") if x.strip()]
            if whitelist:
                await user_agent.cleanup_bots(dest_chat_id, whitelist)

            invite_attempt = await self.join_service.invite_discovered_bot(bot_token, dest_chat_id)
            if not invite_attempt.success:
                invite_attempt.name = strategy
                return StrategyReadOutcome(attempt=invite_attempt, terminal=True)

            try:
                import redis as redis_mod

                redis_client = redis_mod.from_url(settings.REDIS_URL, decode_responses=True)
                cleanup_key = f"matkap:pending_cleanup:{victim_username}:{dest_chat_id}"
                redis_client.setex(cleanup_key, 3600, "1")
            except Exception as redis_exc:
                logger.warning("    [Scraper] Could not set Matkap cleanup key: %s", redis_exc)

            lifecycle = TelegramClientLifecycle(timeout=None, label=strategy, logger=logger)
            messages = await lifecycle.run(
                lambda: self.read_func(bot_token, from_chat_id, dest_chat_id, anchor_id, limit),
                label=strategy,
            )
            return StrategyReadOutcome(
                messages=messages or [],
                attempt=StrategyAttempt(
                    name=strategy,
                    success=True,
                    message_count=len(messages or []),
                    reason=ScrapeReason.SUCCESS if messages else ScrapeReason.NO_NEW_MESSAGES,
                    evidence={"anchor_id": anchor_id, "destination_chat_id": dest_chat_id},
                ),
            )
        except Exception as exc:
            return StrategyReadOutcome(
                attempt=self.classifier.classify_exception(exc, strategy=strategy),
                terminal=False,
            )
        finally:
            if victim_username:
                try:
                    from app.services.user_agent_srv import user_agent

                    await user_agent.kick_bot_from_group(victim_username, dest_chat_id)
                except Exception as kick_exc:
                    logger.warning(
                        "    [Scraper] Post-forwarding kick failed for %s: %s",
                        victim_username,
                        kick_exc,
                    )
            if cleanup_key:
                try:
                    import redis as redis_mod

                    redis_client = redis_mod.from_url(settings.REDIS_URL, decode_responses=True)
                    redis_client.delete(cleanup_key)
                except Exception:
                    pass


def unique_append(
    target: list[dict[str, Any]],
    seen_ids: set[int],
    messages: list[dict[str, Any]],
    *,
    chat_id: int | None = None,
) -> int:
    added = 0
    for message in messages:
        message_id = message.get("telegram_msg_id")
        if message_id in seen_ids:
            continue
        if chat_id is not None and str(message.get("chat_id")) != str(chat_id):
            continue
        target.append(message)
        if isinstance(message_id, int):
            seen_ids.add(message_id)
        added += 1
    return added


async def sleep_for_flood_wait(seconds: int | float) -> None:
    await asyncio.sleep(max(0, float(seconds)))


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
