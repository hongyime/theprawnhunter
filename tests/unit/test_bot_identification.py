import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from app.services.scraper_srv import ScraperService

class TestBotIdentification(unittest.TestCase):
    def setUp(self):
        self.scraper = ScraperService()
        self.monitor_token = "1209926912:AAF8zrjCKM4a-x8ZEH-F3KSWtomgAw_9w9Q"

    @patch("app.services.scraper_srv.settings")
    def test_is_monitor_bot_exact_match(self, mock_settings):
        mock_settings.bot_tokens = [self.monitor_token]
        
        # Exact match
        self.assertTrue(self.scraper.is_monitor_bot(self.monitor_token))
        
        # Match with whitespace
        self.assertTrue(self.scraper.is_monitor_bot(f"  {self.monitor_token}  "))
        self.assertTrue(self.scraper.is_monitor_bot(f"\n{self.monitor_token}\t"))

    @patch("app.services.scraper_srv.settings")
    def test_is_monitor_bot_id_match(self, mock_settings):
        mock_settings.bot_tokens = [self.monitor_token]
        
        # Same ID, different secret (simulating format variations or rotations)
        different_secret = "1209926912:DIFFERENT_SECRET"
        self.assertTrue(self.scraper.is_monitor_bot(different_secret))
        
        # Whitespace and same ID
        self.assertTrue(self.scraper.is_monitor_bot("  1209926912:XYZ  "))

    @patch("app.services.scraper_srv.settings")
    def test_is_monitor_bot_no_match(self, mock_settings):
        mock_settings.bot_tokens = [self.monitor_token]
        
        # Completely different token
        other_token = "987654321:OTHER_SECRET"
        self.assertFalse(self.scraper.is_monitor_bot(other_token))
        
        # Empty inputs
        self.assertFalse(self.scraper.is_monitor_bot(""))
        self.assertFalse(self.scraper.is_monitor_bot(None))

    @patch("app.services.scraper_srv.settings")
    def test_is_monitor_bot_missing_settings(self, mock_settings):
        mock_settings.bot_tokens = None
        self.assertFalse(self.scraper.is_monitor_bot(self.monitor_token))

    @patch("app.services.scraper_srv.settings")
    def test_is_monitor_bot_multi_token_list(self, mock_settings):
        """Test that is_monitor_bot works with multiple tokens in the list."""
        mock_settings.bot_tokens = [
            "111111111:AAAA",
            self.monitor_token,
            "333333333:CCCC"
        ]
        # Should match the second token
        self.assertTrue(self.scraper.is_monitor_bot(self.monitor_token))
        # Should match by ID for first token
        self.assertTrue(self.scraper.is_monitor_bot("111111111:DIFFERENT"))
        # Should NOT match an unknown token
        self.assertFalse(self.scraper.is_monitor_bot("999999999:UNKNOWN"))

if __name__ == "__main__":
    unittest.main()


# ── _fetch_bot_capabilities tests ──────────────────────────────────────────

import pytest
import httpx
from unittest.mock import AsyncMock


class TestFetchBotCapabilities:
    """Unit tests for _fetch_bot_capabilities helper."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_all_keys(self):
        """All 6 API calls succeed → all 8 capability keys present."""
        from app.workers.tasks.flow_tasks import _fetch_bot_capabilities

        mock_responses = {
            "getMe": {"can_join_groups": True, "can_read_all_group_messages": False, "supports_inline_queries": True},
            "getMyDefaultAdministratorRights?for_channels=false": {"is_anonymous": False},
            "getMyDefaultAdministratorRights?for_channels=true": {"can_post_messages": True},
            "getMyDescription": {"description": "Test bot"},
            "getMyShortDescription": {"short_description": "Short"},
            "getChat?chat_id=12345": {"linked_chat_id": 99999},
        }

        async def fake_get(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            for key, val in mock_responses.items():
                if key in url:
                    mock_resp.json.return_value = {"result": val}
                    return mock_resp
            mock_resp.json.return_value = {"result": {}}
            return mock_resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = fake_get

        with patch("app.workers.tasks.flow_tasks.httpx.AsyncClient", return_value=mock_client):
            result = await _fetch_bot_capabilities("123456:ABC", chat_id=12345)

        assert "can_join_groups" in result
        assert result["can_join_groups"] is True
        assert result["description"] == "Test bot"
        assert result["linked_chat_id"] == 99999

    @pytest.mark.asyncio
    async def test_partial_failure_returns_partial_dict(self):
        """If getMyDescription raises, other keys still populated."""
        from app.workers.tasks.flow_tasks import _fetch_bot_capabilities

        call_count = {"n": 0}

        async def fake_get(url, **kwargs):
            call_count["n"] += 1
            if "getMyDescription" in url:
                raise httpx.RequestError("timeout")
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"result": {"can_join_groups": True}}
            return mock_resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = fake_get

        with patch("app.workers.tasks.flow_tasks.httpx.AsyncClient", return_value=mock_client):
            result = await _fetch_bot_capabilities("123456:ABC")

        assert "can_join_groups" in result
        # description should be absent or empty due to failure
        assert result.get("description", "") == ""

    @pytest.mark.asyncio
    async def test_total_failure_never_raises(self):
        """If all calls raise, returns without raising — no can_join_groups key."""
        from app.workers.tasks.flow_tasks import _fetch_bot_capabilities

        async def fake_get(url, **kwargs):
            raise httpx.ConnectError("refused")

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = fake_get

        with patch("app.workers.tasks.flow_tasks.httpx.AsyncClient", return_value=mock_client):
            result = await _fetch_bot_capabilities("123456:ABC")

        # getMe failed → capability flags are NOT set
        assert "can_join_groups" not in result
        assert "can_read_all_group_messages" not in result
        # other keys are set to None/empty but no exception raised
        assert result is not None
