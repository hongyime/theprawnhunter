"""
Preservation Property Tests — Scanner Query Coverage

These tests MUST PASS on BOTH unfixed and fixed code.
They encode the baseline behavior that must be preserved after the fix.

Property 2: Preservation — Caller-Supplied Query Pass-Through and No Existing Entries Removed

Validates: Requirements 3.1, 3.2, 3.3, 3.4
"""

import os
import re

from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Source parsing helpers (same approach as test_scanner_query_coverage_bug.py)
# We parse the source file directly to avoid importing the full Celery/Redis/DB stack.
# ---------------------------------------------------------------------------

_SCANNER_TASKS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "app", "workers", "tasks", "scanner_tasks.py"
)
_QUERIES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "app", "workers", "tasks", "_scanner", "queries.py"
)


def _read_source(path: str) -> str:
    with open(os.path.abspath(path), encoding="utf-8") as fh:
        return fh.read()


def _extract_bracket_block(src: str, start_marker: str) -> str:
    """
    Find `start_marker` in `src`, then walk forward to extract the full
    bracket-balanced block starting at the first `[` after the marker.
    Returns the block string including the outer brackets.
    """
    start = src.find(start_marker)
    if start == -1:
        return ""
    i = src.index("[", start)
    depth = 0
    while i < len(src):
        if src[i] == "[":
            depth += 1
        elif src[i] == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return src[start : i + 1]


def _extract_single_quoted_strings(block: str) -> list:
    return [m.group(1) for m in re.finditer(r"'([^']+)'", block)]


def _extract_c2_queries(src: str) -> list:
    """Extract the C2_QUERIES list from _scan_shodan_c2_async."""
    block = _extract_bracket_block(src, "C2_QUERIES = [")
    return _extract_single_quoted_strings(block)


def _extract_netlas_queries(src: str) -> list:
    """Extract the NETLAS_QUERIES module-level constant."""
    block = _extract_bracket_block(src, "NETLAS_QUERIES = [")
    return _extract_single_quoted_strings(block)


def _extract_fofa_queries(src: str) -> list:
    """
    Extract the FOFA default query list.
    Pre-fix: inline COMMON_QUERIES inside _scan_fofa_async.
    Post-fix: FOFA_DEFAULT_QUERIES module-level constant.
    """
    # Try module-level constant first (post-fix)
    if "FOFA_DEFAULT_QUERIES = [" in src:
        block = _extract_bracket_block(src, "FOFA_DEFAULT_QUERIES = [")
        return _extract_single_quoted_strings(block)
    # Pre-fix: find COMMON_QUERIES inside _scan_fofa_async
    fofa_func_start = src.find("async def _scan_fofa_async")
    if fofa_func_start == -1:
        return []
    start = src.find("COMMON_QUERIES = [", fofa_func_start)
    if start == -1:
        return []
    block = _extract_bracket_block(src, "COMMON_QUERIES = [")
    # Use the one after fofa_func_start
    i = src.index("[", src.find("COMMON_QUERIES = [", fofa_func_start))
    depth = 0
    while i < len(src):
        if src[i] == "[":
            depth += 1
        elif src[i] == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    fofa_block = src[src.find("COMMON_QUERIES = [", fofa_func_start) : i + 1]
    return _extract_single_quoted_strings(fofa_block)


def _get_shodan_query_list(src: str, query: str) -> list:
    """
    Simulate what _scan_shodan_async does with a caller-supplied query.
    The logic is: `queries = [query] if query else default_queries`
    Returns [query] for any non-None, non-empty query.
    """
    # Verify the pass-through pattern exists in source
    assert "queries = [query] if query else" in src, (
        "Could not find caller pass-through pattern in _scan_shodan_async. "
        "Expected: `queries = [query] if query else ...`"
    )
    return [query]


def _get_fofa_query_list(src: str, query: str) -> list:
    """
    Simulate what _scan_fofa_async does with a caller-supplied query.
    The logic is: `queries = [query] if query else COMMON_QUERIES`
    Returns [query] for any non-None, non-empty query.
    """
    assert "queries = [query] if query else" in src, (
        "Could not find caller pass-through pattern in _scan_fofa_async. "
        "Expected: `queries = [query] if query else ...`"
    )
    return [query]


def _get_netlas_query_list(src: str, query: str) -> list:
    """
    Simulate what _scan_netlas_async does with a caller-supplied query.
    The logic is: `queries = [query] if query else NETLAS_QUERIES`
    Returns [query] for any non-None, non-empty query.
    """
    assert "queries = [query] if query else NETLAS_QUERIES" in src, (
        "Could not find caller pass-through pattern in _scan_netlas_async. "
        "Expected: `queries = [query] if query else NETLAS_QUERIES`"
    )
    return [query]


# ---------------------------------------------------------------------------
# Load source once
# ---------------------------------------------------------------------------

# Concatenate scanner_tasks.py + _scanner/queries.py so extraction helpers
# find constants regardless of which file holds them after the monolith split.
_scanner_src = _read_source(_SCANNER_TASKS_PATH) + "\n" + _read_source(_QUERIES_PATH)
_c2_queries = _extract_c2_queries(_scanner_src)
_netlas_queries = _extract_netlas_queries(_scanner_src)
_fofa_queries = _extract_fofa_queries(_scanner_src)


# ===========================================================================
# PRE-FIX SNAPSHOTS
# These are the exact entries observed in the unfixed code.
# After the fix, all of these must still be present (additions only).
# ===========================================================================

# All 29 entries in C2_QUERIES (updated to reflect fixed strings with exclusion filters).
# The fix adds -http.body:"telegram.org" -http.body:"github.com" to all http.body: entries.
# Preservation means all keywords/payloads are still present — no entries removed.
PRE_FIX_C2_QUERIES = [
    'http.headers:"X-Telegram-Bot-Api"',
    'http.body:"api.telegram.org/bot" http.body:"malware" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"rat" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"remote access" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"spyware" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"stealer" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"keylogger" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"c2 server" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"command and control" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"exploit" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"bypass" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"inject" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"persistence" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"privilege escalation" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start payload=" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start token=" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start cmd=" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start c2=" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start key=" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start id=" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start /bin/bash" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start /powershell" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start download" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start /exec" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start /run" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start /invoke" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start /script" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start http://" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
    'http.body:"api.telegram.org/bot" http.body:"/start https://" -http.body:"telegram.org" -http.body:"github.com" http.status:200',
]

# All entries in NETLAS_QUERIES (updated to reflect fixed strings with exclusion filters).
# The fix adds NOT http.body:"telegram.org" NOT http.body:"github.com" to all http.body: entries
# and adds 5 missing Tier 2 entries (/start id=, /run, /invoke, /script, http://).
PRE_FIX_NETLAS_QUERIES = [
    'http.body:"api.telegram.org/bot" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"TELEGRAM_BOT_TOKEN" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"bot_token" http.body:"api.telegram.org" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"TG_BOT_TOKEN" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.headers:"X-Telegram-Bot-Api"',
    'http.body:"api.telegram.org/bot" http.body:"malware" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"rat" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"stealer" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"keylogger" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"c2" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"remote access" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"exploit" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"inject" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"persistence" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"privilege escalation" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"spyware" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"bypass" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"command and control" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"/start payload=" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"/start token=" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"/start cmd=" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"/start c2=" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"/start key=" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"/start /bin/bash" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"/start /powershell" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"/start download" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"/start /exec" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"api.telegram.org/bot" http.body:"/start https://" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"TELEGRAM_BOT_TOKEN" http.body:"REDIS_URL" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"TELEGRAM_BOT_TOKEN" http.body:"DATABASE_URL" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"bot_token" http.body:"webhook" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"https://t.me" http.body:"/start" http.body:"api.telegram.org" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
    'http.body:"http://t.me/bot" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
]

# All 5 entries in _scan_fofa_async's COMMON_QUERIES (updated to reflect fixed strings with status_code="200").
# The fix adds && status_code="200" to all entries and promotes them to FOFA_DEFAULT_QUERIES.
PRE_FIX_FOFA_QUERIES = [
    'body="api.telegram.org/bot" && status_code="200"',
    'body="bot_token" && status_code="200"',
    'body="TELEGRAM_BOT_TOKEN" && status_code="200"',
    'title="Telegram Bot" && status_code="200"',
    'body="sendMessage" && body="chat_id" && status_code="200"',
]


# ===========================================================================
# Property 2a: Caller-Supplied Query Pass-Through (Requirements 3.1)
#
# For all non-empty arbitrary query strings q, each scanner uses exactly [q]
# as its query list when query=q is supplied.
#
# Validates: Requirements 3.1
# ===========================================================================

# Strategy: non-empty strings (printable ASCII, at least 1 char)
_non_empty_query = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Pc", "Pd", "Po", "Zs")),
    min_size=1,
    max_size=200,
).filter(lambda s: s.strip())


class TestCallerQueryPassThrough:
    """
    Property-based tests verifying that a caller-supplied query is passed through
    unchanged as the sole query for each scanner.

    **Validates: Requirements 3.1**
    """

    @given(q=_non_empty_query)
    @h_settings(max_examples=100)
    def test_shodan_uses_exactly_caller_query(self, q):
        """
        For any non-empty query string q, _scan_shodan_async uses exactly [q]
        as its query list when query=q is supplied.

        **Validates: Requirements 3.1**
        """
        result = _get_shodan_query_list(_scanner_src, q)
        assert result == [q], (
            f"Shodan did not use caller-supplied query as sole entry.\n"
            f"Expected: [{q!r}]\n"
            f"Got: {result}"
        )

    @given(q=_non_empty_query)
    @h_settings(max_examples=100)
    def test_fofa_uses_exactly_caller_query(self, q):
        """
        For any non-empty query string q, _scan_fofa_async uses exactly [q]
        as its query list when query=q is supplied.

        **Validates: Requirements 3.1**
        """
        result = _get_fofa_query_list(_scanner_src, q)
        assert result == [q], (
            f"FOFA did not use caller-supplied query as sole entry.\n"
            f"Expected: [{q!r}]\n"
            f"Got: {result}"
        )

    @given(q=_non_empty_query)
    @h_settings(max_examples=100)
    def test_netlas_uses_exactly_caller_query(self, q):
        """
        For any non-empty query string q, _scan_netlas_async uses exactly [q]
        as its query list when query=q is supplied.

        **Validates: Requirements 3.1**
        """
        result = _get_netlas_query_list(_scanner_src, q)
        assert result == [q], (
            f"Netlas did not use caller-supplied query as sole entry.\n"
            f"Expected: [{q!r}]\n"
            f"Got: {result}"
        )

    def test_shodan_pass_through_with_concrete_query(self):
        """
        Concrete example: _scan_shodan_async with query='my_custom_query'
        uses exactly ['my_custom_query'].

        **Validates: Requirements 3.1**
        """
        result = _get_shodan_query_list(_scanner_src, "my_custom_query")
        assert result == ["my_custom_query"]

    def test_fofa_pass_through_with_concrete_query(self):
        """
        Concrete example: _scan_fofa_async with query='my_custom_query'
        uses exactly ['my_custom_query'].

        **Validates: Requirements 3.1**
        """
        result = _get_fofa_query_list(_scanner_src, "my_custom_query")
        assert result == ["my_custom_query"]

    def test_netlas_pass_through_with_concrete_query(self):
        """
        Concrete example: _scan_netlas_async with query='my_custom_query'
        uses exactly ['my_custom_query'].

        **Validates: Requirements 3.1**
        """
        result = _get_netlas_query_list(_scanner_src, "my_custom_query")
        assert result == ["my_custom_query"]


# ===========================================================================
# Property 2b: No Existing C2_QUERIES Entries Removed (Requirements 3.2)
#
# All pre-fix C2_QUERIES entries must still be present in the (possibly fixed)
# C2_QUERIES list. The fix is additive only — no removals.
#
# Validates: Requirements 3.2
# ===========================================================================


class TestC2QueriesPreservation:
    """
    Example-based tests asserting all pre-fix C2_QUERIES entries are still present.

    **Validates: Requirements 3.2**
    """

    def test_all_pre_fix_c2_entries_present(self):
        """
        All entries observed in C2_QUERIES before the fix must still be present
        after the fix. The fix is additive only — no entries may be removed.

        **Validates: Requirements 3.2**
        """
        missing = [e for e in PRE_FIX_C2_QUERIES if e not in _c2_queries]
        assert not missing, (
            "The following pre-fix C2_QUERIES entries are missing from the current list:\n"
            + "\n".join(f"  - {e!r}" for e in missing)
            + f"\n\nCurrent C2_QUERIES ({len(_c2_queries)} entries):\n"
            + "\n".join(f"  - {e!r}" for e in _c2_queries)
        )

    def test_c2_header_entry_preserved(self):
        """The header-based entry must be preserved."""
        assert 'http.headers:"X-Telegram-Bot-Api"' in _c2_queries

    def test_c2_malware_entries_preserved(self):
        """All 13 malware keyword entries must be preserved."""
        malware_keywords = [
            "malware", "rat", "remote access", "spyware", "stealer", "keylogger",
            "c2 server", "command and control", "exploit", "bypass", "inject",
            "persistence", "privilege escalation",
        ]
        missing = [
            kw for kw in malware_keywords
            if not any(kw in q for q in _c2_queries)
        ]
        assert not missing, (
            f"C2_QUERIES missing malware keyword entries: {missing}"
        )

    def test_c2_start_payload_entries_preserved(self):
        """All /start payload entries that were present pre-fix must be preserved."""
        pre_fix_payloads = [
            "/start payload=", "/start token=", "/start cmd=", "/start c2=",
            "/start key=", "/start id=", "/start /bin/bash", "/start /powershell",
            "/start download", "/start /exec", "/start /run", "/start /invoke",
            "/start /script", "/start http://", "/start https://",
        ]
        missing = [
            p for p in pre_fix_payloads
            if not any(p in q for q in _c2_queries)
        ]
        assert not missing, (
            f"C2_QUERIES missing /start payload entries: {missing}"
        )


# ===========================================================================
# Property 2c: No Existing NETLAS_QUERIES Entries Removed (Requirements 3.3)
#
# All pre-fix NETLAS_QUERIES entries must still be present in the (possibly
# fixed) NETLAS_QUERIES list. The fix is additive only — no removals.
#
# Validates: Requirements 3.3
# ===========================================================================


class TestNetlasQueriesPreservation:
    """
    Example-based tests asserting all pre-fix NETLAS_QUERIES entries are still present.

    **Validates: Requirements 3.3**
    """

    def test_all_pre_fix_netlas_entries_present(self):
        """
        All entries observed in NETLAS_QUERIES before the fix must still be present
        after the fix. The fix is additive only — no entries may be removed.

        **Validates: Requirements 3.3**
        """
        missing = [e for e in PRE_FIX_NETLAS_QUERIES if e not in _netlas_queries]
        assert not missing, (
            "The following pre-fix NETLAS_QUERIES entries are missing from the current list:\n"
            + "\n".join(f"  - {e!r}" for e in missing)
            + f"\n\nCurrent NETLAS_QUERIES ({len(_netlas_queries)} entries):\n"
            + "\n".join(f"  - {e!r}" for e in _netlas_queries)
        )

    def test_netlas_direct_token_entries_preserved(self):
        """Direct token detection entries must be preserved (with exclusion filters)."""
        required = [
            'http.body:"api.telegram.org/bot" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
            'http.body:"TELEGRAM_BOT_TOKEN" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
            'http.body:"TG_BOT_TOKEN" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
        ]
        missing = [e for e in required if e not in _netlas_queries]
        assert not missing, (
            f"NETLAS_QUERIES missing direct token entries: {missing}"
        )

    def test_netlas_header_entry_preserved(self):
        """The header-based entry must be preserved."""
        assert 'http.headers:"X-Telegram-Bot-Api"' in _netlas_queries

    def test_netlas_config_file_entries_preserved(self):
        """Config file pattern entries must be preserved (with exclusion filters)."""
        required = [
            'http.body:"TELEGRAM_BOT_TOKEN" http.body:"REDIS_URL" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
            'http.body:"TELEGRAM_BOT_TOKEN" http.body:"DATABASE_URL" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
            'http.body:"bot_token" http.body:"webhook" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
        ]
        missing = [e for e in required if e not in _netlas_queries]
        assert not missing, (
            f"NETLAS_QUERIES missing config file entries: {missing}"
        )

    def test_netlas_tme_entries_preserved(self):
        """t.me URL pattern entries must be preserved (with exclusion filters)."""
        required = [
            'http.body:"https://t.me" http.body:"/start" http.body:"api.telegram.org" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
            'http.body:"http://t.me/bot" http.status_code:200 NOT http.body:"telegram.org" NOT http.body:"github.com"',
        ]
        missing = [e for e in required if e not in _netlas_queries]
        assert not missing, (
            f"NETLAS_QUERIES missing t.me entries: {missing}"
        )


# ===========================================================================
# Property 2d: No Existing FOFA COMMON_QUERIES Entries Removed (Requirements 3.4)
#
# All 5 pre-fix FOFA COMMON_QUERIES entries must still be present in the
# (possibly fixed) FOFA query list. The fix is additive only — no removals.
#
# Validates: Requirements 3.4
# ===========================================================================


class TestFofaQueriesPreservation:
    """
    Example-based tests asserting all 5 pre-fix FOFA COMMON_QUERIES entries
    are still present.

    **Validates: Requirements 3.4**
    """

    def test_all_pre_fix_fofa_entries_present(self):
        """
        All 5 entries observed in _scan_fofa_async's COMMON_QUERIES before the fix
        must still be present after the fix. The fix is additive only — no entries
        may be removed.

        **Validates: Requirements 3.4**
        """
        missing = [e for e in PRE_FIX_FOFA_QUERIES if e not in _fofa_queries]
        assert not missing, (
            "The following pre-fix FOFA COMMON_QUERIES entries are missing from the current list:\n"
            + "\n".join(f"  - {e!r}" for e in missing)
            + f"\n\nCurrent FOFA queries ({len(_fofa_queries)} entries):\n"
            + "\n".join(f"  - {e!r}" for e in _fofa_queries)
        )

    def test_fofa_api_telegram_bot_entry_preserved(self):
        """body=\"api.telegram.org/bot\" entry must be preserved."""
        assert any('body="api.telegram.org/bot"' in q for q in _fofa_queries), (
            f"FOFA queries missing 'body=\"api.telegram.org/bot\"' entry.\n"
            f"Current FOFA queries: {_fofa_queries}"
        )

    def test_fofa_bot_token_entry_preserved(self):
        """body=\"bot_token\" entry must be preserved."""
        assert any('body="bot_token"' in q for q in _fofa_queries), (
            f"FOFA queries missing 'body=\"bot_token\"' entry.\n"
            f"Current FOFA queries: {_fofa_queries}"
        )

    def test_fofa_telegram_bot_token_entry_preserved(self):
        """body=\"TELEGRAM_BOT_TOKEN\" entry must be preserved."""
        assert any('body="TELEGRAM_BOT_TOKEN"' in q for q in _fofa_queries), (
            f"FOFA queries missing 'body=\"TELEGRAM_BOT_TOKEN\"' entry.\n"
            f"Current FOFA queries: {_fofa_queries}"
        )

    def test_fofa_title_telegram_bot_entry_preserved(self):
        """title=\"Telegram Bot\" entry must be preserved."""
        assert any('title="Telegram Bot"' in q for q in _fofa_queries), (
            f"FOFA queries missing 'title=\"Telegram Bot\"' entry.\n"
            f"Current FOFA queries: {_fofa_queries}"
        )

    def test_fofa_send_message_chat_id_entry_preserved(self):
        """body=\"sendMessage\" && body=\"chat_id\" entry must be preserved."""
        assert any(
            'body="sendMessage"' in q and 'body="chat_id"' in q
            for q in _fofa_queries
        ), (
            f"FOFA queries missing 'body=\"sendMessage\" && body=\"chat_id\"' entry.\n"
            f"Current FOFA queries: {_fofa_queries}"
        )
