from app.domain.media import bot_api_media_info


def test_animation_extraction():
    payload = {
        "animation": {
            "file_id": "anim-id",
            "file_unique_id": "anim-uid",
            "file_name": "clip.mp4",
            "mime_type": "video/mp4",
            "file_size": 123,
            "duration": 3,
            "width": 100,
            "height": 100,
        }
    }
    media_type, meta = bot_api_media_info(payload)
    assert media_type == "animation"
    assert meta["file_id"] == "anim-id"
    assert meta["duration"] == 3


def test_paid_media_extraction():
    payload = {
        "paid_media": {
            "star_count": 100,
            "paid_media_items": [
                {"type": "preview", "width": 100, "height": 200, "duration": 5},
                {"type": "video", "file_id": "vid-id"},
            ],
        }
    }
    media_type, meta = bot_api_media_info(payload)
    assert media_type == "paid_media"
    assert meta["paid_media_star_count"] == 100
    assert len(meta["paid_media_items"]) == 2


def test_story_extraction():
    payload = {"story": {"id": 7, "chat": {"id": 123}}}
    media_type, meta = bot_api_media_info(payload)
    assert media_type == "story"
    assert meta["story_id"] == 7
    assert meta["story_chat_id"] == 123


def test_existing_photo_priority_kept():
    payload = {
        "photo": [{"file_id": "ph"}],
        "animation": {"file_id": "anim"},
    }
    media_type, meta = bot_api_media_info(payload)
    assert media_type == "photo"
    assert meta["file_id"] == "ph"


def test_plain_text_fallback():
    media_type, meta = bot_api_media_info({"text": "hi"})
    assert media_type == "text"
    assert meta["source"] == "bot_api"
