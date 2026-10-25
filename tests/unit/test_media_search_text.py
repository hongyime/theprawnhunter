"""Tests for the shared searchable-media-text helper."""

from __future__ import annotations

from app.services.media_text import (
    MAX_SEARCHABLE_MEDIA_TEXT_CHARS,
    searchable_media_text,
)


def test_includes_content_transcript_and_ocr_in_order():
    text = searchable_media_text(
        "caption",
        {"transcript_text": "spoken words", "ocr_text": "printed words"},
    )

    assert text == "caption\nspoken words\nprinted words"


def test_content_only_is_unchanged_apart_from_trimming():
    assert searchable_media_text("  hello  ", None) == "hello"
    assert searchable_media_text("hello", {}) == "hello"


def test_skips_empty_and_missing_media_text():
    text = searchable_media_text(
        "caption",
        {
            "transcript_text": "   ",
            "ocr_text": "",
            "thumb_path": "ignored",
        },
    )

    assert text == "caption"


def test_transcript_only_without_content():
    assert searchable_media_text(None, {"transcript_text": "voice"}) == "voice"
    assert searchable_media_text("", {"ocr_text": "image text"}) == "image text"


def test_handles_none_empty_and_non_dict_meta():
    assert searchable_media_text(None, None) == ""
    assert searchable_media_text("", {}) == ""
    assert searchable_media_text(None, "not-a-dict") == ""  # type: ignore[arg-type]
    assert searchable_media_text(None, ["transcript_text"]) == ""  # type: ignore[arg-type]


def test_ignores_non_string_values_without_raising():
    text = searchable_media_text(
        "caption",
        {"transcript_text": 123, "ocr_text": {"nested": True}},
    )

    assert text == "caption"


def test_output_is_bounded():
    content = "a" * (MAX_SEARCHABLE_MEDIA_TEXT_CHARS + 500)
    text = searchable_media_text(content, {"transcript_text": "tail"})

    assert len(text) == MAX_SEARCHABLE_MEDIA_TEXT_CHARS
    assert text == content[:MAX_SEARCHABLE_MEDIA_TEXT_CHARS]


def test_is_stable_for_the_same_input():
    meta = {"transcript_text": "same", "ocr_text": "same2"}

    assert searchable_media_text("caption", meta) == searchable_media_text("caption", meta)
