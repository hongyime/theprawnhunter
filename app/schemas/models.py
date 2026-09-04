from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CredentialBase(BaseModel):
    source: str
    meta: dict[str, Any] = {}

class CredentialCreate(CredentialBase):
    token: str

class CredentialOut(CredentialBase):
    id: UUID
    chat_id: int | None = None
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Enrichment metadata. Known subkeys: "
            "topic_id (int), bot_username (str), bot_id (str), "
            "all_chats (list), enriched (bool), "
            "capabilities (dict with: can_join_groups, can_read_all_group_messages, "
            "supports_inline_queries, default_admin_rights_groups, "
            "default_admin_rights_channels, description, short_description, linked_chat_id), "
            "last_scrape_at (ISO datetime), last_scrape_reason (str)."
        ),
    )
    status: str
    created_at: datetime
    updated_at: datetime

    # Exclude bot_token from default output for security
    model_config = ConfigDict(from_attributes=True)

class MessageOut(BaseModel):
    id: UUID
    credential_id: UUID
    telegram_msg_id: int
    sender_name: str | None = None
    content: str | None = None
    media_type: str
    is_broadcasted: bool
    broadcast_error: dict[str, Any] | None = None
    broadcast_attempts: int = 0
    next_retry_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class ScanRequest(BaseModel):
    source: str = "shodan"
    query: str

class StatsOut(BaseModel):
    credentials_total: int
    credentials_active: int
    messages_exfiltrated: int
    messages_broadcasted: int


FindingType = Literal[
    "credential_exposure", "infrastructure_cluster", "cross_bot_pattern"
]
FindingStatus = Literal[
    "new", "triaged", "in_progress", "resolved", "dismissed", "suppressed"
]


class FindingOut(BaseModel):
    id: UUID
    type: FindingType
    canonical_key: str
    title: str
    summary: str
    why_it_matters: str
    recommended_action: str
    confidence: float
    severity: Literal["low", "medium", "high", "critical"]
    priority: int
    score_explanation: dict[str, Any]
    status: FindingStatus
    assignee: str | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    evidence_count: int
    material_version: int
    last_material_change_at: datetime
    created_at: datetime
    updated_at: datetime


class FindingEvidenceOut(BaseModel):
    id: UUID
    finding_id: UUID
    evidence_key: str
    evidence_type: str
    source_table: str
    source_id: str
    observed_at: datetime
    weight: float
    excerpt_redacted: str | None = None
    provenance: dict[str, Any]
    message_id: UUID | None = None
    credential_id: UUID | None = None


class FindingDetailOut(FindingOut):
    evidence: list[FindingEvidenceOut] = Field(default_factory=list)


class FindingFeedbackIn(BaseModel):
    label: Literal["useful", "noise", "duplicate", "irrelevant", "actioned"]
    reason_code: Literal["confirmed", "actionable", "false_positive", "duplicate", "out_of_scope", "insufficient_evidence"] | None = None
    note: str | None = Field(default=None, max_length=4000)
    status: FindingStatus | None = None
    assignee: str | None = Field(default=None, max_length=200)
    suppress_pattern: str | None = Field(default=None, max_length=500)


class FindingFeedbackOut(BaseModel):
    feedback_id: UUID
    finding_id: UUID
    status: FindingStatus | None = None


class EngagementLifecycleIn(BaseModel):
    owned_bot_id: int = Field(gt=0)
    subject_reference: str = Field(min_length=1, max_length=256)
    campaign_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    campaign_source: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    event_type: Literal["qualified", "handoff", "outcome", "block_report"]
    qualification_code: str | None = Field(default=None, max_length=100)
    outcome_code: str | None = Field(default=None, max_length=100)
    reason_code: str | None = Field(default=None, max_length=100)
    occurred_at: datetime | None = None
