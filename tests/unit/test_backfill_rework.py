from scripts.ops.backfill_rework import extract_full_text, source_label_patch


def test_extract_full_text_message():
    payload = {"update_id": 1, "message": {"message_id": 2, "text": "full text"}}
    assert extract_full_text(payload) == "full text"


def test_extract_full_text_caption():
    payload = {"update_id": 1, "channel_post": {"message_id": 2, "caption": "cap"}}
    assert extract_full_text(payload) == "cap"


def test_extract_full_text_callback():
    payload = {"update_id": 1, "callback_query": {"id": "c", "data": "/pay"}}
    assert extract_full_text(payload) == "[Callback Query] /pay"


def test_extract_full_text_missing():
    assert extract_full_text({"update_id": 1}) is None
    assert extract_full_text(None) is None


def test_source_label_patch_from_credential():
    credential = {"chat_type": "group", "chat_name": "My Group", "meta": {}}
    patch = source_label_patch(credential, {"chat_id": -1})
    assert patch["source_chat_type"] == "group"
    assert patch["source_chat_title"] == "My Group"


def test_source_label_patch_from_all_chats():
    credential = {
        "chat_type": None,
        "chat_name": None,
        "meta": {
            "all_chats": [
                {"chat_id": 1, "chat_type": "channel", "chat_name": "Chan"},
                {"chat_id": 2, "chat_type": "group", "chat_name": "Grp"},
            ]
        },
    }
    patch = source_label_patch(credential, {"chat_id": 2})
    assert patch["source_chat_type"] == "group"
    assert patch["source_chat_title"] == "Grp"


def test_source_label_patch_unknown():
    patch = source_label_patch(None, {})
    assert patch["source_chat_type"] == "unknown"
    assert patch["source_chat_title"] is None
