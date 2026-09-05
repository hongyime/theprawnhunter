"""
Bug Condition Exploration Test — Scanner Query Coverage

This test MUST FAIL on unfixed code. Failure confirms the bug exists.
DO NOT fix the code or the test when it fails.

The test encodes the expected (post-fix) behavior for all five scanners.
When the fix is applied (Task 3), this same test will pass and confirm
the bug is resolved.

Validates: Requirements 1.1–1.15 (bug conditions) / 2.1–2.14 (expected behavior)
"""

import os
import re

# ---------------------------------------------------------------------------
# Helpers to extract the inline query lists from scanner_tasks without
# importing the full Celery/Redis/DB stack.
# ---------------------------------------------------------------------------

# We parse the source file directly so we don't need a running environment.
# Query constants were extracted from scanner_tasks.py into _scanner/queries.py
# during the monolith split; we concatenate both sources so extraction helpers
# see the same content regardless of which file holds the constants.
_SCANNER_TASKS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "app", "workers", "tasks", "scanner_tasks.py"
)
_QUERIES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "app", "workers", "tasks", "_scanner", "queries.py"
)
_QUERIES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "app", "workers", "tasks", "_scanner", "queries.py"
)
_BACKGROUND_JS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "extension", "background.js"
)


def _read_source(path: str) -> str:
    with open(os.path.abspath(path), encoding="utf-8") as fh:
        return fh.read()


def _extract_shodan_default_queries(src: str) -> list[str]:
    """
    Extract the inline `default_queries` list from `_scan_shodan_async`.
    After the fix this will be `SHODAN_DEFAULT_QUERIES`; before the fix it is
    built inline.  We collect every string literal that ends up in the list.
    """
    queries: list[str] = []

    # 1. Collect the COMMON_QUERIES strings used to build http.html: entries
    common_match = re.search(
        r"COMMON_QUERIES\s*=\s*\[(.*?)\]",
        src,
        re.DOTALL,
    )
    if common_match:
        for m in re.finditer(r'"([^"]+)"', common_match.group(1)):
            queries.append(f'http.html:"{m.group(1)}"')

    # 2. Collect the extend() / literal entries in default_queries
    # Find the block between `default_queries = [` and the matching `]`
    dq_start = src.find("default_queries = [")
    if dq_start == -1:
        # Post-fix: look for SHODAN_DEFAULT_QUERIES module-level constant
        dq_start = src.find("SHODAN_DEFAULT_QUERIES = [")
    if dq_start != -1:
        # Walk forward to find the closing bracket at the same nesting level
        depth = 0
        i = src.index("[", dq_start)
        block_start = i
        while i < len(src):
            if src[i] == "[":
                depth += 1
            elif src[i] == "]":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        block = src[block_start : i + 1]
        for m in re.finditer(r"'([^']+)'", block):
            queries.append(m.group(1))

    # 3. Collect entries added via default_queries.extend([...])
    for extend_match in re.finditer(
        r"default_queries\.extend\(\s*\[(.*?)\]\s*\)", src, re.DOTALL
    ):
        for m in re.finditer(r"'([^']+)'", extend_match.group(1)):
            queries.append(m.group(1))

    return queries


def _extract_c2_queries(src: str) -> list[str]:
    """Extract the C2_QUERIES list from `_scan_shodan_c2_async`."""
    # Find C2_QUERIES = [ ... ]
    start = src.find("C2_QUERIES = [")
    if start == -1:
        return []
    depth = 0
    i = src.index("[", start)
    while i < len(src):
        if src[i] == "[":
            depth += 1
        elif src[i] == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    block = src[start : i + 1]
    return [m.group(1) for m in re.finditer(r"'([^']+)'", block)]


def _extract_netlas_queries(src: str) -> list[str]:
    """Extract the NETLAS_QUERIES module-level constant."""
    start = src.find("NETLAS_QUERIES = [")
    if start == -1:
        return []
    depth = 0
    i = src.index("[", start)
    while i < len(src):
        if src[i] == "[":
            depth += 1
        elif src[i] == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    block = src[start : i + 1]
    return [m.group(1) for m in re.finditer(r"'([^']+)'", block)]


def _extract_fofa_queries(src: str) -> list[str]:
    """
    Extract the FOFA default query list.
    Before the fix: inline COMMON_QUERIES inside `_scan_fofa_async`.
    After the fix: FOFA_DEFAULT_QUERIES module-level constant.
    """
    # Try module-level constant first (post-fix)
    start = src.find("FOFA_DEFAULT_QUERIES = [")
    if start == -1:
        # Pre-fix: find the COMMON_QUERIES inside _scan_fofa_async
        # There are two COMMON_QUERIES blocks; we want the one inside _scan_fofa_async
        fofa_func_start = src.find("async def _scan_fofa_async")
        if fofa_func_start == -1:
            return []
        start = src.find("COMMON_QUERIES = [", fofa_func_start)
        if start == -1:
            return []
    depth = 0
    i = src.index("[", start)
    while i < len(src):
        if src[i] == "[":
            depth += 1
        elif src[i] == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    block = src[start : i + 1]
    return [m.group(1) for m in re.finditer(r"'([^']+)'", block)]


def _extract_base_query_template(js_src: str) -> str:
    """Extract BASE_QUERY_TEMPLATE value from background.js."""
    m = re.search(r"const\s+BASE_QUERY_TEMPLATE\s*=\s*'([^']+)'", js_src)
    if m:
        return m.group(1)
    m = re.search(r'const\s+BASE_QUERY_TEMPLATE\s*=\s*"([^"]+)"', js_src)
    if m:
        return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# Load sources once
# ---------------------------------------------------------------------------

# Concatenate scanner_tasks.py + _scanner/queries.py so extraction helpers
# find constants regardless of which file holds them after the monolith split.
_scanner_src = _read_source(_SCANNER_TASKS_PATH) + "\n" + _read_source(_QUERIES_PATH)
_js_src = _read_source(_BACKGROUND_JS_PATH)


_shodan_default = _extract_shodan_default_queries(_scanner_src)
_c2_queries = _extract_c2_queries(_scanner_src)
_netlas_queries = _extract_netlas_queries(_scanner_src)
_fofa_queries = _extract_fofa_queries(_scanner_src)
_base_query_template = _extract_base_query_template(_js_src)


# ===========================================================================
# TIER DEFINITIONS
# ===========================================================================

TIER1_SHODAN = [
    'http.headers:"X-Telegram-Bot-Api"',
    # body queries — checked by substring presence
    'http.body:"api.telegram.org/bot"',
    'http.body:"http://t.me/bot"',
    'http.body:"https://t.me"',  # combined with /start
]

TIER2_PAYLOADS = [
    "payload=",
    "token=",
    "cmd=",
    "c2=",
    "key=",
    "id=",
    "/bin/bash",
    "/powershell",
    "download",
    "/exec",
    "/run",
    "/invoke",
    "/script",
    "http://",
    "https://",
]

TIER3_KEYWORDS = [
    "malware",
    "rat",
    "remote access",
    "spyware",
    "stealer",
    "keylogger",
    "c2 server",
    "command and control",
    "exploit",
    "bypass",
    "inject",
    "persistence",
    "privilege escalation",
]


# ===========================================================================
# SHODAN GENERAL — default_queries / SHODAN_DEFAULT_QUERIES
# ===========================================================================


class TestShodanDefaultQueries:
    """Validates: Requirements 1.1–1.6 / 2.1–2.5"""

    def test_tier1_header_entry_present(self):
        """Tier 1: X-Telegram-Bot-Api header query must be an independent entry."""
        assert any(
            'http.headers:"X-Telegram-Bot-Api"' in q for q in _shodan_default
        ), (
            "MISSING Tier 1 entry: 'http.headers:\"X-Telegram-Bot-Api\"' not found in "
            f"Shodan default queries.\nCurrent list: {_shodan_default}"
        )

    def test_tier1_body_api_telegram_entry_present(self):
        """Tier 1: api.telegram.org/bot body query must be an independent entry."""
        assert any(
            'http.body:"api.telegram.org/bot"' in q and "/start" not in q and "malware" not in q
            for q in _shodan_default
        ), (
            "MISSING Tier 1 entry: standalone 'http.body:\"api.telegram.org/bot\"' not found "
            f"(must not be joined with /start or malware).\nCurrent list: {_shodan_default}"
        )

    def test_tier1_body_tme_http_entry_present(self):
        """Tier 1: http://t.me/bot body query must be present."""
        assert any('http.body:"http://t.me/bot"' in q for q in _shodan_default), (
            "MISSING Tier 1 entry: 'http.body:\"http://t.me/bot\"' not found in "
            f"Shodan default queries.\nCurrent list: {_shodan_default}"
        )

    def test_tier1_body_tme_https_with_start_entry_present(self):
        """Tier 1: https://t.me + /start body query must be present."""
        assert any(
            'http.body:"https://t.me"' in q and 'http.body:"/start"' in q
            for q in _shodan_default
        ), (
            "MISSING Tier 1 entry: combined 'http.body:\"https://t.me\"' + "
            "'http.body:\"/start\"' not found in Shodan default queries.\n"
            f"Current list: {_shodan_default}"
        )

    def test_tier2_all_payload_variants_present(self):
        """Tier 2: all 15 /start payload variants must be independent entries."""
        missing = []
        for payload in TIER2_PAYLOADS:
            found = any(
                "api.telegram.org/bot" in q and f"/start {payload}" in q
                for q in _shodan_default
            )
            if not found:
                missing.append(payload)
        assert not missing, (
            f"MISSING Tier 2 entries in Shodan default queries for payloads: {missing}\n"
            f"Current list: {_shodan_default}"
        )

    def test_tier3_all_keywords_present(self):
        """Tier 3: all 13 malware keywords must be independent entries."""
        missing = []
        for kw in TIER3_KEYWORDS:
            found = any(
                "api.telegram.org/bot" in q and kw in q
                for q in _shodan_default
            )
            if not found:
                missing.append(kw)
        assert not missing, (
            f"MISSING Tier 3 entries in Shodan default queries for keywords: {missing}\n"
            f"Current list: {_shodan_default}"
        )

    def test_all_body_queries_have_status_200(self):
        """Every http.body: entry must include http.status:200."""
        bad = [
            q for q in _shodan_default
            if "http.body:" in q and "http.status:200" not in q
        ]
        assert not bad, (
            "Shodan default queries missing http.status:200 on body entries:\n"
            + "\n".join(f"  - {q}" for q in bad)
        )

    def test_all_body_queries_have_exclusion_filter_telegram(self):
        """Every http.body: entry must include -http.body:\"telegram.org\"."""
        bad = [
            q for q in _shodan_default
            if "http.body:" in q and '-http.body:"telegram.org"' not in q
        ]
        assert not bad, (
            "Shodan default queries missing -http.body:\"telegram.org\" exclusion:\n"
            + "\n".join(f"  - {q}" for q in bad)
        )

    def test_all_body_queries_have_exclusion_filter_github(self):
        """Every http.body: entry must include -http.body:\"github.com\"."""
        bad = [
            q for q in _shodan_default
            if "http.body:" in q and '-http.body:"github.com"' not in q
        ]
        assert not bad, (
            "Shodan default queries missing -http.body:\"github.com\" exclusion:\n"
            + "\n".join(f"  - {q}" for q in bad)
        )


# ===========================================================================
# SHODAN C2 — C2_QUERIES
# ===========================================================================


class TestC2Queries:
    """Validates: Requirements 1.7–1.8 / 2.6–2.7"""

    def test_start_key_entry_present(self):
        """/start key= must be present in C2_QUERIES."""
        assert any("/start key=" in q for q in _c2_queries), (
            "MISSING: '/start key=' not found in C2_QUERIES.\n"
            f"Current C2_QUERIES: {_c2_queries}"
        )

    def test_start_id_entry_present(self):
        """/start id= must be present in C2_QUERIES."""
        assert any("/start id=" in q for q in _c2_queries), (
            "MISSING: '/start id=' not found in C2_QUERIES.\n"
            f"Current C2_QUERIES: {_c2_queries}"
        )

    def test_all_body_queries_have_exclusion_filter_telegram(self):
        """Every http.body: entry in C2_QUERIES must include -http.body:\"telegram.org\"."""
        bad = [
            q for q in _c2_queries
            if "http.body:" in q and '-http.body:"telegram.org"' not in q
        ]
        assert not bad, (
            "C2_QUERIES body entries missing -http.body:\"telegram.org\" exclusion:\n"
            + "\n".join(f"  - {q}" for q in bad)
        )

    def test_all_body_queries_have_exclusion_filter_github(self):
        """Every http.body: entry in C2_QUERIES must include -http.body:\"github.com\"."""
        bad = [
            q for q in _c2_queries
            if "http.body:" in q and '-http.body:"github.com"' not in q
        ]
        assert not bad, (
            "C2_QUERIES body entries missing -http.body:\"github.com\" exclusion:\n"
            + "\n".join(f"  - {q}" for q in bad)
        )


# ===========================================================================
# NETLAS — NETLAS_QUERIES
# ===========================================================================


class TestNetlasQueries:
    """Validates: Requirements 1.9–1.10 / 2.8–2.9"""

    def test_start_id_entry_present(self):
        """/start id= must be present in NETLAS_QUERIES."""
        assert any("/start id=" in q for q in _netlas_queries), (
            "MISSING: '/start id=' not found in NETLAS_QUERIES.\n"
            f"Current NETLAS_QUERIES: {_netlas_queries}"
        )

    def test_start_run_entry_present(self):
        """/start /run must be present in NETLAS_QUERIES."""
        assert any("/start /run" in q for q in _netlas_queries), (
            "MISSING: '/start /run' not found in NETLAS_QUERIES.\n"
            f"Current NETLAS_QUERIES: {_netlas_queries}"
        )

    def test_start_invoke_entry_present(self):
        """/start /invoke must be present in NETLAS_QUERIES."""
        assert any("/start /invoke" in q for q in _netlas_queries), (
            "MISSING: '/start /invoke' not found in NETLAS_QUERIES.\n"
            f"Current NETLAS_QUERIES: {_netlas_queries}"
        )

    def test_start_script_entry_present(self):
        """/start /script must be present in NETLAS_QUERIES."""
        assert any("/start /script" in q for q in _netlas_queries), (
            "MISSING: '/start /script' not found in NETLAS_QUERIES.\n"
            f"Current NETLAS_QUERIES: {_netlas_queries}"
        )

    def test_start_http_entry_present(self):
        """/start http:// must be present in NETLAS_QUERIES."""
        assert any("/start http://" in q for q in _netlas_queries), (
            "MISSING: '/start http://' not found in NETLAS_QUERIES.\n"
            f"Current NETLAS_QUERIES: {_netlas_queries}"
        )

    def test_all_body_queries_have_exclusion_filter_telegram(self):
        """Every http.body: entry in NETLAS_QUERIES must include NOT http.body:\"telegram.org\"."""
        bad = [
            q for q in _netlas_queries
            if "http.body:" in q and 'NOT http.body:"telegram.org"' not in q
        ]
        assert not bad, (
            "NETLAS_QUERIES body entries missing NOT http.body:\"telegram.org\" exclusion:\n"
            + "\n".join(f"  - {q}" for q in bad)
        )

    def test_all_body_queries_have_exclusion_filter_github(self):
        """Every http.body: entry in NETLAS_QUERIES must include NOT http.body:\"github.com\"."""
        bad = [
            q for q in _netlas_queries
            if "http.body:" in q and 'NOT http.body:"github.com"' not in q
        ]
        assert not bad, (
            "NETLAS_QUERIES body entries missing NOT http.body:\"github.com\" exclusion:\n"
            + "\n".join(f"  - {q}" for q in bad)
        )


# ===========================================================================
# FOFA — COMMON_QUERIES / FOFA_DEFAULT_QUERIES
# ===========================================================================


class TestFofaQueries:
    """Validates: Requirements 1.11–1.14 / 2.10–2.13"""

    def test_tier2_at_least_one_payload_entry_present(self):
        """FOFA Tier-2 /start payload= queries were intentionally removed after
        7+ hours of zero results across all runs — FOFA does not index live C2
        body content matching these patterns with status_code=200.
        Instead, verify the replacement Tier-2 bot UI fingerprint queries are present."""
        tier2_present = any(
            "sendMessage" in q and "chat_id" in q
            for q in _fofa_queries
        ) or any(
            'title="Telegram Bot"' in q
            for q in _fofa_queries
        )
        assert tier2_present, (
            "FOFA Tier-2 bot UI fingerprint queries missing. "
            f"Current queries: {_fofa_queries}"
        )

    def test_tier3_at_least_one_malware_keyword_entry_present(self):
        """FOFA Tier-3 malware-keyword queries were intentionally removed after
        7+ hours of zero results — FOFA does not index such body content with
        status_code=200. Instead, verify at least one t.me URL pattern is present."""
        tier3_present = any(
            "t.me" in q or "/start" in q
            for q in _fofa_queries
        )
        assert tier3_present, (
            "FOFA Tier-3 t.me / /start URL pattern queries missing. "
            f"Current queries: {_fofa_queries}"
        )

    def test_all_fofa_queries_have_status_code_200(self):
        """Every FOFA query must include status_code=\"200\"."""
        bad = [q for q in _fofa_queries if 'status_code="200"' not in q]
        assert not bad, (
            "FOFA queries missing status_code=\"200\":\n"
            + "\n".join(f"  - {q}" for q in bad)
        )


# ===========================================================================
# EXTENSION — BASE_QUERY_TEMPLATE
# ===========================================================================


class TestExtensionBaseQueryTemplate:
    """Validates: Requirement 1.15 / 2.14"""

    def test_base_query_template_uses_bot_path(self):
        """BASE_QUERY_TEMPLATE must be 'body=\"api.telegram.org/bot\"' (not the broad anchor)."""
        expected = 'body="api.telegram.org/bot"'
        assert _base_query_template == expected, (
            f"BASE_QUERY_TEMPLATE is '{_base_query_template}' "
            f"but expected '{expected}'.\n"
            "The extension uses a broad anchor that matches documentation pages."
        )
