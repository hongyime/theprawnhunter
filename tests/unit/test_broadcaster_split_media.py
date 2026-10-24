from app.services.broadcaster_srv import (
    ARCHIVE_MEDIA_TYPES,
    _cap_utf16,
    _source_label,
    _split_utf16_text,
)


def test_animation_in_archive_media_types():
    assert "animation" in ARCHIVE_MEDIA_TYPES


def test_split_short_text_single_part():
    parts = _split_utf16_text("hello world", limit=4096)
    assert parts == ["hello world"]


def test_split_long_text_numbered():
    text = "word " * 2000
    parts = _split_utf16_text(text, limit=200)
    assert len(parts) > 1
    assert parts[0].endswith(f"(1/{len(parts)})")
    assert parts[-1].endswith(f"({len(parts)}/{len(parts)})")
    for part in parts:
        assert len(part.encode("utf-16-le")) // 2 <= 200 + 16


def test_split_oversized_single_word():
    parts = _split_utf16_text("x" * 500, limit=200)
    assert len(parts) >= 3
    for part in parts:
        assert len(part.encode("utf-16-le")) // 2 <= 216


def test_source_label_variants():
    assert _source_label({"source_chat_type": "private"}) == "[DM]"
    assert _source_label({"source_chat_type": "group", "source_chat_title": "T"}) == "[Group: T]"
    assert _source_label({"source_chat_type": "channel"}) == "[Channel]"
    assert _source_label({}) == ""


def test_cap_utf16_short_text_untouched():
    assert _cap_utf16("abc", 1024) == "abc"


def test_cap_utf16_truncates_long():
    result = _cap_utf16("a" * 2000, 100)
    units = len(result.encode("utf-16-le")) // 2
    assert units <= 100
    assert result.endswith("...")
