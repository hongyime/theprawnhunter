"""Operator-only ``/watchlist`` command.

The webapp manages watchlist rules through operator-gated SQL RPCs, which the
bot cannot call (they require an operator JWT). This handler writes the same
``watchlist_rules`` / ``watchlist_hits`` rows through the service-role client
with the same validation constraints as ``upsert_watchlist_rule``:

* name must be non-empty,
* pattern length must be between 1 and ``MAX_PATTERN_LENGTH``,
* cooldown must be at least one minute,
* regex patterns must compile before they are stored, so a broken pattern can
  never reach the evaluator.

Every reply is compact Markdown and is deleted after
``AUTO_DELETE_SECONDS`` through the shared ``_reply_and_delete`` helper. The
handler is operator-gated exactly like ``/pause``: non-admins get no reply and
no state change. Failures reply with a short message and never raise.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.database import db
from app.infrastructure.database import execute_async
from app.services.watchlist_alerts import _resolve_message_deeplink
from app.services.watchlists import MAX_PATTERN_LENGTH

from .common import (
    ContextTypes,
    ParseMode,
    Update,
    _reply_and_delete,
    escape_markdown,
    is_admin,
    logger,
)

ID_PREFIX_LENGTH = 8
LIST_LIMIT = 20
NAME_PREVIEW_LENGTH = 40
PATTERN_PREVIEW_LENGTH = 40
MATCH_PREVIEW_LENGTH = 80
HITS_DEFAULT = 5
HITS_MAX = 20
DEFAULT_COOLDOWN_MINUTES = 60
RULE_FETCH_LIMIT = 1000

USAGE = (
    "Usage: /watchlist [list | add [-r] <name> | <pattern> | "
    "rm <id> | on <id> | off <id> | cooldown <id> <minutes> | hits [n]]"
)


class _RuleError(Exception):
    """Internal signal for a user-facing validation failure."""


def _short_id(value: Any) -> str:
    return str(value or "")[:ID_PREFIX_LENGTH]


def _preview(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _md(value: Any, limit: int | None = None) -> str:
    text = _preview(value, limit) if limit else str(value or "")
    return escape_markdown(text, version=1)


def _format_time(value: Any) -> str:
    if not value:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:16]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%m-%d %H:%M UTC")


def _rule_line(rule: Mapping[str, Any]) -> str:
    credential_id = rule.get("credential_id")
    scope = f"`{_md(_short_id(credential_id))}`" if credential_id else "GLOBAL"
    kind = "regex" if rule.get("is_regex") else "text"
    cooldown = rule.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)
    state = "on" if rule.get("enabled", True) else "off"
    return (
        f"• `{_short_id(rule.get('id'))}` {_md(rule.get('name') or 'unnamed', NAME_PREVIEW_LENGTH)} "
        f"[{kind}] \"{_md(rule.get('pattern'), PATTERN_PREVIEW_LENGTH)}\" "
        f"{scope} cd={cooldown} {state}"
    )


async def _execute(query_builder: Any) -> Any:
    return await execute_async(query_builder)


async def _fetch_rules() -> list[dict[str, Any]]:
    result = await _execute(
        db.table("watchlist_rules")
        .select("id,name,pattern,is_regex,credential_id,enabled,cooldown_minutes")
        .order("created_at", desc=True)
        .limit(LIST_LIMIT + 1)
    )
    return [dict(row) for row in (getattr(result, "data", None) or []) if isinstance(row, Mapping)]


async def _list_rules() -> str:
    rows = await _fetch_rules()
    if not rows:
        return "No watchlist rules."

    shown = rows[:LIST_LIMIT]
    lines = [f"*Watchlist rules* ({len(shown)})", ""]
    lines.extend(_rule_line(rule) for rule in shown)
    if len(rows) > LIST_LIMIT:
        lines.append(f"Showing first {LIST_LIMIT}; more rules exist.")
    return "\n".join(lines)


def _parse_add_args(raw_args: list[str]) -> tuple[bool, str, str] | None:
    if not raw_args:
        return None
    is_regex = raw_args[0].lower() in ("-r", "--regex")
    body = " ".join(raw_args[1:] if is_regex else raw_args)
    if "|" not in body:
        return None
    name, pattern = body.split("|", 1)
    return is_regex, name.strip(), pattern.strip()


def _validate_rule(name: str, pattern: str, is_regex: bool) -> None:
    if not name:
        raise _RuleError("Rule name is required.")
    if not pattern:
        raise _RuleError("Rule pattern is required.")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise _RuleError(f"Pattern too long (max {MAX_PATTERN_LENGTH} characters).")
    if is_regex:
        try:
            re.compile(pattern, re.IGNORECASE)
        except (re.error, RecursionError):
            raise _RuleError("Invalid regex pattern.") from None


async def _add_rule(raw_args: list[str]) -> str:
    parsed = _parse_add_args(raw_args)
    if parsed is None:
        raise _RuleError("Usage: /watchlist add [-r] <name> | <pattern>")
    is_regex, name, pattern = parsed
    _validate_rule(name, pattern, is_regex)

    payload = {
        "name": name,
        "pattern": pattern,
        "is_regex": is_regex,
        "credential_id": None,
        "enabled": True,
        "cooldown_minutes": DEFAULT_COOLDOWN_MINUTES,
    }
    result = await _execute(db.table("watchlist_rules").insert(payload))
    rows = getattr(result, "data", None) or []
    rule_id = _short_id(rows[0].get("id")) if rows and isinstance(rows[0], Mapping) else "new"
    kind = "regex" if is_regex else "literal"
    return f"Rule added: `{rule_id}` ({kind})."


async def _find_rule(prefix: str) -> dict[str, Any]:
    if not prefix:
        raise _RuleError(USAGE)

    result = await _execute(
        db.table("watchlist_rules").select("id,name").limit(RULE_FETCH_LIMIT)
    )
    rows = [
        dict(row)
        for row in (getattr(result, "data", None) or [])
        if isinstance(row, Mapping)
    ]
    matches = [row for row in rows if str(row.get("id") or "").startswith(prefix)]
    if not matches:
        raise _RuleError("Rule not found.")
    if len(matches) > 1:
        raise _RuleError(f"Ambiguous prefix ({len(matches)} matches). Use more characters.")
    return matches[0]


def _rule_label(rule: Mapping[str, Any]) -> str:
    return f"`{_short_id(rule.get('id'))}` {_md(rule.get('name') or 'unnamed', NAME_PREVIEW_LENGTH)}"


async def _remove_rule(prefix: str) -> str:
    rule = await _find_rule(prefix)
    await _execute(db.table("watchlist_rules").delete().eq("id", rule["id"]))
    return f"Rule deleted: {_rule_label(rule)}."


async def _toggle_rule(prefix: str, *, enabled: bool) -> str:
    rule = await _find_rule(prefix)
    await _execute(
        db.table("watchlist_rules")
        .update({"enabled": enabled})
        .eq("id", rule["id"])
    )
    state = "enabled" if enabled else "disabled"
    return f"Rule {state}: {_rule_label(rule)}."


async def _set_cooldown(prefix: str, raw_minutes: str) -> str:
    rule = await _find_rule(prefix)
    try:
        minutes = int(raw_minutes)
    except (TypeError, ValueError):
        raise _RuleError("Cooldown must be a whole number of minutes.") from None
    if minutes < 1:
        raise _RuleError("Cooldown must be at least 1 minute.")

    await _execute(
        db.table("watchlist_rules")
        .update({"cooldown_minutes": minutes})
        .eq("id", rule["id"])
    )
    return f"Cooldown set to {minutes}m: {_rule_label(rule)}."


def _hits_limit(raw: str | None) -> int:
    if raw is None:
        return HITS_DEFAULT
    try:
        requested = int(raw)
    except (TypeError, ValueError):
        raise _RuleError("Usage: /watchlist hits [1-20]") from None
    return max(1, min(HITS_MAX, requested))


async def _rule_names(rule_ids: list[str]) -> dict[str, str]:
    if not rule_ids:
        return {}
    result = await _execute(
        db.table("watchlist_rules").select("id,name").in_("id", rule_ids)
    )
    return {
        str(row.get("id")): str(row.get("name") or "unnamed")
        for row in (getattr(result, "data", None) or [])
        if isinstance(row, Mapping)
    }


async def _format_hits(raw_limit: str | None) -> str:
    limit = _hits_limit(raw_limit)
    result = await _execute(
        db.table("watchlist_hits")
        .select("id,rule_id,message_id,credential_id,matched_text,created_at")
        .order("created_at", desc=True)
        .limit(limit)
    )
    hits = [
        dict(row)
        for row in (getattr(result, "data", None) or [])
        if isinstance(row, Mapping)
    ]
    if not hits:
        return "No watchlist hits."

    names = await _rule_names(
        sorted({str(hit.get("rule_id")) for hit in hits if hit.get("rule_id")})
    )

    lines = [f"*Recent watchlist hits* ({len(hits)})", ""]
    for hit in hits:
        name = names.get(str(hit.get("rule_id")), "unknown rule")
        credential_id = hit.get("credential_id")
        credential = (
            f"`{_short_id(credential_id)}`" if credential_id else "global"
        )
        matched = _preview(hit.get("matched_text"), MATCH_PREVIEW_LENGTH)
        lines.append(
            f"• {_format_time(hit.get('created_at'))} | {_md(name, NAME_PREVIEW_LENGTH)} "
            f"| {credential} | \"{_md(matched)}\""
        )
        try:
            link = await _resolve_message_deeplink(
                message_id=hit.get("message_id"),
                credential_id=credential_id,
            )
        except Exception as exc:  # noqa: BLE001 - link is optional
            logger.warning(
                "[WatchlistCmd] deep link failed for hit %s: %s",
                _short_id(hit.get("id")),
                type(exc).__name__,
            )
            link = None
        if link:
            lines.append(f"📎 {link}")
    return "\n".join(lines)


async def _dispatch(sub: str, rest: list[str]) -> str:
    if sub in ("list", "ls"):
        return await _list_rules()
    if sub == "add":
        return await _add_rule(rest)
    if sub == "rm":
        return await _remove_rule((rest[0] if rest else "").strip().lower())
    if sub == "on":
        return await _toggle_rule(
            (rest[0] if rest else "").strip().lower(), enabled=True
        )
    if sub == "off":
        return await _toggle_rule(
            (rest[0] if rest else "").strip().lower(), enabled=False
        )
    if sub == "cooldown":
        if len(rest) < 2:
            raise _RuleError("Usage: /watchlist cooldown <id> <minutes>")
        return await _set_cooldown(
            rest[0].strip().lower(), rest[1]
        )
    if sub == "hits":
        return await _format_hits(rest[0] if rest else None)
    raise _RuleError(USAGE)


async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage watchlist rules from the monitor group (operator only)."""
    if not is_admin(update):
        return

    args = list(getattr(context, "args", None) or [])
    sub = args[0].strip().lower() if args else "list"
    rest = args[1:]

    try:
        text = await _dispatch(sub, rest)
    except _RuleError as exc:
        text = str(exc)
    except Exception as exc:  # noqa: BLE001 - the bot must not crash
        logger.error("[WatchlistCmd] %s failed: %s", sub, type(exc).__name__)
        text = "Watchlist command failed. Try again."

    try:
        await _reply_and_delete(update, text, parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:  # noqa: BLE001 - delivery failure must not raise
        logger.warning("[WatchlistCmd] reply failed: %s", type(exc).__name__)


__all__ = ["watchlist_command"]
