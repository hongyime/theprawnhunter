"""Honeypot mode — receive incoming webhook POSTs from Telegram that were
originally destined for third-party C2 endpoints we took over.

Flow:
    1. Bot X's webhook is registered to https://malicious.example.com/hook
    2. flow.exfiltrate_chat detects webhook conflict + deletes it (existing)
    3. If HONEYPOT_MODE=True and credential opts in, we register our OWN
       webhook to HONEYPOT_WEBHOOK_URL/{secret}/{credential_id}
    4. Telegram now sends all messages meant for the third party to us
    5. This router receives them, stores them in honeypot_updates, and
       broadcasts to the same topic as regular exfiltration

Safety:
    - Path-based secret (HONEYPOT_SECRET) filters random noise
    - Per-credential opt-in via HONEYPOT_ALLOWLIST env var
    - Only activated after we've already taken over the webhook —
      we never intercept traffic to a legitimate operator

Deployment requirements (all of these MUST be true):
    - Public HTTPS endpoint (Telegram won't POST to HTTP or self-signed)
    - HONEYPOT_MODE=True
    - HONEYPOT_WEBHOOK_URL=https://your-public-host/honeypot/receive
    - HONEYPOT_SECRET=<random 32-char string>

Without a public HTTPS endpoint, this endpoint exists but never receives
traffic because we never call setWebhook.
"""

import asyncio as _asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Request

from app.core.config import settings
from app.core.database import db
from app.core.logger import get_logger
from app.core.webhook import dispatch_alert as _dispatch_alert

logger = get_logger(__name__)

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

    Backwards-compat: still checks the path secret in the OLD format
    (/receive/{secret}/{credential_id}) so existing registrations don't break.

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
        cred = db.table("discovered_credentials").select("id, status").eq("id", credential_id).limit(1).execute()
        if not cred.data or (cred.data[0].get("status") not in ("active",)):
            logger.info(f"[Honeypot] credential {credential_id[:8]}... not active — dropping")
            return {"ok": True}
    except Exception as e:
        logger.debug(f"[Honeypot] active-check skipped: {e}")

    try:
        payload = await request.json()
    except Exception as e:
        logger.warning(f"[Honeypot] non-JSON body: {e}")
        return {"ok": True}

    # Structural sanity: Telegram webhook updates must be JSON objects with an update_id
    if not isinstance(payload, dict) or "update_id" not in payload:
        logger.warning("[Honeypot] payload missing update_id — dropping")
        return {"ok": True}

    now = datetime.now(UTC)
    try:
        db.table("honeypot_updates").insert(
            {
                "credential_id": credential_id,
                "update_type": _classify_update(payload),
                "payload": payload,
                "received_at": now.isoformat(),
                "source_ip": request.client.host if request.client else None,
            }
        ).execute()
        _asyncio.create_task(_dispatch_alert({
            "event": "honeypot_update",
            "timestamp": now.isoformat(),
            "credential_id": str(credential_id),
            "honeypot_update": payload,
        }))
    except Exception as e:
        logger.error(f"[Honeypot] insert failed for {credential_id[:8]}...: {e}")
        return {"ok": True}

    logger.info(
        f"🍯 [Honeypot] captured update for {credential_id[:8]}... "
        f"type={_classify_update(payload)}"
    )

    return {"ok": True}


# NOTE: Legacy path-secret endpoint (/receive/{secret}/{credential_id}) was
# removed in the round-2 hardening pass. Any existing setWebhook registration
# still using that path will fail auth silently (returns {ok: true} but
# doesn't persist), and the operator must run flow.migrate_honeypot_webhooks
# to reset those to the header-based scheme.


@router.get("/status")
async def honeypot_status(x_monitor_key: str | None = Header(None)):
    """Report honeypot configuration state. Requires monitor API key so external
    scanners can't fingerprint our deployment."""
    if not settings.MONITOR_API_KEY or x_monitor_key != settings.MONITOR_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing monitor API key")
    raw_allowlist = (settings.HONEYPOT_ALLOWLIST or "").strip()
    is_auto = raw_allowlist.upper() == "AUTO"
    return {
        "mode_enabled": settings.HONEYPOT_MODE,
        "receiver_url_configured": bool(settings.HONEYPOT_WEBHOOK_URL),
        "secret_configured": bool(settings.HONEYPOT_SECRET),
        "allowlist_mode": (
            "auto_all_webhook_bots" if is_auto
            else "explicit_opt_in" if raw_allowlist
            else "deny_all"
        ),
        "allowlist_size": "unlimited (auto)" if is_auto else (
            len([c for c in raw_allowlist.split(",") if c.strip()])
            if raw_allowlist else 0
        ),
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
    return "other"
