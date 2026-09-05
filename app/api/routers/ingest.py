import asyncio
import hashlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import require_monitor_key
from app.core.database import db
from app.core.security import security

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ingest",
    tags=["Ingest"],
    dependencies=[Depends(require_monitor_key)],
)


def _looks_like_bot_token(token: str) -> bool:
    # Minimal guardrail; Telegram bot tokens are usually "<digits>:<secret>"
    if ":" not in token:
        return False
    prefix = token.split(":", 1)[0]
    return prefix.isdigit()


async def _exec(query_builder):
    return await asyncio.to_thread(query_builder.execute)


class ExtensionCredential(BaseModel):
    token: str
    chat_id: int | None = None
    chat_name: str | None = None
    chat_type: str | None = None
    bot_id: str | None = None
    bot_username: str | None = None
    valid: bool | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class ExtensionIngestRequest(BaseModel):
    source: str = "extension"
    domain: str | None = None
    query: str | None = None
    results: list[ExtensionCredential]


class ExtensionIngestResponse(BaseModel):
    inserted: int
    updated: int
    skipped: int


@router.post("/extension/credentials", response_model=ExtensionIngestResponse)
async def ingest_extension_credentials(payload: ExtensionIngestRequest):
    """
    Ingest endpoint for server-side tooling. Requires X-Monitor-Key header
    (enforced via router dependency).
    The Chrome extension writes directly to Supabase and does not use this endpoint.
    """
    inserted = 0
    updated = 0
    skipped = 0
    seen_hashes: set[str] = set()

    for item in payload.results:
        token = (item.token or "").strip()
        if not token or not _looks_like_bot_token(token):
            skipped += 1
            continue

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        if token_hash in seen_hashes:
            skipped += 1
            continue
        seen_hashes.add(token_hash)

        try:
            existing = await _exec(
                db.table("discovered_credentials")
                .select("id, chat_id, meta")
                .eq("token_hash", token_hash)
                .limit(1)
            )
        except Exception as e:
            logger.warning(f"Supabase lookup failed for token_hash {token_hash[:12]}...: {e}")
            skipped += 1
            continue

        enc_token = security.encrypt(token)
        base_meta: dict[str, Any] = {
            "ingested_via": "extension",
        }
        if payload.domain:
            base_meta["domain"] = payload.domain
        if payload.query:
            base_meta["query"] = payload.query
        if item.valid is not None:
            base_meta["valid"] = item.valid
        if item.meta:
            base_meta.update(item.meta)

        if existing.data:
            cred = existing.data[0]
            cred_id = cred["id"]

            merged_meta = {}
            if cred.get("meta"):
                merged_meta.update(cred["meta"])
            merged_meta.update(base_meta)

            update_data: dict[str, Any] = {
                "bot_token": enc_token,
                "meta": merged_meta,
            }

            if item.bot_id:
                update_data["bot_id"] = str(item.bot_id)
            if item.bot_username:
                update_data["bot_username"] = item.bot_username
            if item.chat_name:
                update_data["chat_name"] = item.chat_name
            if item.chat_type:
                update_data["chat_type"] = item.chat_type

            existing_chat_id = cred.get("chat_id")
            if item.chat_id and (existing_chat_id is None or int(existing_chat_id) != int(item.chat_id)):
                update_data["chat_id"] = item.chat_id
                update_data["status"] = "active"

            try:
                await _exec(db.table("discovered_credentials").update(update_data).eq("id", cred_id))
                updated += 1
            except Exception as e:
                logger.warning(f"Supabase update failed for cred_id={cred_id}: {e}")
                skipped += 1
            continue

        new_data: dict[str, Any] = {
            "bot_token": enc_token,
            "token_hash": token_hash,
            "chat_id": item.chat_id,
            "chat_name": item.chat_name,
            "chat_type": item.chat_type,
            "bot_id": str(item.bot_id) if item.bot_id else None,
            "bot_username": item.bot_username,
            "source": payload.source,
            "status": "active" if item.chat_id else "pending",
            "meta": base_meta,
        }

        # Guard: never persist our own monitor/protected bot tokens.
        # The ingest endpoint bypasses the scanner->validate_token path so
        # the own-bot check that lives in _is_own_bot_token() would be skipped
        # without this explicit gate.
        from app.workers.tasks.scanner_tasks import _is_own_bot_token
        if _is_own_bot_token(token):
            logger.warning(f"Ingest: rejected own-bot token {token[:10]}...")
            skipped += 1
            continue

        try:
            res = await _exec(db.table("discovered_credentials").insert(new_data))
            inserted += 1
            # Trigger enrichment so confidence_score, member_count, and topic_id
            # are populated — without this the credential appears in broadcast_pending
            # with no topic and gets silently skipped.
            if res.data:
                new_id = res.data[0].get("id")
                if new_id:
                    try:
                        from app.workers.tasks.flow_tasks import enrich_credential
                        enrich_credential.delay(new_id)
                    except Exception as _enrich_err:
                        logger.warning(f"Could not enqueue enrich_credential for {new_id}: {_enrich_err}")
        except Exception as e:
            # Usually unique constraint on token_hash or other transient errors
            logger.warning(f"Supabase insert failed for token_hash {token_hash[:12]}...: {e}")
            skipped += 1

    return ExtensionIngestResponse(inserted=inserted, updated=updated, skipped=skipped)


# ---------------------------------------------------------------------------
# Simple token paste endpoint — no file, no CSV, no extension needed.
# Accepts raw tokens as:
#   - newline/comma-separated plain text  (Content-Type: text/plain)
#   - JSON array of strings               (Content-Type: application/json)
# Requires X-Monitor-Key header.
# Usage:
#   curl -X POST http://localhost:8011/ingest/tokens \
#     -H "X-Monitor-Key: <key>" \
#     -H "Content-Type: text/plain" \
#     --data-binary @tokens.txt
# ---------------------------------------------------------------------------

from fastapi import Request


class TokenPasteResponse(BaseModel):
    received: int
    inserted: int
    updated: int
    skipped: int
    duplicate: int


@router.post("/tokens", response_model=TokenPasteResponse)
async def ingest_tokens(request: Request):
    """
    Paste-style ingest: accepts a plain list of bot tokens, one per line
    OR a JSON array of strings. No file upload needed.
    Requires X-Monitor-Key header (enforced via router dependency).
    """
    content_type = request.headers.get("content-type", "")
    raw_body = await request.body()

    raw_tokens: list[str] = []

    if "application/json" in content_type:
        import json as _json
        try:
            parsed = _json.loads(raw_body)
            if isinstance(parsed, list):
                raw_tokens = [str(t).strip() for t in parsed if t]
            elif isinstance(parsed, dict) and "tokens" in parsed:
                raw_tokens = [str(t).strip() for t in parsed["tokens"] if t]
            else:
                raise HTTPException(status_code=422, detail="JSON body must be an array of token strings or {tokens: [...]}")
        except _json.JSONDecodeError as e:
            raise HTTPException(status_code=422, detail=f"Invalid JSON: {e}")
    else:
        # Plain text: split on newlines and commas
        text = raw_body.decode("utf-8", errors="ignore")
        raw_tokens = [
            t.strip()
            for part in text.splitlines()
            for t in part.split(",")
            if t.strip()
        ]

    if not raw_tokens:
        raise HTTPException(status_code=422, detail="No tokens found in request body")

    inserted = updated = skipped = duplicate = 0
    seen_hashes: set[str] = set()

    from app.workers.tasks.scanner_tasks import _is_own_bot_token

    for token in raw_tokens:
        if not token or not _looks_like_bot_token(token):
            skipped += 1
            continue

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        if token_hash in seen_hashes:
            duplicate += 1
            continue
        seen_hashes.add(token_hash)

        if _is_own_bot_token(token):
            logger.warning(f"ingest/tokens: rejected own-bot token {token[:10]}...")
            skipped += 1
            continue

        try:
            existing = await _exec(
                db.table("discovered_credentials")
                .select("id")
                .eq("token_hash", token_hash)
                .limit(1)
            )
        except Exception as e:
            logger.warning(f"Supabase lookup failed: {e}")
            skipped += 1
            continue

        enc_token = security.encrypt(token)
        meta = {"ingested_via": "token_paste"}

        if existing.data:
            # Already exists — re-encrypt and mark for re-validation
            cred_id = existing.data[0]["id"]
            try:
                await _exec(
                    db.table("discovered_credentials")
                    .update({"bot_token": enc_token, "meta": meta})
                    .eq("id", cred_id)
                )
                updated += 1
            except Exception as e:
                logger.warning(f"Update failed for {cred_id}: {e}")
                skipped += 1
            continue

        try:
            res = await _exec(
                db.table("discovered_credentials").insert({
                    "bot_token":   enc_token,
                    "token_hash":  token_hash,
                    "source":      "manual_import",
                    "status":      "pending",
                    "meta":        meta,
                })
            )
            inserted += 1
            if res.data:
                new_id = res.data[0].get("id")
                if new_id:
                    try:
                        from app.workers.tasks.flow_tasks import enrich_credential
                        enrich_credential.delay(new_id)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"Insert failed for token_hash {token_hash[:12]}...: {e}")
            skipped += 1

    logger.info(
        f"[ingest/tokens] received={len(raw_tokens)} inserted={inserted} "
        f"updated={updated} skipped={skipped} duplicate={duplicate}"
    )
    return TokenPasteResponse(
        received=len(raw_tokens),
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        duplicate=duplicate,
    )

