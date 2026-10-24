"""Safe backfill for the display rework.

Subcommands:
    full-content   restore full untruncated text for honeypot rows whose
                   stored content was capped at 2000 chars, using the raw
                   update retained in honeypot_updates.
    source-labels  backfill source_chat_type / source_chat_title into
                   exfiltrated_messages.file_meta and fill NULL chat_name /
                   chat_type on discovered_credentials.

Examples:
    python scripts/ops/backfill_rework.py full-content --dry-run
    python scripts/ops/backfill_rework.py source-labels --apply

Every mode is read-only unless ``--apply`` is explicitly supplied.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

BATCH_SIZE = 500
_TARGET_KEYS = ("message", "channel_post", "edited_message", "edited_channel_post")


def extract_full_text(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in _TARGET_KEYS:
        target = payload.get(key)
        if isinstance(target, dict):
            text = target.get("text") or target.get("caption")
            if isinstance(text, str):
                return text
    callback = payload.get("callback_query")
    if isinstance(callback, dict):
        data = callback.get("data")
        if isinstance(data, str) and data:
            return f"[Callback Query] {data}"
    inline = payload.get("inline_query")
    if isinstance(inline, dict):
        query = inline.get("query")
        if isinstance(query, str) and query:
            return f"[Inline Query] {query}"
    return None


def source_label_patch(credential: dict | None, file_meta: dict | None) -> dict:
    file_meta = dict(file_meta or {})
    chat_type = None
    chat_title = None
    if isinstance(credential, dict):
        chat_type = credential.get("chat_type")
        chat_title = credential.get("chat_name")
    if not chat_title and isinstance(file_meta.get("chat_id"), int) and isinstance(credential, dict):
        meta = credential.get("meta")
        if isinstance(meta, dict):
            for entry in meta.get("all_chats") or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("chat_id") == file_meta.get("chat_id"):
                    chat_type = entry.get("chat_type") or chat_type
                    chat_title = entry.get("chat_name") or chat_title
                    break
    file_meta["source_chat_type"] = chat_type or "unknown"
    file_meta["source_chat_title"] = chat_title
    return file_meta


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    for name, help_text in (
        ("full-content", "restore full text for truncated honeypot rows"),
        ("source-labels", "backfill chat source labels and credential chat fields"),
    ):
        mode = modes.add_parser(name, help=help_text)
        safety = mode.add_mutually_exclusive_group()
        safety.add_argument(
            "--dry-run",
            dest="apply",
            action="store_false",
            help="report eligible rows without writes (default)",
        )
        safety.add_argument(
            "--apply",
            dest="apply",
            action="store_true",
            help="perform the selected database updates",
        )
        mode.set_defaults(apply=False)
    labels = modes.choices["source-labels"]
    scope = labels.add_mutually_exclusive_group()
    scope.add_argument(
        "--messages-only",
        dest="scope",
        action="store_const",
        const="messages",
        help="only backfill exfiltrated_messages.file_meta source labels",
    )
    scope.add_argument(
        "--credentials-only",
        dest="scope",
        action="store_const",
        const="credentials",
        help="only backfill discovered_credentials.chat_name/chat_type",
    )
    labels.set_defaults(scope="all")
    return parser


def _run_full_content(apply: bool) -> int:
    from app.core.database import db

    total = 0
    offset = 0
    while True:
        query = (
            db.table("exfiltrated_messages")
            .select("id, credential_id, telegram_msg_id, content, file_meta")
            .eq("file_meta->>content_truncated", "true")
            .lt("telegram_msg_id", 0)
            .order("id")
        )
        rows = (query.range(offset, offset + BATCH_SIZE - 1).execute().data) or []
        if not rows:
            break
        offset += len(rows)

        for row in rows:
            update_id = -int(row["telegram_msg_id"])
            raw_res = (
                db.table("honeypot_updates")
                .select("payload")
                .eq("credential_id", row["credential_id"])
                .eq("update_id", update_id)
                .limit(1)
                .execute()
            )
            payload = ((raw_res.data or [{}])[0]).get("payload")
            full_text = extract_full_text(payload)
            if full_text is None:
                continue
            total += 1
            if not apply:
                continue
            file_meta = dict(row.get("file_meta") or {})
            file_meta["content_truncated"] = False
            file_meta["text_preview_version"] = 2
            file_meta["content_original_length"] = len(full_text)
            db.table("exfiltrated_messages").update(
                {"content": full_text, "file_meta": file_meta}
            ).eq("id", row["id"]).execute()

    if apply:
        print(f"Applied full-content restore: {total} rows updated.")
    else:
        print(f"Dry run: {total} rows would be restored. Pass --apply to write.")
    return 0


def _run_source_labels(apply: bool, scope: str = "all") -> int:
    from app.core.database import db

    messages_done = 0
    if scope in ("all", "messages"):
        offset = 0
        while True:
            query = (
                db.table("exfiltrated_messages")
                .select("id, credential_id, file_meta")
                .is_("file_meta->>source_chat_type", "null")
                .order("id")
            )
            rows = (query.range(offset, offset + BATCH_SIZE - 1).execute().data) or []
            if not rows:
                break
            offset += len(rows)

            for row in rows:
                cred_res = (
                    db.table("discovered_credentials")
                    .select("chat_id, chat_name, chat_type, meta")
                    .eq("id", row["credential_id"])
                    .limit(1)
                    .execute()
                )
                credential = (cred_res.data or [None])[0]
                file_meta = source_label_patch(credential, row.get("file_meta"))
                messages_done += 1
                if apply:
                    db.table("exfiltrated_messages").update({"file_meta": file_meta}).eq(
                        "id", row["id"]
                    ).execute()

    credentials_done = 0
    if scope in ("all", "credentials"):
        offset = 0
        while True:
            query = (
                db.table("discovered_credentials")
                .select("id, chat_id, chat_name, chat_type, meta")
                .or_("chat_name.is.null,chat_type.is.null")
                .order("id")
            )
            rows = (query.range(offset, offset + BATCH_SIZE - 1).execute().data) or []
            if not rows:
                break
            offset += len(rows)

            for row in rows:
                meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
                chat_type = row.get("chat_type")
                chat_title = row.get("chat_name")
                if not chat_type and isinstance(meta.get("chat_type"), str):
                    chat_type = meta["chat_type"]
                if not chat_title and isinstance(meta.get("chat_name"), str):
                    chat_title = meta["chat_name"]
                all_chats = meta.get("all_chats") or []
                candidates = [
                    entry
                    for entry in all_chats
                    if isinstance(entry, dict) and entry.get("chat_name")
                ]
                match = next(
                    (entry for entry in candidates if entry.get("chat_id") == row.get("chat_id")),
                    candidates[0] if candidates else None,
                )
                if match is not None:
                    if not chat_type and match.get("chat_type"):
                        chat_type = match["chat_type"]
                    if not chat_title and match.get("chat_name"):
                        chat_title = match["chat_name"]
                if chat_type is None and chat_title is None:
                    continue
                credentials_done += 1
                if apply:
                    db.table("discovered_credentials").update(
                        {"chat_name": chat_title, "chat_type": chat_type}
                    ).eq("id", row["id"]).execute()

    if apply:
        print(
            f"Applied source labels: messages={messages_done}, credentials={credentials_done}."
        )
    else:
        print(
            f"Dry run: {messages_done} messages and {credentials_done} credentials "
            "would be backfilled. Pass --apply to write."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.mode == "full-content":
        return _run_full_content(apply=args.apply)
    if args.mode == "source-labels":
        return _run_source_labels(apply=args.apply, scope=args.scope)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
