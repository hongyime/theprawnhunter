from app.services.membership_srv import (
    build_all_chats_merge,
    extract_membership_update,
)


def test_extract_join_update():
    payload = {
        "update_id": 1,
        "my_chat_member": {
            "chat": {"id": -100456, "type": "supergroup", "title": "Victim Group"},
            "new_chat_member": {"status": "member"},
            "old_chat_member": {"status": "left"},
            "date": 1000,
        },
    }
    update = extract_membership_update(payload, "cred-id")
    assert update["action"] == "joined"
    assert update["chat_id"] == -100456
    assert update["chat_title"] == "Victim Group"


def test_extract_left_update():
    payload = {
        "update_id": 1,
        "my_chat_member": {
            "chat": {"id": -100456, "type": "group"},
            "new_chat_member": {"status": "kicked"},
            "old_chat_member": {"status": "member"},
        },
    }
    update = extract_membership_update(payload, "cred-id")
    assert update["action"] == "left"


def test_extract_monitor_group_ignored():
    payload = {
        "update_id": 1,
        "my_chat_member": {
            "chat": {"id": -100123, "type": "supergroup"},
            "new_chat_member": {"status": "member"},
            "old_chat_member": {"status": "left"},
        },
    }
    assert extract_membership_update(payload, "cred-id") is None


def test_all_chats_merge_join_then_remove():
    update = {
        "action": "joined",
        "chat_id": -100456,
        "chat_type": "supergroup",
        "chat_title": "G",
    }
    meta = build_all_chats_merge({}, update)
    assert meta["all_chats"][0]["status"] == "present"
    assert meta["membership_updated_at"]

    meta = build_all_chats_merge(meta, {**update, "action": "left"})
    assert meta["all_chats"][0]["status"] == "removed"
    assert meta["all_chats"][0]["removed_at"]
    assert len(meta["all_chats"]) == 1
