"""Record and display message URLs without contacting their destinations."""

import re
from collections.abc import Mapping, Sequence
from typing import Final, TypedDict

from app.services.honeypot_rendering import JsonValue
from app.services.message_preview import TextPreview, preview_text, utf16_length

_URL_PATTERN: Final = re.compile(r"\b(?:https?://|www\.)[^\s<>'\"`]+", re.IGNORECASE)


class TextLink(TypedDict):
    text: str
    url: str


class UrlMetadata(TypedDict):
    urls: list[str]
    text_links: list[TextLink]


def extract_message_urls(target: Mapping[str, JsonValue]) -> UrlMetadata:
    urls: list[str] = []
    text_links: list[TextLink] = []
    for text_field, entity_field in (("text", "entities"), ("caption", "caption_entities")):
        text = target.get(text_field)
        if not isinstance(text, str):
            continue
        for match in _URL_PATTERN.finditer(text):
            url = match.group().rstrip(".,;!?")
            for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
                while url.endswith(closing) and url.count(closing) > url.count(opening):
                    url = url[:-1]
            urls.append(url)

        entities = target.get(entity_field)
        if not isinstance(entities, list):
            continue
        encoded = text.encode("utf-16-le", errors="surrogatepass")
        for entity in entities:
            if not isinstance(entity, dict) or entity.get("type") not in ("url", "text_link"):
                continue
            offset, length = entity.get("offset"), entity.get("length")
            if (
                not isinstance(offset, int) or isinstance(offset, bool)
                or not isinstance(length, int) or isinstance(length, bool)
                or offset < 0 or length <= 0 or (offset + length) * 2 > len(encoded)
            ):
                continue
            # Telegram counts entity offsets in UTF-16 units, not Python characters.
            try:
                visible = encoded[offset * 2:(offset + length) * 2].decode("utf-16-le")
            except UnicodeDecodeError:
                continue
            url = entity.get("url") if entity.get("type") == "text_link" else visible
            if not isinstance(url, str) or not url:
                continue
            urls.append(url)
            if entity.get("type") == "text_link":
                link: TextLink = {"text": visible, "url": url}
                if link not in text_links:
                    text_links.append(link)

    return {"urls": list(dict.fromkeys(urls)), "text_links": text_links}


def _shorten(text: str, limit: int) -> str:
    encoded = text.encode("utf-16-le")
    if len(encoded) <= limit * 2:
        return text
    return encoded[:max(0, limit - 3) * 2].decode("utf-16-le", errors="ignore") + "..."


def _link_section(urls: Sequence[str], explicit_evidence: bool = False) -> str:
    if not urls:
        return ""
    shown = [_shorten(url.replace("\r", " ").replace("\n", " "), 160) for url in urls[:3]]
    section = "\n\nLinks:\n" + "\n".join(f"- {url}" for url in shown)
    if len(urls) > len(shown):
        location = "full links in evidence JSON file" if explicit_evidence else "full URLs saved"
        section += f"\n(+{len(urls) - len(shown)} more; {location})"
    elif any(original != displayed for original, displayed in zip(urls, shown, strict=True)):
        section += (
            "\n[Links shortened; full links in evidence JSON file.]"
            if explicit_evidence else "\n(Full URLs saved)"
        )
    return section


def format_link_caption(header: str, content: str, urls: Sequence[str]) -> str:
    section = _link_section(urls)
    reserved = len((header + "\n" + section).encode("utf-16-le")) // 2
    return header + "\n" + _shorten(content, 1024 - reserved) + section


def format_link_preview(header: str, content: TextPreview, urls: Sequence[str]) -> str:
    section = _link_section(urls, explicit_evidence=True)
    available = 1024 - utf16_length(header + "\n" + section)
    return header + "\n" + preview_text(content, available) + section
