"""Unit tests for the central ``sweep_text_for_tokens`` helper."""

from app.domain.tokens import sweep_text_for_tokens

VALID = "123456789:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh"
OTHER = "987654321:BBHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh"
PURE_HEX = "123456789:" + "0123456789abcdef" * 2 + "012"


class TestSweepTextForTokens:
    def test_bare_token(self):
        """A bare token in free text is found with all contract keys."""
        result = sweep_text_for_tokens(f"config token: {VALID} end", "unit")

        assert len(result) == 1
        assert set(result[0]) == {"token", "chat_id", "context", "source"}
        assert result[0]["token"] == VALID
        assert result[0]["chat_id"] is None
        assert result[0]["source"] == "unit"

    def test_bot_url_form(self):
        """The /bot<token> URL form is normalised to the bare token."""
        text = f"https://api.telegram.org/bot{VALID}/getUpdates"

        result = sweep_text_for_tokens(text, "url")

        assert [item["token"] for item in result] == [VALID]

    def test_duplicate_token_deduped(self):
        """Repeated tokens collapse to a single first-occurrence entry."""
        text = f"{VALID} then {OTHER} then {VALID}"

        result = sweep_text_for_tokens(text, "dup")

        assert [item["token"] for item in result] == [VALID, OTHER]

    def test_chat_id_association(self):
        """The first chat_id in the same text is attached to every token."""
        text = f"chat_id=-1001234567890 token {VALID}"

        result = sweep_text_for_tokens(text, "chat")

        assert result[0]["chat_id"] == "-1001234567890"

    def test_pure_hex_secret_rejected(self):
        """A pure-hex secret is a hash collision, not a token."""
        assert sweep_text_for_tokens(f"leak {PURE_HEX} here", "hex") == []

    def test_oversized_input_truncated(self):
        """Input beyond max_bytes is truncated and still yields kept tokens."""
        text = f"{VALID} " + ("padding " * 10_000)
        limit = len(VALID) + 10

        result = sweep_text_for_tokens(text, "big", max_bytes=limit)

        assert [item["token"] for item in result] == [VALID]

    def test_empty_input(self):
        """Empty and whitespace-only input returns an empty list."""
        assert sweep_text_for_tokens("", "empty") == []
        assert sweep_text_for_tokens("   \n\t  ", "empty") == []

    def test_binary_input(self):
        """Binary blobs are skipped without raising."""
        assert sweep_text_for_tokens("\x00\x01\x02" * 1000, "bin") == []

    def test_context_window(self):
        """Context contains the token and surrounding characters, capped at 500."""
        text = "left-marker " + VALID + " right-marker"

        context = sweep_text_for_tokens(text, "ctx")[0]["context"]

        assert VALID in context
        assert "left-marker" in context
        assert "right-marker" in context
        assert len(context) <= 500
