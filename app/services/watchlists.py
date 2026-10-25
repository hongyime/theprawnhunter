"""Watchlist evaluation for captured message content.

Operators define literal or regex patterns in ``watchlist_rules``. The capture
lane calls :func:`evaluate` after a message is stored; matches are recorded in
``watchlist_hits`` and returned to the caller so the alert lane can broadcast
them.

Design notes
------------
* Evaluation is best-effort by contract: every failure path logs and returns
  ``[]``. A malformed rule must never break message capture.
* Enabled rules are cached in-process for :data:`RULES_CACHE_TTL_SECONDS`
  seconds (monotonic clock). The repo uses Redis for cross-process caches, but
  watchlists are a small operator-owned table read once per minute per worker;
  an in-process TTL keeps the capture path free of a Redis round-trip and makes
  it trivially testable via :func:`clear_cache`.
* Duplicate suppression uses ``ON CONFLICT (rule_id, message_id) DO NOTHING``
  through PostgREST's ``ignore_duplicates``. PostgREST returns only inserted
  rows, so an empty response means the pair already existed.
* Cooldown is derived from the most recent ``watchlist_hits`` row for the
  ``(rule_id, credential_id)`` pair (globally per rule when ``credential_id``
  is NULL). No extra state store is required and the behaviour is testable
  against the hits table alone.
* Regex rules are length-capped and evaluated under ``SIGALRM`` on the main
  thread. When evaluation happens off the main thread (where signals are
  unavailable) a daemon helper thread with a bounded join is used: the caller
  still returns within the timeout, but the orphaned thread cannot be killed
  and is left to finish on its own. Production evaluation runs on the event
  loop's main thread, so the signal path is the authoritative one.
"""

from __future__ import annotations

import re
import signal
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.database import db
from app.core.logger import get_logger
from app.infrastructure.database import execute_async

logger = get_logger(__name__)

RULES_CACHE_TTL_SECONDS = 60
MAX_PATTERN_LENGTH = 200
REGEX_TIMEOUT_SECONDS = 0.25
MAX_MATCHED_TEXT_LENGTH = 300

_RULES_SELECT = "id,name,pattern,is_regex,credential_id,enabled,cooldown_minutes"

_rules_cache: list[dict[str, Any]] | None = None
_rules_cache_expires_at = 0.0


class _RegexTimeoutError(Exception):
    """Raised from the SIGALRM handler when a regex evaluation overruns."""


def clear_cache() -> None:
    """Invalidate the enabled-rules cache (used by tests and operators)."""
    global _rules_cache, _rules_cache_expires_at
    _rules_cache = None
    _rules_cache_expires_at = 0.0


def _cache_is_valid() -> bool:
    return _rules_cache is not None and time.monotonic() < _rules_cache_expires_at


async def load_rules(*, client: Any = db) -> list[dict[str, Any]]:
    """Return enabled watchlist rules, cached for :data:`RULES_CACHE_TTL_SECONDS`."""
    global _rules_cache, _rules_cache_expires_at

    if _cache_is_valid():
        return list(_rules_cache or [])

    try:
        query = (
            client.table("watchlist_rules")
            .select(_RULES_SELECT)
            .eq("enabled", True)
        )
        result = await execute_async(query)
        rows = [
            dict(row)
            for row in (getattr(result, "data", None) or [])
            if isinstance(row, Mapping)
        ]
    except Exception as exc:  # noqa: BLE001 - capture path must never break
        logger.warning(f"[Watchlist] could not load rules: {exc}")
        return list(_rules_cache or [])

    _rules_cache = rows
    _rules_cache_expires_at = time.monotonic() + RULES_CACHE_TTL_SECONDS
    return list(rows)


def _raise_regex_timeout(_signum: int, _frame: Any) -> None:
    raise _RegexTimeoutError


def _signal_timeout_available() -> bool:
    return (
        hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )


def _match_with_signal_timeout(
    compiled: re.Pattern[str], content: str
) -> re.Match[str] | None:
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_regex_timeout)
    signal.setitimer(signal.ITIMER_REAL, REGEX_TIMEOUT_SECONDS)
    try:
        return compiled.search(content)
    except _RegexTimeoutError:
        logger.warning(
            f"[Watchlist] regex evaluation timed out after {REGEX_TIMEOUT_SECONDS:.2f}s"
        )
        return None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _match_with_thread_timeout(
    compiled: re.Pattern[str], content: str
) -> re.Match[str] | None:
    outcome: list[re.Match[str] | None] = []

    def _run() -> None:
        try:
            outcome.append(compiled.search(content))
        except Exception:  # noqa: BLE001 - surfaced as "no match"
            outcome.append(None)

    worker = threading.Thread(
        target=_run, name="watchlist-regex", daemon=True
    )
    worker.start()
    worker.join(REGEX_TIMEOUT_SECONDS)
    if worker.is_alive():
        logger.warning(
            "[Watchlist] regex evaluation timed out in non-main-thread fallback; "
            "returning no match"
        )
        return None
    return outcome[0] if outcome else None


def _match_with_timeout(
    compiled: re.Pattern[str], content: str
) -> re.Match[str] | None:
    if _signal_timeout_available():
        return _match_with_signal_timeout(compiled, content)
    return _match_with_thread_timeout(compiled, content)


def _matched_text(rule: Mapping[str, Any], content: str) -> str | None:
    """Return the matched text, or ``None`` when the rule does not match."""
    pattern = str(rule.get("pattern") or "")
    if not pattern:
        return None
    if len(pattern) > MAX_PATTERN_LENGTH:
        logger.warning(
            f"[Watchlist] skipping rule {rule.get('id')}: pattern length "
            f"{len(pattern)} exceeds {MAX_PATTERN_LENGTH}"
        )
        return None

    try:
        if rule.get("is_regex"):
            compiled = re.compile(pattern, re.IGNORECASE)
            match = _match_with_timeout(compiled, content)
        else:
            # Literal rules never go through the regex engine as a pattern.
            compiled = re.compile(re.escape(pattern), re.IGNORECASE)
            match = compiled.search(content)
    except re.error as exc:
        logger.warning(f"[Watchlist] skipping rule {rule.get('id')}: invalid regex: {exc}")
        return None

    if match is None:
        return None
    text = match.group(0) or content
    return text[:MAX_MATCHED_TEXT_LENGTH]


async def _on_cooldown(
    rule: Mapping[str, Any],
    *,
    credential_id: str | None,
    client: Any,
    now: datetime,
) -> bool:
    try:
        cooldown_minutes = int(rule.get("cooldown_minutes") or 0)
    except (TypeError, ValueError):
        cooldown_minutes = 0
    if cooldown_minutes <= 0:
        return False

    cutoff = (now - timedelta(minutes=cooldown_minutes)).isoformat()
    query = (
        client.table("watchlist_hits")
        .select("id")
        .eq("rule_id", str(rule["id"]))
        .gte("created_at", cutoff)
    )
    if credential_id:
        query = query.eq("credential_id", credential_id)
    else:
        query = query.is_("credential_id", "null")
    query = query.order("created_at", desc=True).limit(1)
    result = await execute_async(query)
    return bool(getattr(result, "data", None))


async def _record_hit(
    rule_id: str,
    *,
    message_id: str,
    credential_id: str | None,
    matched_text: str,
    client: Any,
) -> bool:
    """Insert the hit; return ``False`` when the (rule, message) pair existed."""
    payload = {
        "rule_id": rule_id,
        "message_id": message_id,
        "credential_id": credential_id,
        "matched_text": matched_text[:MAX_MATCHED_TEXT_LENGTH],
    }
    query = client.table("watchlist_hits").upsert(
        payload,
        on_conflict="rule_id,message_id",
        ignore_duplicates=True,
    )
    result = await execute_async(query)
    return bool(getattr(result, "data", None))


async def _evaluate_rule(
    rule: Mapping[str, Any],
    content: str,
    *,
    message_id: str,
    credential_id: str | None,
    client: Any,
    now: datetime,
) -> dict[str, Any] | None:
    rule_id = rule.get("id")
    if not rule_id:
        return None

    # Rules may be scoped to a single credential.
    scoped_credential = rule.get("credential_id")
    if scoped_credential and str(scoped_credential) != (credential_id or ""):
        return None

    matched_text = _matched_text(rule, content)
    if matched_text is None:
        return None

    rule_id_str = str(rule_id)
    if await _on_cooldown(
        rule, credential_id=credential_id, client=client, now=now
    ):
        logger.debug(f"[Watchlist] rule {rule_id_str} suppressed by cooldown")
        return None

    recorded = await _record_hit(
        rule_id_str,
        message_id=message_id,
        credential_id=credential_id,
        matched_text=matched_text,
        client=client,
    )
    if not recorded:
        logger.debug(f"[Watchlist] rule {rule_id_str} already recorded for message")
        return None

    return {
        "rule_id": rule_id_str,
        "name": str(rule.get("name") or ""),
        "matched_text": matched_text,
        "credential_id": credential_id,
    }


async def evaluate(
    content: str,
    *,
    message_id: Any,
    credential_id: Any = None,
    client: Any = db,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Evaluate ``content`` against enabled watchlist rules.

    Returns one dict per emitted hit with keys ``rule_id``, ``name``,
    ``matched_text`` and ``credential_id``. Never raises into the capture path.
    """
    if not isinstance(content, str) or not content.strip():
        return []
    if message_id is None:
        return []

    try:
        rules = await load_rules(client=client)
    except Exception as exc:  # noqa: BLE001 - defensive; load_rules already guards
        logger.warning(f"[Watchlist] rule load failed: {exc}")
        return []
    if not rules:
        return []

    observed_at = now or datetime.now(UTC)
    message_id_str = str(message_id)
    credential_id_str = str(credential_id) if credential_id else None

    hits: list[dict[str, Any]] = []
    for rule in rules:
        try:
            hit = await _evaluate_rule(
                rule,
                content,
                message_id=message_id_str,
                credential_id=credential_id_str,
                client=client,
                now=observed_at,
            )
        except Exception as exc:  # noqa: BLE001 - one bad rule must not stop the rest
            logger.warning(
                f"[Watchlist] rule {rule.get('id')} evaluation failed: {exc}"
            )
            continue
        if hit is not None:
            hits.append(hit)
    return hits


__all__ = [
    "MAX_MATCHED_TEXT_LENGTH",
    "MAX_PATTERN_LENGTH",
    "REGEX_TIMEOUT_SECONDS",
    "RULES_CACHE_TTL_SECONDS",
    "clear_cache",
    "evaluate",
    "load_rules",
]
