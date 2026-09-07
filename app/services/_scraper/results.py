import asyncio
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any


class ScrapeReason(str, Enum):  # noqa: UP042 — StrEnum not available in Python 3.11 standard lib at pinned version
    SUCCESS = "success"
    BOT_HISTORY_RESTRICTED = "bot_history_restricted"
    USER_AGENT_INVITE_FAILED = "user_agent_invite_failed"
    TOO_MANY_BOTS = "too_many_bots"
    WEBHOOK_CONFLICT = "webhook_conflict"
    NETWORK_DISCONNECT = "network_disconnect"
    TIMEOUT = "timeout"
    FLOOD_WAIT = "flood_wait"
    FORBIDDEN = "forbidden"
    BAD_REQUEST = "bad_request"
    NO_ACCESSIBLE_UPDATES = "no_accessible_updates"
    NO_NEW_MESSAGES = "no_new_messages"


NEXT_ACTION_BY_REASON = {
    ScrapeReason.SUCCESS: "persist_messages",
    ScrapeReason.BOT_HISTORY_RESTRICTED: "try_live_updates_or_user_agent",
    ScrapeReason.USER_AGENT_INVITE_FAILED: "retry_when_user_agent_available",
    ScrapeReason.TOO_MANY_BOTS: "manual_chat_cleanup_required",
    ScrapeReason.WEBHOOK_CONFLICT: "inspect_webhook_owner_or_enable_delete_policy",
    ScrapeReason.NETWORK_DISCONNECT: "retry_with_backoff",
    ScrapeReason.TIMEOUT: "retry_with_longer_timeout",
    ScrapeReason.FLOOD_WAIT: "retry_after_flood_wait",
    ScrapeReason.FORBIDDEN: "verify_bot_membership_or_token",
    ScrapeReason.BAD_REQUEST: "verify_chat_id_and_token",
    ScrapeReason.NO_ACCESSIBLE_UPDATES: "wait_for_live_update_or_use_user_agent",
    ScrapeReason.NO_NEW_MESSAGES: "no_action",
}


@dataclass(slots=True)
class StrategyAttempt:
    name: str
    success: bool = False
    message_count: int = 0
    reason: str | ScrapeReason | None = None
    retryable: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        reason = data.get("reason")
        if isinstance(reason, ScrapeReason):
            data["reason"] = reason.value
        return data


@dataclass
class ScrapeResult(Sequence[dict]):
    messages: list[dict]
    reason: str | ScrapeReason
    retryable: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)
    strategy_attempts: list[StrategyAttempt | dict[str, Any]] = field(default_factory=list)
    next_action: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.reason, ScrapeReason):
            reason = self.reason
        else:
            try:
                reason = ScrapeReason(self.reason)
            except ValueError:
                reason = None
        if self.next_action is None and reason is not None:
            self.next_action = NEXT_ACTION_BY_REASON.get(reason)

    def __iter__(self) -> Iterator[dict]:
        return iter(self.messages)

    def __len__(self) -> int:
        return len(self.messages)

    def __getitem__(self, index):
        return self.messages[index]

    def __bool__(self) -> bool:
        return bool(self.messages)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return self.messages == other
        if isinstance(other, ScrapeResult):
            return (
                self.messages == other.messages
                and self.reason_code == other.reason_code
                and self.retryable == other.retryable
            )
        return False

    @property
    def reason_code(self) -> str:
        return self.reason.value if isinstance(self.reason, ScrapeReason) else str(self.reason)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "last_scrape_reason": self.reason_code,
            "last_scrape_retryable": self.retryable,
            "last_scrape_evidence": self.evidence,
            "last_scrape_strategy_attempts": [
                attempt.to_dict()
                if isinstance(attempt, StrategyAttempt)
                else _jsonable_attempt(attempt)
                for attempt in self.strategy_attempts
            ],
            "last_scrape_next_action": self.next_action,
        }


def _jsonable_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    out = dict(attempt)
    reason = out.get("reason")
    if isinstance(reason, ScrapeReason):
        out["reason"] = reason.value
    return out


class ScrapeResultClassifier:
    TERMINAL_PRIORITY = (
        ScrapeReason.WEBHOOK_CONFLICT,
        ScrapeReason.TOO_MANY_BOTS,
        ScrapeReason.BOT_HISTORY_RESTRICTED,
        ScrapeReason.USER_AGENT_INVITE_FAILED,
        ScrapeReason.FORBIDDEN,
        ScrapeReason.BAD_REQUEST,
    )
    TRANSIENT_PRIORITY = (
        ScrapeReason.FLOOD_WAIT,
        ScrapeReason.TIMEOUT,
        ScrapeReason.NETWORK_DISCONNECT,
    )

    def classify_exception(
        self,
        exc: BaseException,
        *,
        strategy: str,
        evidence: dict[str, Any] | None = None,
    ) -> StrategyAttempt:
        reason, retryable, next_evidence = self._reason_from_exception(exc)
        merged_evidence = {
            "exception_type": exc.__class__.__name__,
            "exception": str(exc)[:500],
        }
        if next_evidence:
            merged_evidence.update(next_evidence)
        if evidence:
            merged_evidence.update(evidence)
        return StrategyAttempt(
            name=strategy,
            success=False,
            reason=reason,
            retryable=retryable,
            evidence=merged_evidence,
        )

    def result_from_attempts(
        self,
        messages: list[dict],
        attempts: list[StrategyAttempt | dict[str, Any]],
        *,
        evidence: dict[str, Any] | None = None,
    ) -> ScrapeResult:
        if messages:
            return ScrapeResult(
                messages=messages,
                reason=ScrapeReason.SUCCESS,
                retryable=False,
                evidence={**(evidence or {}), "message_count": len(messages)},
                strategy_attempts=attempts,
            )

        normalized = [_attempt_to_dict(attempt) for attempt in attempts]
        for reason in (*self.TERMINAL_PRIORITY, *self.TRANSIENT_PRIORITY):
            matched = [a for a in normalized if a.get("reason") == reason.value]
            if matched:
                retryable = any(bool(a.get("retryable")) for a in matched)
                return ScrapeResult(
                    messages=[],
                    reason=reason,
                    retryable=retryable,
                    evidence={**(evidence or {}), "matched_attempts": matched},
                    strategy_attempts=attempts,
                )

        if any(a.get("success") for a in normalized):
            reason = ScrapeReason.NO_NEW_MESSAGES
        elif attempts:
            reason = ScrapeReason.NO_ACCESSIBLE_UPDATES
        else:
            reason = ScrapeReason.NO_NEW_MESSAGES

        return ScrapeResult(
            messages=[],
            reason=reason,
            retryable=False,
            evidence=evidence or {},
            strategy_attempts=attempts,
        )

    def _reason_from_exception(
        self,
        exc: BaseException,
    ) -> tuple[ScrapeReason, bool, dict[str, Any]]:
        text = str(exc).lower()
        class_name = exc.__class__.__name__.lower()
        evidence: dict[str, Any] = {}

        seconds = getattr(exc, "seconds", None)
        if seconds is not None:
            evidence["seconds"] = seconds

        if isinstance(exc, asyncio.TimeoutError) or "timeout" in class_name or "timed out" in text:
            return ScrapeReason.TIMEOUT, True, evidence

        if "floodwait" in class_name or "flood wait" in text or "retry after" in text:
            return ScrapeReason.FLOOD_WAIT, True, evidence

        if "webhook" in text or "terminated by other getupdates request" in text:
            return ScrapeReason.WEBHOOK_CONFLICT, False, evidence

        if "too many bots" in text or "bots in this chat" in text:
            return ScrapeReason.TOO_MANY_BOTS, False, evidence

        if (
            "chatadminrequired" in class_name
            or "chat admin required" in text
            or "api access for bot users is restricted" in text
        ):
            return ScrapeReason.BOT_HISTORY_RESTRICTED, False, evidence

        if "forbidden" in class_name or "forbidden" in text or "bot was kicked" in text:
            return ScrapeReason.FORBIDDEN, False, evidence

        if "badrequest" in class_name or "bad request" in text or "400" in text:
            return ScrapeReason.BAD_REQUEST, False, evidence

        network_terms = (
            "network",
            "disconnect",
            "connection",
            "connecterror",
            "readerror",
            "writeerror",
            "remoteprotocol",
        )
        if any(term in class_name or term in text for term in network_terms):
            return ScrapeReason.NETWORK_DISCONNECT, True, evidence

        return ScrapeReason.NETWORK_DISCONNECT, True, evidence


def _attempt_to_dict(attempt: StrategyAttempt | dict[str, Any]) -> dict[str, Any]:
    if isinstance(attempt, StrategyAttempt):
        return attempt.to_dict()
    if is_dataclass(attempt):
        return asdict(attempt)
    return dict(attempt)
