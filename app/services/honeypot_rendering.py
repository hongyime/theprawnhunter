"""Display-only descriptions; never change the stored honeypot record."""

from collections.abc import Mapping
from typing import Final, TypeAlias

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)

_DESCRIPTIONS: Final = {
    "forum_topic_created": "Forum topic created",
    "forum_topic_edited": "Forum topic updated",
    "forum_topic_closed": "Forum topic closed",
    "forum_topic_reopened": "Forum topic reopened",
    "general_forum_topic_hidden": "General forum topic hidden",
    "general_forum_topic_unhidden": "General forum topic shown",
    "new_chat_members": "New members joined the chat",
    "left_chat_member": "A member left or was removed from the chat",
    "pinned_message": "Message pinned",
    "new_chat_title": "Chat title changed",
    "new_chat_photo": "Chat photo changed",
    "delete_chat_photo": "Chat photo removed",
    "group_chat_created": "Group created",
    "supergroup_chat_created": "Supergroup created",
    "channel_chat_created": "Channel created",
    "migrate_to_chat_id": "Group upgraded to a supergroup",
    "migrate_from_chat_id": "Group upgraded to a supergroup",
    "message_auto_delete_timer_changed": "Message auto-delete timer changed",
    "video_chat_scheduled": "Video chat scheduled",
    "video_chat_started": "Video chat started",
    "video_chat_ended": "Video chat ended",
    "video_chat_participants_invited": "Members invited to a video chat",
    "successful_payment": "Payment completed",
    "refunded_payment": "Payment refunded",
    "users_shared": "User details shared",
    "chat_shared": "Chat details shared",
    "connected_website": "Website connected",
    "write_access_allowed": "Permission to send messages granted",
    "passport_data": "Telegram Passport details shared",
    "proximity_alert_triggered": "Location proximity alert triggered",
    "boost_added": "Chat boost added",
    "chat_background_set": "Chat background changed",
    "giveaway_created": "Giveaway created",
    "giveaway": "Giveaway posted",
    "giveaway_winners": "Giveaway winners announced",
    "giveaway_completed": "Giveaway completed",
    "web_app_data": "Web app data submitted",
    "checklist_tasks_done": "Checklist task status changed",
    "checklist_tasks_added": "Checklist tasks added",
    "paid_message_price_changed": "Paid message price changed",
    "direct_message_price_changed": "Direct message price changed",
    "animation": "Animation shared",
    "venue": "Place shared",
    "location": "Location shared",
    "contact": "Contact shared",
    "poll": "Poll posted",
    "dice": "Dice or game emoji rolled",
    "game": "Game shared",
    "story": "Story shared",
    "paid_media": "Paid media shared",
    "checklist": "Checklist shared",
}


def render_honeypot_broadcast(
    message: Mapping[str, JsonValue], raw_update: Mapping[str, JsonValue]
) -> dict[str, JsonValue]:
    """Return a display copy, preserving text, media handling and stored fields."""
    if message.get("content") or message.get("media_type", "text") != "text":
        return dict(message)

    for key in ("message", "channel_post", "edited_message", "edited_channel_post"):
        target = raw_update.get(key)
        if isinstance(target, dict):
            description = next(
                (description for field, description in _DESCRIPTIONS.items() if field in target),
                "Message received without text or caption",
            )
            return {**message, "content": description}

    return dict(message)
