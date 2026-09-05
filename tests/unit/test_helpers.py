"""
Unit tests for helper utilities.
Test token validation, chat ID extraction, and message parsing.
"""
import pytest

from app.utils.helpers import (
    extract_chat_id,
    extract_tokens_and_chat_ids,
    is_valid_telegram_token,
    parse_telegram_message,
)


class TestTokenValidation:
    """Test Telegram token validation logic"""

    def test_valid_token(self):
        """Test valid token format"""
        assert is_valid_telegram_token("123456789:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh")
        assert is_valid_telegram_token("987654321:AAabcdefghijklmnopqrstuvwxyz1234567")

    def test_invalid_token_no_colon(self):
        """Test rejection of token without colon"""
        assert not is_valid_telegram_token("123456789AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh")

    def test_invalid_token_multiple_colons(self):
        """Test rejection of token with multiple colons"""
        assert not is_valid_telegram_token("123:456:789:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh")

    def test_invalid_token_short_bot_id(self):
        """Test rejection of short bot ID"""
        assert not is_valid_telegram_token("1234567:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh")

    def test_invalid_token_long_bot_id(self):
        """Test rejection of long bot ID"""
        assert not is_valid_telegram_token("1234567890123456:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh")

    def test_invalid_token_non_numeric_id(self):
        """Test rejection of non-numeric bot ID"""
        assert not is_valid_telegram_token("abc123456:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh")

    def test_invalid_token_wrong_secret_length(self):
        """Test rejection of wrong secret length"""
        assert not is_valid_telegram_token("123456789:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5JhXXX")
        assert not is_valid_telegram_token("123456789:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5")

    def test_invalid_token_no_aa_prefix(self):
        """NOTE: Telegram bot token secrets do NOT require an AA prefix.
        Tokens starting with BB, CC, etc. are perfectly valid real tokens
        issued by Telegram's BotFather. This test was incorrect and has been
        updated to document the real constraint: secret must be 35 chars in
        the base64url charset [A-Za-z0-9_-]."""
        # Tokens with non-AA prefix ARE valid — real Telegram tokens exist with any prefix
        assert is_valid_telegram_token("123456789:BBHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh")
        # Still invalid: wrong length
        assert not is_valid_telegram_token("123456789:BBHhbW3Pzj9V5JhU5KzJ")

    def test_invalid_token_fernet_key(self):
        """Test rejection of Fernet key"""
        assert not is_valid_telegram_token("gAAAAABfZqT9...")

    def test_invalid_token_pure_hex(self):
        """Test rejection of pure hex string"""
        assert not is_valid_telegram_token("123456789:AA0123456789abcdef0123456789abcdef0")


class TestChatIDExtraction:
    """Test chat ID extraction from text"""

    def test_extract_chat_id_standard_format(self):
        """Test extraction with standard format"""
        assert extract_chat_id('chat_id=123456789') == "123456789"
        assert extract_chat_id('"chat_id": 987654321') == "987654321"

    def test_extract_chat_id_negative_id(self):
        """Test extraction of negative chat ID"""
        assert extract_chat_id('chat_id=-100123456789') == "-100123456789"

    def test_extract_chat_id_variations(self):
        """Test extraction with different keywords"""
        assert extract_chat_id('target=555555') == "555555"
        assert extract_chat_id('cid:777777') == "777777"

    def test_extract_chat_id_not_found(self):
        """Test when chat ID is not present"""
        assert extract_chat_id('no chat id here') is None


class TestTokenAndChatExtraction:
    """Test combined token and chat ID extraction"""

    def test_extract_from_url(self):
        """Test extraction from URL format"""
        text = "https://api.telegram.org/bot123456789:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh?chat_id=987654321"
        results = extract_tokens_and_chat_ids(text)

        assert len(results) == 1
        assert results[0]['token'] == "123456789:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh"
        assert results[0]['chat_id'] == "987654321"

    def test_extract_multiple_tokens(self):
        """Test extraction of multiple tokens"""
        text = """
        Token 1: 111111111:AAHhbW3Pzj9V5JhU5KzJ9V5JhU5KzJ9V5Jh
        Token 2: 222222222:AAabcdefghijklmnopqrstuvwxyz1234567
        """
        results = extract_tokens_and_chat_ids(text)

        assert len(results) == 2


class TestMessageParsing:
    """Test Telegram message parsing"""

    def test_parse_bot_api_message(self):
        """Test parsing Bot API formatted message"""
        bot_api_msg = {
            'message_id': 123,
            'from': {
                'username': 'testuser',
                'first_name': 'Test'
            },
            'text': 'Hello world',
            'chat': {
                'id': 987654321
            }
        }

        result = parse_telegram_message(bot_api_msg)

        assert result['telegram_msg_id'] == 123
        assert result['sender_name'] == 'testuser'
        assert result['content'] == 'Hello world'
        assert result['media_type'] == 'text'
        assert result['chat_id'] == 987654321

    def test_parse_message_with_photo(self):
        """Test parsing message with photo"""
        msg = {
            'message_id': 456,
            'from': {'first_name': 'Alice'},
            'caption': 'Check this out',
            'photo': [{}],
            'chat': {'id': 111}
        }

        result = parse_telegram_message(msg)

        assert result['media_type'] == 'photo'
        assert result['content'] == 'Check this out'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
