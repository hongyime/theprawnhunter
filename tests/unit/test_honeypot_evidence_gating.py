from app.workers.tasks.flows.honeypot import _needs_individual_evidence


def test_media_type_non_text_needs_evidence():
    assert _needs_individual_evidence({"media_type": "photo"}, {}) is True
    assert _needs_individual_evidence({"media_type": "animation"}, {}) is True
    assert _needs_individual_evidence({"media_type": "callback_query"}, {}) is True
    assert _needs_individual_evidence({"media_type": "paid_media"}, {}) is True


def test_plain_text_without_context_defers():
    raw = {"update_id": 1, "message": {"message_id": 1, "text": "hi", "chat": {"id": 1}}}
    assert _needs_individual_evidence({"media_type": "text"}, raw) is False


def test_plain_text_with_reply_needs_evidence():
    raw = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "text": "hi",
            "chat": {"id": 1},
            "reply_to_message": {"message_id": 0},
        },
    }
    assert _needs_individual_evidence({"media_type": "text"}, raw) is True


def test_plain_text_with_forward_needs_evidence():
    raw = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "text": "hi",
            "chat": {"id": 1},
            "forward_from_message_id": 5,
        },
    }
    assert _needs_individual_evidence({"media_type": "text"}, raw) is True


def test_missing_target_defers():
    assert _needs_individual_evidence({"media_type": "text"}, {"update_id": 1}) is False
