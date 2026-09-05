"""
Unit tests for multi-bot rotation functionality.
Tests config parsing, bot username helpers, and broadcaster cycling.
"""
import unittest
import os
from unittest.mock import patch


class TestConfigMultiToken(unittest.TestCase):
    """Test that config correctly parses multiple bot tokens."""

    def test_parse_single_token_via_model(self):
        """Single token string should produce a single-item bot_tokens list."""
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()

        with patch.dict(os.environ, {
            'SUPABASE_URL': 'https://x.supabase.co',
            'SUPABASE_KEY': 'k',
            'SUPABASE_SERVICE_ROLE_KEY': 'k',
            'REDIS_URL': 'redis://localhost',
            'ENCRYPTION_KEY': key,
            'MONITOR_BOT_TOKEN': '123456789:AAXXX',
            'MONITOR_GROUP_ID': '-100',
            'TELEGRAM_API_ID': '12345',
            'TELEGRAM_API_HASH': 'abc',
        }, clear=False):
            from app.core.config import Settings
            s = Settings()
            self.assertEqual(len(s.bot_tokens), 1)
            self.assertEqual(s.bot_tokens[0], '123456789:AAXXX')

    def test_parse_multiple_tokens_via_model(self):
        """Comma-separated tokens should become a multi-item list."""
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()

        with patch.dict(os.environ, {
            'SUPABASE_URL': 'https://x.supabase.co',
            'SUPABASE_KEY': 'k',
            'SUPABASE_SERVICE_ROLE_KEY': 'k',
            'REDIS_URL': 'redis://localhost',
            'ENCRYPTION_KEY': key,
            'MONITOR_BOT_TOKEN': '111:AAA,222:BBB,333:CCC',
            'MONITOR_GROUP_ID': '-100',
            'TELEGRAM_API_ID': '12345',
            'TELEGRAM_API_HASH': 'abc',
        }, clear=False):
            from app.core.config import Settings
            s = Settings()
            self.assertEqual(len(s.bot_tokens), 3)
            self.assertEqual(s.bot_tokens[0], '111:AAA')
            self.assertEqual(s.bot_tokens[1], '222:BBB')
            self.assertEqual(s.bot_tokens[2], '333:CCC')

    def test_parse_tokens_with_whitespace(self):
        """Whitespace around tokens should be stripped."""
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()

        with patch.dict(os.environ, {
            'SUPABASE_URL': 'https://x.supabase.co',
            'SUPABASE_KEY': 'k',
            'SUPABASE_SERVICE_ROLE_KEY': 'k',
            'REDIS_URL': 'redis://localhost',
            'ENCRYPTION_KEY': key,
            'MONITOR_BOT_TOKEN': '  111:AAA , 222:BBB , 333:CCC  ',
            'MONITOR_GROUP_ID': '-100',
            'TELEGRAM_API_ID': '12345',
            'TELEGRAM_API_HASH': 'abc',
        }, clear=False):
            from app.core.config import Settings
            s = Settings()
            self.assertEqual(len(s.bot_tokens), 3)
            self.assertEqual(s.bot_tokens[0], '111:AAA')


class TestBotUsernameHelpers(unittest.TestCase):
    """Test the bot rotation helper functions in bot_listener."""

    def test_get_other_bot_usernames_excludes_current(self):
        """Should return all bots except the current one."""
        from app.services.bot_listener import _bot_usernames, _get_other_bot_usernames, _locked_bots

        # Setup state
        _bot_usernames.clear()
        _locked_bots.clear()
        _bot_usernames["token1"] = "BotA"
        _bot_usernames["token2"] = "BotB"
        _bot_usernames["token3"] = "BotC"

        result = _get_other_bot_usernames("BotA")
        self.assertEqual(sorted(result), ["BotB", "BotC"])

    def test_get_other_bot_usernames_excludes_locked(self):
        """Should exclude locked bots from recommendations."""
        from app.services.bot_listener import _bot_usernames, _get_other_bot_usernames, _locked_bots

        _bot_usernames.clear()
        _locked_bots.clear()
        _bot_usernames["token1"] = "BotA"
        _bot_usernames["token2"] = "BotB"
        _bot_usernames["token3"] = "BotC"
        _locked_bots.add("token2")  # BotB is locked

        result = _get_other_bot_usernames("BotA")
        self.assertEqual(result, ["BotC"])

    def test_get_other_bot_usernames_empty_pool(self):
        """Should return empty list if only one bot exists."""
        from app.services.bot_listener import _bot_usernames, _get_other_bot_usernames, _locked_bots

        _bot_usernames.clear()
        _locked_bots.clear()
        _bot_usernames["token1"] = "BotA"

        result = _get_other_bot_usernames("BotA")
        self.assertEqual(result, [])

    def test_get_all_bot_usernames_except_includes_locked(self):
        """Fallback should include locked bots (since all alternatives are shown)."""
        from app.services.bot_listener import (
            _bot_usernames,
            _get_all_bot_usernames_except,
            _locked_bots,
        )

        _bot_usernames.clear()
        _locked_bots.clear()
        _bot_usernames["token1"] = "BotA"
        _bot_usernames["token2"] = "BotB"
        _locked_bots.add("token2")  # BotB is locked

        result = _get_all_bot_usernames_except("BotA")
        self.assertEqual(result, ["BotB"])

    def test_case_insensitive_matching(self):
        """Username matching should be case-insensitive."""
        from app.services.bot_listener import _bot_usernames, _get_other_bot_usernames, _locked_bots

        _bot_usernames.clear()
        _locked_bots.clear()
        _bot_usernames["token1"] = "BotA"
        _bot_usernames["token2"] = "BotB"

        result = _get_other_bot_usernames("bota")  # lowercase
        self.assertEqual(result, ["BotB"])


class TestBroadcasterTokenCycling(unittest.TestCase):
    """Test that BroadcasterService builds a rotation pool from all tokens."""

    @patch("app.services.broadcaster_srv.settings")
    def test_round_robin_cycling(self, mock_settings):
        """Pool should contain one entry per token; _cycle produces round-robin order."""
        mock_settings.bot_tokens = [
            "111111111:AAAA",
            "222222222:BBBB",
            "333333333:CCCC",
        ]

        from app.services.broadcaster_srv import BroadcasterService
        broadcaster = BroadcasterService()

        # Pool has one entry per token
        self.assertEqual(len(broadcaster._pool), 3)
        pool_ids = [entry["id"] for entry in broadcaster._pool]
        self.assertIn("111111111:AAAA", pool_ids)
        self.assertIn("222222222:BBBB", pool_ids)
        self.assertIn("333333333:CCCC", pool_ids)

        # _cycle is an itertools.cycle -- advance it and verify round-robin order
        tokens_seen = [next(broadcaster._cycle)["id"] for _ in range(6)]
        self.assertEqual(tokens_seen[0], "111111111:AAAA")
        self.assertEqual(tokens_seen[1], "222222222:BBBB")
        self.assertEqual(tokens_seen[2], "333333333:CCCC")
        self.assertEqual(tokens_seen[3], "111111111:AAAA")
        self.assertEqual(tokens_seen[4], "222222222:BBBB")
        self.assertEqual(tokens_seen[5], "333333333:CCCC")

        # _get_bot_instance returns a Bot object for any token in the pool
        bot = broadcaster._get_bot_instance("111111111:AAAA")
        self.assertIsNotNone(bot)


if __name__ == "__main__":
    unittest.main()
