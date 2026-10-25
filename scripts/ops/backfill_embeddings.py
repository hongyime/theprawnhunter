"""Backfill pgvector embeddings for stored messages.

Examples:
    python scripts/ops/backfill_embeddings.py                 # dry run (default)
    python scripts/ops/backfill_embeddings.py --apply         # write vectors
    python scripts/ops/backfill_embeddings.py --apply --limit 500

Selection predicate (freshness-aware)
-------------------------------------
A message is selected when either:

1. it has no row in ``message_embeddings``; or
2. its ``file_meta`` contributes ``transcript_text``/``ocr_text`` and the
   stored vector is *not* marked media-aware.

The marker lives in ``message_embeddings.model``: vectors built from the full
media document are stored under :data:`MEDIA_AWARE_MODEL_NAME`
(``"<model>+media"``); content-only vectors keep the plain
:data:`MODEL_NAME`. ``exfiltrated_messages`` has no ``updated_at`` column, so
comparing timestamps cannot tell when a transcript landed; the model label is
the durable source of truth and is written atomically with the vector. The
upsert on ``message_id`` therefore replaces a stale content-only vector with a
media-aware one. Re-running after that is a no-op.

The run is resumable: selected rows are ordered by id and progress is tracked
with ``after_id``, so a second invocation continues where the last one stopped.
On an embed-api failure the script prints a ``RESUME_MARKER`` and exits
non-zero without writing the failed batch.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.services.embedding_hook import (  # noqa: E402
    MEDIA_AWARE_MODEL_NAME,
    embed_model_name,
    has_derived_media_text,
)
from app.services.media_text import searchable_media_text  # noqa: E402

BATCH_SIZE = 64
DEFAULT_EMBED_API_URL = "http://127.0.0.1:8020"
DEFAULT_TIMEOUT_SECONDS = 30.0
MODEL_NAME = "all-MiniLM-L6-v2"
DIMENSIONS = 384
MESSAGES_TABLE = "exfiltrated_messages"
EMBEDDINGS_TABLE = "message_embeddings"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill 384-d message embeddings via the embed-api service.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    safety = parser.add_mutually_exclusive_group()
    safety.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        help="report eligible messages without HTTP calls or writes (default)",
    )
    safety.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        help="call embed-api and upsert message_embeddings rows",
    )
    parser.set_defaults(apply=False)
    parser.add_argument(
        "--embed-api-url",
        default=DEFAULT_EMBED_API_URL,
        help="base URL of the embed-api service",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"messages per request (max {BATCH_SIZE})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="maximum messages to scan (default: all)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="per-request timeout in seconds",
    )
    return parser


def _chunks(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _fetch_messages(
    client: Any,
    *,
    after_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    query = client.table(MESSAGES_TABLE).select("id,content,file_meta").order("id").limit(limit)
    if after_id is not None:
        query = query.gt("id", after_id)
    return list(query.execute().data or [])


def _embedded_models(client: Any, message_ids: list[str]) -> dict[str, str | None]:
    """Map each embedded message id to the model label of its stored vector."""
    if not message_ids:
        return {}
    rows = (
        client.table(EMBEDDINGS_TABLE)
        .select("message_id,model")
        .in_("message_id", message_ids)
        .execute()
        .data
        or []
    )
    return {row["message_id"]: row.get("model") for row in rows}


def _needs_embedding(row: dict[str, Any], stored_model: str | None) -> bool:
    """Return True when this row's stored vector is missing or stale.

    ``stored_model`` is None when the message has no embedding row. An existing
    vector is only considered stale when the row now has derived media text and
    the vector was not marked media-aware. Rows with no derived text and an
    existing vector are never re-embedded.
    """
    if stored_model is None:
        return True
    if stored_model == MEDIA_AWARE_MODEL_NAME:
        return False
    return has_derived_media_text(row.get("file_meta"))


def _embed_texts(
    http_client: Any,
    base_url: str,
    texts: list[str],
    *,
    timeout: float,
) -> list[list[float]]:
    response = http_client.post(
        f"{base_url.rstrip('/')}/embed",
        json={"texts": texts},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json() or {}
    vectors = payload.get("vectors") or []
    if len(vectors) != len(texts):
        raise ValueError(f"embed-api returned {len(vectors)} vectors for {len(texts)} texts")
    for vector in vectors:
        if len(vector) != DIMENSIONS:
            raise ValueError(f"embed-api returned {len(vector)} dimensions")
    return [[float(value) for value in vector] for vector in vectors]


def _upsert_embeddings(client: Any, rows: list[dict[str, Any]]) -> None:
    client.table(EMBEDDINGS_TABLE).upsert(
        rows,
        on_conflict="message_id",
    ).execute()


def _run(
    *,
    apply: bool,
    base_url: str,
    client: Any,
    http_client: Any,
    batch_size: int = BATCH_SIZE,
    limit: int | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    batch_size = max(1, min(batch_size, BATCH_SIZE))
    stats: dict[str, Any] = {
        "scanned": 0,
        "eligible": 0,
        "embedded": 0,
        "skipped_empty": 0,
        "batches": 0,
        "resume_after": None,
        "stopped": False,
        "error": None,
    }

    after_id: str | None = None
    while True:
        if limit is not None and stats["scanned"] >= limit:
            break
        page_limit = batch_size
        if limit is not None:
            page_limit = max(1, min(batch_size, limit - stats["scanned"]))
        page = _fetch_messages(client, after_id=after_id, limit=page_limit)
        if not page:
            break

        after_id = page[-1]["id"]
        stats["scanned"] += len(page)
        stored_models = _embedded_models(client, [row["id"] for row in page])

        eligible: list[dict[str, Any]] = []
        for row in page:
            if not _needs_embedding(row, stored_models.get(row["id"])):
                continue
            # Same logical document as the FTS migration and the incremental
            # hook: content + file_meta.transcript_text + file_meta.ocr_text.
            text = searchable_media_text(row.get("content"), row.get("file_meta"))
            if not text:
                stats["skipped_empty"] += 1
                continue
            eligible.append({**row, "_search_text": text})

        for chunk in _chunks(eligible, batch_size):
            stats["eligible"] += len(chunk)
            stats["batches"] += 1
            if not apply:
                stats["resume_after"] = chunk[-1]["id"]
                continue
            try:
                vectors = _embed_texts(
                    http_client,
                    base_url,
                    [row["_search_text"] for row in chunk],
                    timeout=timeout,
                )
                _upsert_embeddings(
                    client,
                    [
                        {
                            "message_id": row["id"],
                            "embedding": vector,
                            "model": embed_model_name(row.get("file_meta")),
                        }
                        for row, vector in zip(chunk, vectors, strict=True)
                    ],
                )
            except Exception as exc:  # noqa: BLE001 - stop cleanly, keep progress
                stats["stopped"] = True
                stats["error"] = str(exc)
                return stats
            stats["embedded"] += len(chunk)
            stats["resume_after"] = chunk[-1]["id"]

    return stats


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    import httpx

    from app.core.database import db

    with httpx.Client(timeout=httpx.Timeout(args.timeout)) as http_client:
        stats = _run(
            apply=args.apply,
            base_url=args.embed_api_url,
            client=db,
            http_client=http_client,
            batch_size=args.batch_size,
            limit=args.limit,
            timeout=args.timeout,
        )

    if stats["stopped"]:
        print(f"Stopped: {stats['error']}")
        print(f"RESUME_MARKER after_message_id={stats['resume_after'] or 'none'}")
        print("Re-run the same command to resume; embedded rows are skipped.")
        return 1

    mode = "Applied" if args.apply else "Dry run"
    print(
        f"{mode}: scanned={stats['scanned']} eligible={stats['eligible']} "
        f"embedded={stats['embedded']} skipped_empty={stats['skipped_empty']} "
        f"batches={stats['batches']}"
    )
    if not args.apply:
        print("No changes were made. Pass --apply to write embeddings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
