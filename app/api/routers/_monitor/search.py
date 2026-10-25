"""Monitor search resource routes."""

import os
from datetime import timedelta

import httpx
from pydantic import BaseModel

from app.services.embed_api import DIMENSIONS as _EMBED_DIMENSIONS

from .common import *

router = APIRouter()

# Queries at or above this length use the FTS RPC; shorter ones keep using the
# legacy ILIKE path (single/short tokens are noise for websearch_to_tsquery).
_FTS_MIN_QUERY_LEN = 3

# /monitor/search used to cap at 500; the committed search_evidence RPC clamps
# to 200, so the endpoint uses the same bound on both paths.
_SEARCH_LIMIT_MAX = 200

_SEARCH_SELECT = (
    "id, credential_id, telegram_msg_id, sender_name, content, "
    "media_type, is_broadcasted, created_at, broadcasted_at"
)

# Warn once per process instead of on every request when the FTS RPC is not
# deployed yet (migration pending) or fails transiently.
_fts_fallback_warned = False


def _warn_fts_fallback(exc: Exception) -> None:
    global _fts_fallback_warned
    if _fts_fallback_warned:
        return
    _fts_fallback_warned = True
    logger.warning(
        "monitor/search FTS RPC unavailable (%s); falling back to ILIKE", exc
    )


async def _fts_search(
    query: str,
    credential_id: UUID | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Run the operator-gated search_evidence RPC and return raw hit rows."""
    params = {
        "q": query,
        "credential_id": str(credential_id) if credential_id else None,
        "lim": limit,
    }
    res = await execute_async(db.rpc("search_evidence", params))
    rows = res.data
    if not isinstance(rows, list):
        # A malformed payload is not something we can serve; treat it like an
        # RPC failure and let the caller fall back to ILIKE.
        raise RuntimeError("search_evidence returned an unexpected payload")
    return [row for row in rows if isinstance(row, dict)]


async def _ilike_search(
    query: str,
    limit: int,
    credential_id: UUID | None,
    media_only: bool,
    since_hours: int | None,
) -> list[dict[str, Any]]:
    """Legacy case-insensitive substring search (preserved ordering/paging)."""
    q_pattern = f"%{query}%"
    builder = (
        db.table("exfiltrated_messages")
        .select(_SEARCH_SELECT)
        .or_(f"content.ilike.{q_pattern},sender_name.ilike.{q_pattern}")
        .order("created_at", desc=True)
        .limit(limit)
    )

    if credential_id:
        builder = builder.eq("credential_id", str(credential_id))

    if media_only:
        builder = builder.neq("media_type", "text")

    if since_hours and since_hours > 0:
        since = (datetime.now(UTC) - timedelta(hours=since_hours)).isoformat()
        builder = builder.gte("created_at", since)

    res = await execute_async(builder)
    return list(res.data or [])


def _apply_fts_row_filters(
    rows: list[dict[str, Any]],
    media_only: bool,
    since_hours: int | None,
) -> list[dict[str, Any]]:
    """Apply the legacy-only filters to FTS rows the RPC does not accept."""
    filtered = rows
    if media_only:
        filtered = [
            row
            for row in filtered
            if str(row.get("media_type") or "") not in {"", "text"}
        ]

    if since_hours and since_hours > 0:
        cutoff = datetime.now(UTC) - timedelta(hours=since_hours)
        recent: list[dict[str, Any]] = []
        for row in filtered:
            parsed = _parse_created_at(row.get("created_at"))
            if parsed is not None and parsed >= cutoff:
                recent.append(row)
        filtered = recent

    return filtered


def _parse_created_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@router.get("/search", response_model=SearchOut)
async def search_messages(
    q: str,
    limit: int = 50,
    media_only: bool = False,
    since_hours: int | None = None,
    credential_id: UUID | None = None,
):
    """Search exfiltrated evidence.

    Uses the ``search_evidence`` FTS RPC (highlighted snippets) when the query
    is at least 3 characters; falls back to the legacy case-insensitive ILIKE
    over content/sender_name for short queries or when the RPC is unavailable.

    Params:
    - q: search term
    - limit: max results (default 50, clamped to 1..200)
    - media_only: if True, only rows with media_type != 'text'
    - since_hours: filter to messages created in last N hours
    - credential_id: only rows for the given credential
    """
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="query must be at least 2 chars")

    # Whitelist: only allow alphanumeric + space + safe punctuation. This
    # blocks PostgREST metacharacters that could smuggle extra .or_() clauses
    # into the filter (e.g. ',is.null),content.eq.X('). Wildcards and quotes
    # explicitly rejected.
    import re as _re

    if not _re.fullmatch(r"[A-Za-z0-9 _.@\-]{2,80}", query):
        raise HTTPException(
            status_code=400,
            detail="query must match [A-Za-z0-9 _.@-]{2,80} — no wildcards, punctuation, or spaces at limits",
        )

    limit = min(max(limit, 1), _SEARCH_LIMIT_MAX)

    matches: list[dict[str, Any]] | None = None
    if len(query) >= _FTS_MIN_QUERY_LEN:
        try:
            matches = await _fts_search(query, credential_id, limit)
            matches = _apply_fts_row_filters(matches, media_only, since_hours)
        except Exception as exc:  # RPC missing, permission, transport, payload
            _warn_fts_fallback(exc)
            matches = None

    if matches is None:
        try:
            matches = await _ilike_search(
                query, limit, credential_id, media_only, since_hours
            )
        except Exception as exc:
            logger.exception("monitor/search query failed")
            raise HTTPException(status_code=500, detail="Internal error") from exc

    return {
        "query": q,
        "match_count": len(matches),
        "limit": limit,
        "matches": matches,
    }


# Semantic search degrades instead of erroring: any embed-api problem returns
# HTTP 200 with fallback=true so the dashboard can rerun the FTS RPC.

_SEMANTIC_LIMIT_MAX = 100

_EMBED_DEFAULT_URL = "http://127.0.0.1:8020"

_EMBED_TIMEOUT_SECONDS = 30.0


class SemanticSearchIn(BaseModel):
    """Body of POST /monitor/semantic-search."""

    q: str
    credential_id: UUID | None = None
    limit: int = 25


class SemanticUnavailableError(RuntimeError):
    """The embed service could not turn the query into a 384-d vector."""


def _embed_base_url() -> str:
    """Resolve the embed-api base URL (env override, then the Compose default).

    Setting the variable to an empty/whitespace value disables the path without
    attempting any network call.
    """
    raw = os.getenv("EMBED_API_URL")
    if raw is None:
        return _EMBED_DEFAULT_URL
    configured = raw.strip()
    if not configured:
        raise SemanticUnavailableError("embed_api_url_unset")
    return configured.rstrip("/")


async def _embed_query(text: str) -> list[float]:
    """Embed one query string via ``POST {EMBED_API_URL}/embed``.

    Raises :class:`SemanticUnavailableError` for every failure (connection, timeout,
    non-200, malformed body, wrong dimensionality) so the route can degrade.
    """
    endpoint = f"{_embed_base_url()}/embed"
    try:
        async with httpx.AsyncClient(timeout=_EMBED_TIMEOUT_SECONDS) as client:
            response = await client.post(endpoint, json={"texts": [text]})
    except Exception as exc:
        raise SemanticUnavailableError("embed_unreachable") from exc

    if response.status_code != 200:
        raise SemanticUnavailableError(f"embed_http_{response.status_code}")

    try:
        payload = response.json()
        vector = payload["vectors"][0]
        embedding = [float(value) for value in vector]
    except Exception as exc:
        raise SemanticUnavailableError("embed_malformed_response") from exc

    if len(embedding) != _EMBED_DIMENSIONS:
        raise SemanticUnavailableError("embed_wrong_dimensions")
    return embedding


@router.post("/semantic-search", dependencies=[Depends(require_monitor_key)])
async def semantic_search(body: SemanticSearchIn):
    """Embed the query locally, then run the operator-gated ``semantic_search``.

    Returns ``{results: [{message_id, credential_id, distance}], fallback,
    reason}``. If the embed service fails for any reason (including an
    unreachable host or a non-200), the response is still HTTP 200 with
    ``fallback: true`` and no results, telling the client to use FTS.
    """
    query = body.q.strip()
    limit = min(max(body.limit, 1), _SEMANTIC_LIMIT_MAX)

    if not query:
        return {"results": [], "fallback": True, "reason": "empty_query"}

    try:
        embedding = await _embed_query(query)
    except SemanticUnavailableError as exc:
        logger.warning("monitor/semantic-search embedding unavailable (%s)", exc)
        return {"results": [], "fallback": True, "reason": str(exc)}

    params = {
        "query_embedding": embedding,
        "credential_id": str(body.credential_id) if body.credential_id else None,
        "lim": limit,
    }
    try:
        res = await execute_async(db.rpc("semantic_search", params))
    except Exception as exc:
        logger.warning("monitor/semantic-search RPC unavailable (%s)", exc)
        return {"results": [], "fallback": True, "reason": "rpc_unavailable"}

    rows = res.data if isinstance(res.data, list) else []
    results = [
        {
            "message_id": row.get("message_id"),
            "credential_id": row.get("credential_id"),
            "distance": row.get("distance"),
        }
        for row in rows
        if isinstance(row, dict)
    ]
    return {"results": results, "fallback": False, "reason": None}
