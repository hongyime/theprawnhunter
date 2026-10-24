"""Honeypot mode — receive incoming webhook POSTs from Telegram that were
originally destined for third-party C2 endpoints we took over.

Flow:
    1. Bot X's webhook is registered to https://malicious.example.com/hook
    2. flow.exfiltrate_chat detects webhook conflict + deletes it (existing)
    3. If HONEYPOT_MODE=True and credential opts in, we register our OWN
       webhook to HONEYPOT_WEBHOOK_URL/receive/{credential_id}, authenticated
       with Telegram's X-Telegram-Bot-Api-Secret-Token header
    4. Telegram now sends all messages meant for the third party to us
    5. This router receives them, validates secret header & payload, dedupes update_id,
       stores in honeypot_updates, forwards raw messages to exfiltrated_messages
       (visible in the webapp), and triggers broadcast to the credential's forum topic.

Safety & Resilience:
    - Header-based secret token (HONEYPOT_SECRET) filters random noise
    - Per-credential opt-in via HONEYPOT_ALLOWLIST env var (or 'AUTO')
    - Idempotent update_id deduplication avoids duplicate records on Telegram retry
    - Returns HTTP 500 on database failures so Telegram retries delivery (no silent loss)
    - Only activated after taking over the webhook — never intercepts legitimate operators
"""

import asyncio as _asyncio
import json as _json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.auth import require_monitor_key
from app.core.config import settings
from app.core.database import db
from app.core.logger import get_logger
from app.core.webhook import dispatch_alert as _dispatch_alert
from app.infrastructure.database import execute_async

logger = get_logger(__name__)


def _json_loads(raw: bytes) -> dict:
    return _json.loads(raw)


router = APIRouter(prefix="/honeypot", tags=["Honeypot"])


def _honeypot_credential_allowed(credential_id: str) -> bool:
    """Check per-credential opt-in.

    Modes:
      - HONEYPOT_ALLOWLIST='AUTO' → all active webhook-registered credentials
        are automatically honeypotted on takeover. Grows with the DB.
      - HONEYPOT_ALLOWLIST='uuid1,uuid2,...' → explicit subset only.
      - HONEYPOT_ALLOWLIST='' (empty) → default-deny, nothing honeypotted.
    """
    raw = (settings.HONEYPOT_ALLOWLIST or "").strip()
    if not raw:
        return False  # default-deny
    if raw.upper() == "AUTO":
        return True  # auto-opt-in for any active credential
    allowed = {c.strip() for c in raw.split(",") if c.strip()}
    return credential_id in allowed


@router.post("/receive/{credential_id}")
async def receive_webhook_update(credential_id: str, request: Request):
    """Receive a Telegram bot webhook POST.

    Authentication: Telegram Bot API 6.7+ supports a `secret_token` header
    (X-Telegram-Bot-Api-Secret-Token) set during setWebhook. This is preferred
    over path-based secrets because setWebhook URLs can be read back by anyone
    with the bot token via getWebhookInfo — leaking a path-embedded secret.

    Legacy path-secret URLs (/receive/{secret}/{credential_id}) are
    intentionally unsupported because getWebhookInfo exposes the full URL.

    Response is always 200 OK — Telegram retries non-2xx aggressively.
    """
    # Fail-closed if honeypot mode isn't enabled
    if not settings.HONEYPOT_MODE:
        raise HTTPException(status_code=404, detail="honeypot mode disabled")

    if not settings.HONEYPOT_SECRET:
        logger.warning("[Honeypot] receiver called but no secret configured")
        return {"ok": True}

    # Prefer header-based secret (Telegram Bot API 6.7+)
    hdr_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if hdr_secret != settings.HONEYPOT_SECRET:
        logger.warning("[Honeypot] missing/wrong X-Telegram-Bot-Api-Secret-Token")
        return {"ok": True}

    if not _honeypot_credential_allowed(credential_id):
        logger.info(f"[Honeypot] credential {credential_id[:8]}... not allowlisted — dropping")
        return {"ok": True}

    # Optional: verify credential exists and is active — declines payloads for
    # revoked/inactive credentials so we don't accumulate garbage in DB.
    try:
        cred = await execute_async(
            db.table("discovered_credentials")
            .select("id, status")
            .eq("id", credential_id)
            .limit(1)
        )
        if not cred.data or (cred.data[0].get("status") not in ("active",)):
            logger.info(f"[Honeypot] credential {credential_id[:8]}... not active — dropping")
            return {"ok": True}
    except Exception as e:
        logger.debug(f"[Honeypot] active-check skipped: {e}")

    try:
        # SEC-005: bound the body read. Telegram updates are typically < 4 KB
        # (largest legit case: a photo caption + rich metadata). 1 MB is a
        # generous upper bound; anything larger is malicious traffic. We
        # return HTTP 200 so Telegram doesn't retry — logged silently.
        raw_body = await _asyncio.wait_for(request.body(), timeout=5.0)
        if len(raw_body) > 1_048_576:
            logger.warning(
                f"[Honeypot] oversized payload dropped: {len(raw_body)} bytes "
                f"from cred={credential_id[:8]}..."
            )
            return {"ok": True}
        payload = _json_loads(raw_body)
    except TimeoutError:
        logger.warning("[Honeypot] body read timed out")
        return {"ok": True}
    except Exception as e:
        logger.warning(f"[Honeypot] non-JSON body: {e}")
        return {"ok": True}

    # Structural sanity: Telegram webhook updates must be JSON objects with a strictly positive integer update_id
    if not isinstance(payload, dict) or "update_id" not in payload:
        logger.warning("[Honeypot] payload missing update_id — dropping")
        return {"ok": True}

    raw_update_id = payload.get("update_id")
    # Strict validation: reject bool (subclass of int), floats, strings, None, <= 0, and > 2^31 - 1
    MAX_TELEGRAM_UPDATE_ID = 2_147_483_647
    if (
        raw_update_id is None
        or isinstance(raw_update_id, bool)
        or not isinstance(raw_update_id, int)
        or raw_update_id <= 0
        or raw_update_id > MAX_TELEGRAM_UPDATE_ID
    ):
        logger.warning(f"[Honeypot] payload update_id={raw_update_id!r} not a valid 32-bit positive integer — dropping")
        return {"ok": True}

    update_id = raw_update_id
    now = datetime.now(UTC)

    try:
        await execute_async(
            db.table("honeypot_updates").insert(
                {
                    "credential_id": credential_id,
                    "update_id": update_id,
                    "update_type": _classify_update(payload),
                    "payload": payload,
                    "received_at": now.isoformat(),
                    "source_ip": request.client.host if request.client else None,
                }
            )
        )
    except Exception as e:
        err_msg = str(e).lower()
        if "idx_honeypot_cred_update_id" in err_msg or "duplicate key" in err_msg or "23505" in err_msg:
            logger.info(
                f"[Honeypot] duplicate update_id={update_id} for {credential_id[:8]}... — ignoring insert"
            )
            # Ensure forwarding was completed even if this is a retried webhook delivery
            from app.services.honeypot_srv import forward_honeypot_message

            try:
                await forward_honeypot_message(payload, credential_id)
            except Exception as fwd_err:
                logger.error(f"[Honeypot] retry forward error: {fwd_err}")
                raise HTTPException(status_code=500, detail="Retry message forwarding failed") from fwd_err
            return {"ok": True}

        logger.error(f"[Honeypot] insert failed for {credential_id[:8]}...: {e}")
        # Signal failure to Telegram so it retries delivery per Bot API docs
        raise HTTPException(status_code=500, detail="Database persistence failed")

    try:
        from app.services.membership_srv import process_membership_update

        await process_membership_update(payload, credential_id)
    except Exception as _membership_err:
        logger.debug(f"[Honeypot] membership processing skipped: {_membership_err}")

    # Raw message forwarding to exfiltrated_messages (feeds webapp) and topic broadcast.
    # Awaiting here ensures that DB errors raise HTTP 500 to trigger Telegram redelivery.
    from app.services.honeypot_srv import forward_honeypot_message

    try:
        await forward_honeypot_message(payload, credential_id)
    except Exception as fwd_exc:
        logger.error(f"[Honeypot] forwarding message failed for {credential_id[:8]}...: {fwd_exc}")
        raise HTTPException(status_code=500, detail="Message forwarding failed") from fwd_exc

    _asyncio.create_task(_dispatch_alert({
        "event": "honeypot_update",
        "timestamp": now.isoformat(),
        "credential_id": str(credential_id),
        "honeypot_update": payload,
    }))

    logger.info(
        f"🍯 [Honeypot] captured update for {credential_id[:8]}... "
        f"type={_classify_update(payload)}"
    )

    return {"ok": True}


# Legacy path-secret URLs do not match a route and return 404. Re-register any
# remaining old callback through the normal webhook reset flow, which uses the
# current credential-only path and Telegram's secret-token header.


@router.get("/status", dependencies=[Depends(require_monitor_key)])
async def honeypot_status():
    """Report honeypot configuration state.

    Auth: X-Monitor-Key header required (constant-time compare via
    `require_monitor_key`). Trimmed response — does NOT leak allowlist size or
    secret configuration flag, which would fingerprint the deployment if the
    monitor key ever leaked.
    """
    raw_allowlist = (settings.HONEYPOT_ALLOWLIST or "").strip()
    return {
        "mode_enabled": settings.HONEYPOT_MODE,
        "receiver_url_configured": bool(settings.HONEYPOT_WEBHOOK_URL),
        "allowlist_configured": bool(raw_allowlist),
    }


def _classify_update(payload: dict) -> str:
    """Return a short type label for the update — helps monitoring dashboards."""
    if not isinstance(payload, dict):
        return "unknown"
    if "message" in payload:
        return "message"
    if "callback_query" in payload:
        return "callback_query"
    if "inline_query" in payload:
        return "inline_query"
    if "edited_message" in payload:
        return "edited_message"
    if "channel_post" in payload:
        return "channel_post"
    if "edited_channel_post" in payload:
        return "edited_channel_post"
    return "other"
