import asyncio

import asyncio

import base64

import contextlib

import contextlib

import html as htmlmod

import hashlib

import logging

import json

import re

from typing import Any

import logging

import random

import httpx

import re

from datetime import UTC, datetime

from app.core.config import settings

from typing import Any

from app.utils.http_client import get_async_http_client

from urllib.parse import urlparse

import httpx

import urllib3

from app.core.config import settings

from app.domain.tokens import CHAT_ID_PATTERN, TOKEN_PATTERN, _is_valid_token

from app.utils.http_client import (  # noqa: F401 — retry_with_backoff moved here in DEAD-007, re-exported for existing importers
    get_async_http_client,
    retry_with_backoff,
)

logger = logging.getLogger("scanners")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SPOOFED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",  # Derived from "lang": "en-GB"
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chrome";v="143", "Not=A?Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"'
}

ADJACENT_ENDPOINT_REGEX = re.compile(
    r"(https?://[^\s'\"]+)|"
    r"(?:[a-zA-Z0-9_]+_(?:HOST|DOMAIN|URL|IP|SERVER|API|ENDPOINT|PORT)\s*[:=]\s*[\"']?([^\s'\"]+)[\"']?)",
    re.IGNORECASE,
)

LOCAL_ENDPOINT_VALUES = {"127.0.0.1", "0.0.0.0", "localhost", "::1"}

def _token_context(code_text: str, token: str, line_radius: int = 50) -> str:
    lines = (code_text or "").splitlines()
    if not lines:
        return code_text or ""
    for idx, line in enumerate(lines):
        if token in line:
            start = max(0, idx - line_radius)
            end = min(len(lines), idx + line_radius + 1)
            return "\n".join(lines[start:end])
    return code_text or ""

def _is_remote_endpoint(value: str) -> bool:
    if not value:
        return False
    # Strip the delimiter chars that commonly surround URLs in source (quotes,
    # brackets, punctuation). Ruff B005 flags multi-char .strip() as misleading,
    # so use an explicit character-by-character trim via translate.
    clean = value.strip().strip("',\")]}>,")  # noqa: B005 — intentional per-char strip
    if not clean:
        return False
    parsed = urlparse(clean if "://" in clean else f"//{clean}")
    host = parsed.hostname or clean.split("/")[0].split(":")[0]
    if host.lower() in LOCAL_ENDPOINT_VALUES:
        return False
    return "." in host or host.replace(".", "").isdigit() or "/" in clean

def extract_infrastructure_context(
    code_text: str,
    source_meta: dict[str, Any],
    token: str | None = None,
) -> dict[str, Any]:
    context_text = _token_context(code_text, token) if token else (code_text or "")
    endpoints = set()
    for url_match, config_match in ADJACENT_ENDPOINT_REGEX.findall(context_text):
        candidate = (url_match or config_match or "").strip().strip("',\")]}>,")  # noqa: B005 — intentional per-char strip
        if _is_remote_endpoint(candidate):
            endpoints.add(candidate)

    if not endpoints:
        return {}

    return {
        "source_file_path": source_meta.get("path") or source_meta.get("file") or source_meta.get("filename"),
        "repository_uri": source_meta.get("repository") or source_meta.get("repo") or source_meta.get("project_id"),
        "co_located_endpoints": sorted(endpoints),
        "extracted_at": datetime.now(UTC).isoformat(),
    }

async def _perform_active_deep_scan(
    target_url: str,
    client: httpx.AsyncClient = None,
    on_error=None,
) -> list[dict[str, str]]:
    """
    Connects to a URL, scans HTML body, finds <script src="..."> tags,
    fetches those JS files, and scans them too.
    Returns a list of dicts: [{'token': '...', 'chat_id': '...'}]
    """
    found_results = [] # List of {'token': ..., 'chat_id': ...}

    def report_error(failed_url: str, error: Exception):
        if on_error is not None:
            try:
                on_error(failed_url, error)
                return
            except Exception as callback_error:
                logger.warning("Active scan error callback failed: %s", type(callback_error).__name__)
        logger.debug("[suppressed] %s", error)

    def extract_from_text(text: str) -> list[dict[str, str]]:
        tokens = TOKEN_PATTERN.findall(text)
        chat_ids = CHAT_ID_PATTERN.findall(text)

        extracted = []
        if len(text) < 500:
             cid = chat_ids[0] if chat_ids else None
             for t in tokens:
                 if _is_valid_token(t):
                     extracted.append({'token': t, 'chat_id': cid})
             return extracted

        for t in set(tokens): # dedup tokens
             if _is_valid_token(t):
                 cid = chat_ids[0] if chat_ids else None
                 extracted.append({'token': t, 'chat_id': cid})
        return extracted

    # Use provided client or create a temporary one
    should_close = False
    if client is None:
        client = httpx.AsyncClient(verify=False, timeout=10.0)
        should_close = True

    try:
        # 0. Check URL string itself
        found_results.extend(extract_from_text(target_url))

        if "api.telegram.org" in target_url:
            if should_close:
                await client.aclose()
            return found_results

        # 1. Fetch Main HTML
        # print(f"      [DeepScan] Fetching: {target_url}")
        try:
            res = await client.get(target_url, headers=SPOOFED_HEADERS, follow_redirects=True)
            if res.status_code == 200:
                html_content = res.text
                found_results.extend(extract_from_text(html_content))

                # 2. Find External JS
                js_links = re.findall(r'src=["\'](.*?.js)["\']', html_content)
                unique_js = list(set(js_links))[:5]

                # Create async tasks for JS fetching
                js_tasks = []
                js_urls = []
                for js_path in unique_js:
                    if js_path.startswith("//"):
                        js_url = "https:" + js_path
                    elif js_path.startswith("http"):
                        js_url = js_path
                    else:
                        from urllib.parse import urljoin
                        js_url = urljoin(target_url, js_path)

                    js_tasks.append(client.get(js_url, headers=SPOOFED_HEADERS, follow_redirects=True))
                    js_urls.append(js_url)

                if js_tasks:
                    js_responses = await asyncio.gather(*js_tasks, return_exceptions=True)
                    for js_url, js_res in zip(js_urls, js_responses):
                        if isinstance(js_res, Exception):
                            report_error(js_url, js_res)
                        elif isinstance(js_res, httpx.Response) and js_res.status_code == 200:
                            found_results.extend(extract_from_text(js_res.text))
                        elif isinstance(js_res, httpx.Response):
                            report_error(js_url, RuntimeError(f"HTTP {js_res.status_code}"))
            else:
                report_error(target_url, RuntimeError(f"HTTP {res.status_code}"))

        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as error:
            report_error(target_url, error)
        except Exception as error:
            report_error(target_url, error)

        # Deduplicate
        final_map = {}
        for item in found_results:
            t = item['token']
            c = item['chat_id']
            if t not in final_map or not final_map[t] and c:
                final_map[t] = c

        return [{'token': t, 'chat_id': c} for t, c in final_map.items()]

    except Exception as error:
        report_error(target_url, error)
        return []
    finally:
        if should_close:
            await client.aclose()

__all__ = ['ADJACENT_ENDPOINT_REGEX', 'Any', 'CHAT_ID_PATTERN', 'LOCAL_ENDPOINT_VALUES', 'SPOOFED_HEADERS', 'TOKEN_PATTERN', 'UTC', '_is_remote_endpoint', '_is_valid_token', '_perform_active_deep_scan', '_token_context', 'asyncio', 'base64', 'contextlib', 'datetime', 'extract_infrastructure_context', 'get_async_http_client', 'hashlib', 'htmlmod', 'httpx', 'json', 'logger', 'logging', 'random', 're', 'retry_with_backoff', 'settings', 'urllib3', 'urlparse']
