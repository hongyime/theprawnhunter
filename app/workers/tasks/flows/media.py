"""Media flow task definitions."""

from .common import *

@app.task(name="flow.hash_exfil_media")
def hash_exfil_media(max_messages: int = 100):
    """Download + hash exfiltrated media files that haven't been hashed yet.
    Stores SHA-256 in media_hashes table for duplicate detection across bots.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_hash_exfil_media_logic(max_messages))

async def _hash_exfil_media_logic(max_messages: int) -> dict:
    import hashlib

    # Find media messages that don't have a hash entry yet.
    # LOGIC-002 / DATA-004: pre-filter empty JSONB payloads so rows without a
    # file_id don't waste a download attempt and end up as failure rows.
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
    # Python-side filter: JSONB '{}' is not NULL so it slips past the `.is_("file_meta","null")`
    # filter. Real download requires a file_id (or a Telethon (id, access_hash) pair).
    def _has_downloadable(fm: dict | None) -> bool:
        if not isinstance(fm, dict):
            return False
        if fm.get("file_id"):
            return True
        # Telethon-style bot-independent handle
        return bool(fm.get("id") and fm.get("access_hash"))

    empty_filtered = [r for r in candidate_rows if _has_downloadable(r.get("file_meta") or {})]
    empty_dropped = len(candidate_rows) - len(empty_filtered)
    candidate_rows = empty_filtered
    if not candidate_rows:
        return {"status": "no_candidates", "empty_file_meta_dropped": empty_dropped}

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
        return {
            "status": "all_hashed",
            "candidates_seen": len(candidate_rows),
            "empty_file_meta_dropped": empty_dropped,
        }

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
            # INTR-005: structured failure marker replaces the legacy
            # sha256='__failed__<id>' sentinel. Backward-compat: keep writing
            # the sentinel string too so pre-migration deployments still
            # satisfy the sha256-column NOT NULL constraint.
            try:
                await async_execute(
                    db.table("media_hashes").insert(
                        {
                            "message_id": msg_id,
                            "credential_id": cred_id,
                            "sha256": f"__failed__{msg_id[:8]}",
                            "media_type": media_type,
                            "is_failure": True,
                            "failure_reason": error or "download_returned_none",
                            "error": error or "download_returned_none",
                        }
                    )
                )
            except Exception as insert_exc:
                exc_str = str(insert_exc)
                if "is_failure" in exc_str or "failure_reason" in exc_str:
                    # Migration 20260906000004 not applied yet — fall back
                    # to the legacy sentinel-only insert.
                    with contextlib.suppress(Exception):
                        await async_execute(
                            db.table("media_hashes").insert(
                                {
                                    "message_id": msg_id,
                                    "credential_id": cred_id,
                                    "sha256": f"__failed__{msg_id[:8]}",
                                    "media_type": media_type,
                                    "error": error or "download_returned_none",
                                }
                            )
                        )
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

                import imagehash
                from PIL import Image

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
