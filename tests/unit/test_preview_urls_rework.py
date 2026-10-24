from app.services.honeypot_urls import (
    _link_section,
    extract_message_urls,
    format_link_caption,
    format_link_preview,
)
from app.services.message_preview import (
    TEXT_PREVIEW_VERSION,
    TextPreview,
    prepare_stored_content,
    utf16_length,
)


def test_prepare_stored_content_stores_full_text():
    text = "a" * 10000
    stored, meta = prepare_stored_content(text)
    assert stored == text
    assert meta["content_truncated"] is False
    assert meta["content_original_length"] == len(text)
    assert meta["text_preview_version"] == TEXT_PREVIEW_VERSION
    assert TEXT_PREVIEW_VERSION == 2


def test_link_section_lists_all_urls_fully():
    urls = [
        "https://example.com/" + "x" * 200,
        "https://example.org/path",
    ]
    section = _link_section(urls)
    assert section.startswith("\n\nLinks:\n")
    assert "- https://example.com/" + "x" * 200 in section
    assert "- https://example.org/path" in section


def test_format_link_caption_no_truncation():
    header = "[ID: 1] [From: u]"
    content = "c" * 5000
    urls = ["https://example.com/very/long/path"]
    result = format_link_caption(header, content, urls)
    assert content in result
    assert "https://example.com/very/long/path" in result
    assert result.startswith(header + "\n")


def test_format_link_preview_no_truncation():
    header = "[ID: 1] [From: u]"
    content = "c" * 5000
    result = format_link_preview(header, TextPreview(content), ["https://example.com/x"])
    assert content in result
    assert "https://example.com/x" in result


def test_extract_message_urls_unchanged():
    target = {"text": "see https://example.com and https://example.org ok"}
    meta = extract_message_urls(target)
    assert meta["urls"] == ["https://example.com", "https://example.org"]


def test_utf16_length_ascii():
    assert utf16_length("abcd") == 4
