from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, Dict, Any
from datetime import datetime
from uuid import UUID

class CredentialBase(BaseModel):
    source: str
    meta: Dict[str, Any] = {}

class CredentialCreate(CredentialBase):
    token: str

class CredentialOut(CredentialBase):
    id: UUID
    chat_id: Optional[int] = None
    meta: Dict[str, Any] = Field(
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
    sender_name: Optional[str] = None
    content: Optional[str] = None
    media_type: str
    is_broadcasted: bool
    broadcast_error: Optional[Dict[str, Any]] = None
    broadcast_attempts: int = 0
    next_retry_at: Optional[datetime] = None
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
