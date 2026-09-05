#!/usr/bin/env python
"""
Run a controlled Telegram behavior matrix and print structured JSON.

Input cases come from --matrix JSON or TELEGRAM_PROBE_MATRIX. Each case:
{
  "name": "bot-in-group",
  "bot_token": "123:ABC",
  "chat_id": -100123,
  "allow_delete_webhook": false
}
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_MATRIX_CASES = [
    {
        "name": "bot_in_group",
        "bot_token_env": "TELEGRAM_PROBE_BOT_IN_GROUP_TOKEN",
        "chat_id_env": "TELEGRAM_PROBE_BOT_IN_GROUP_CHAT_ID",
        "expect_chat_access": True,
    },
    {
        "name": "bot_not_in_group",
        "bot_token_env": "TELEGRAM_PROBE_BOT_NOT_IN_GROUP_TOKEN",
        "chat_id_env": "TELEGRAM_PROBE_BOT_NOT_IN_GROUP_CHAT_ID",
        "expect_chat_access": False,
    },
    {
        "name": "channel_or_supergroup",
        "bot_token_env": "TELEGRAM_PROBE_CHANNEL_TOKEN",
        "chat_id_env": "TELEGRAM_PROBE_CHANNEL_CHAT_ID",
        "expect_chat_type": ["channel", "supergroup"],
    },
    {
        "name": "webhook_enabled",
        "bot_token_env": "TELEGRAM_PROBE_WEBHOOK_ENABLED_TOKEN",
        "chat_id_env": "TELEGRAM_PROBE_WEBHOOK_ENABLED_CHAT_ID",
        "expect_webhook_present": True,
    },
    {
        "name": "webhook_disabled",
        "bot_token_env": "TELEGRAM_PROBE_WEBHOOK_DISABLED_TOKEN",
        "chat_id_env": "TELEGRAM_PROBE_WEBHOOK_DISABLED_CHAT_ID",
        "expect_webhook_present": False,
    },
    {
        "name": "invite_allowed",
        "bot_token_env": "TELEGRAM_PROBE_INVITE_ALLOWED_TOKEN",
        "chat_id_env": "TELEGRAM_PROBE_INVITE_ALLOWED_CHAT_ID",
        "invite_link_env": "TELEGRAM_PROBE_INVITE_ALLOWED_LINK",
    },
    {
        "name": "invite_blocked",
        "bot_token_env": "TELEGRAM_PROBE_INVITE_BLOCKED_TOKEN",
        "chat_id_env": "TELEGRAM_PROBE_INVITE_BLOCKED_CHAT_ID",
        "invite_link_env": "TELEGRAM_PROBE_INVITE_BLOCKED_LINK",
    },
    {
        "name": "update_visibility",
        "bot_token_env": "TELEGRAM_PROBE_UPDATE_VISIBILITY_TOKEN",
        "chat_id_env": "TELEGRAM_PROBE_UPDATE_VISIBILITY_CHAT_ID",
        "expect_updates_visible": True,
    },
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


sys.path.insert(0, str(_repo_root()))


def _redact_token(token: str | None) -> str | None:
    if not token or ":" not in token:
        return None
    bot_id = token.split(":", 1)[0]
    return f"{bot_id}:<redacted>"


def _coerce_chat_id(value: str | None) -> int | str | None:
    if value is None or value == "":
        return None
    return int(value) if value.lstrip("-").isdigit() else value


def _default_matrix() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for template in DEFAULT_MATRIX_CASES:
        case = dict(template)
        token_env = str(case.pop("bot_token_env"))
        chat_id_env = str(case.pop("chat_id_env", ""))
        invite_link_env = case.pop("invite_link_env", None)
        case["bot_token"] = os.getenv(token_env)
        case["chat_id"] = _coerce_chat_id(os.getenv(chat_id_env)) if chat_id_env else None
        case["allow_delete_webhook"] = False
        case["source_env"] = {
            "bot_token": token_env,
            "chat_id": chat_id_env,
        }
        if invite_link_env:
            case["invite_link"] = os.getenv(str(invite_link_env))
            case["source_env"]["invite_link"] = str(invite_link_env)
        cases.append(case)
    return cases


def _load_matrix(path: str | None) -> list[dict[str, Any]]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    raw = os.getenv("TELEGRAM_PROBE_MATRIX")
    if raw:
        return json.loads(raw)
    token = os.getenv("TELEGRAM_PROBE_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_PROBE_CHAT_ID")
    if token:
        return [
            {
                "name": "default",
                "bot_token": token,
                "chat_id": _coerce_chat_id(chat_id),
                "allow_delete_webhook": False,
            }
        ]
    return _default_matrix()


async def _probe_case(case: dict[str, Any]) -> dict[str, Any]:
    token = case.get("bot_token")
    chat_id = case.get("chat_id")
    allow_delete = bool(case.get("allow_delete_webhook"))
    output: dict[str, Any] = {
        "name": case.get("name") or "unnamed",
        "bot_token": _redact_token(token),
        "chat_id": chat_id,
        "allow_delete_webhook": allow_delete,
        "started_at": datetime.now(UTC).isoformat(),
        "checks": {},
    }
    if case.get("source_env"):
        output["source_env"] = case["source_env"]
    expectations = {
        key: value
        for key, value in case.items()
        if key.startswith("expect_")
    }
    if expectations:
        output["expectations"] = expectations
    if not token:
        output["status"] = "skipped"
        output["reason"] = "missing_bot_token"
        return output

    base_url = f"https://api.telegram.org/bot{token}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            me = await client.get(f"{base_url}/getMe")
            me_body = me.json() if me.headers.get("content-type", "").startswith("application/json") else {}
            output["checks"]["getMe"] = {
                "status_code": me.status_code,
                "ok": bool(me_body.get("ok")),
                "bot_id": (me_body.get("result") or {}).get("id") if isinstance(me_body, dict) else None,
                "username": (me_body.get("result") or {}).get("username") if isinstance(me_body, dict) else None,
            }
        except Exception as exc:
            output["checks"]["getMe"] = {"error": str(exc)[:300]}

        try:
            webhook = await client.get(f"{base_url}/getWebhookInfo")
            webhook_body = webhook.json()
            webhook_result = webhook_body.get("result") or {}
            output["checks"]["getWebhookInfo"] = {
                "status_code": webhook.status_code,
                "ok": bool(webhook_body.get("ok")),
                "webhook_present": bool(webhook_result.get("url")),
                "pending_update_count": webhook_result.get("pending_update_count"),
                "last_error_message": webhook_result.get("last_error_message"),
            }
            if webhook_result.get("url") and allow_delete:
                deleted = await client.post(f"{base_url}/deleteWebhook")
                output["checks"]["deleteWebhook"] = {
                    "status_code": deleted.status_code,
                    "ok": bool(deleted.json().get("ok")) if deleted.content else False,
                }
            elif webhook_result.get("url"):
                output["checks"]["deleteWebhook"] = {
                    "skipped": True,
                    "reason": "allow_delete_webhook_false",
                }
        except Exception as exc:
            output["checks"]["getWebhookInfo"] = {"error": str(exc)[:300]}

        try:
            updates = await client.get(f"{base_url}/getUpdates", params={"limit": 100})
            updates_body = updates.json() if updates.content else {}
            update_rows = updates_body.get("result") if isinstance(updates_body, dict) else []
            output["checks"]["getUpdates"] = {
                "status_code": updates.status_code,
                "ok": bool(updates_body.get("ok")) if isinstance(updates_body, dict) else False,
                "update_count": len(update_rows or []),
                "chat_ids": sorted(
                    {
                        item.get("message", {}).get("chat", {}).get("id")
                        or item.get("channel_post", {}).get("chat", {}).get("id")
                        for item in (update_rows or [])
                        if isinstance(item, dict)
                    }
                    - {None}
                ),
            }
        except Exception as exc:
            output["checks"]["getUpdates"] = {"error": str(exc)[:300]}

        if chat_id:
            try:
                chat = await client.get(f"{base_url}/getChat", params={"chat_id": chat_id})
                chat_body = chat.json() if chat.content else {}
                chat_result = chat_body.get("result") if isinstance(chat_body, dict) else {}
                output["checks"]["getChat"] = {
                    "status_code": chat.status_code,
                    "ok": bool(chat_body.get("ok")) if isinstance(chat_body, dict) else False,
                    "type": (chat_result or {}).get("type") if isinstance(chat_result, dict) else None,
                    "title": (chat_result or {}).get("title") if isinstance(chat_result, dict) else None,
                    "description": (chat_body.get("description") or "")[:200]
                    if isinstance(chat_body, dict)
                    else None,
                }
            except Exception as exc:
                output["checks"]["getChat"] = {"error": str(exc)[:300]}

        invite_link = case.get("invite_link")
        if invite_link:
            output["checks"]["invite_link"] = {
                "configured": True,
                "redacted": str(invite_link).split("?", 1)[0],
                "joined": False,
                "reason": "non_destructive_probe_only",
            }

    output["finished_at"] = datetime.now(UTC).isoformat()
    output["status"] = "ok"
    return output


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", help="Path to JSON array of probe cases")
    args = parser.parse_args()
    cases = _load_matrix(args.matrix)
    results = [await _probe_case(case) for case in cases]
    print(json.dumps({"status": "ok", "results": results}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
