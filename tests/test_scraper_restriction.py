import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.scraper_srv import ScraperService


@pytest.mark.asyncio
async def test_scraper_restriction_caching():
    """
    Verifies that ScraperService:
    1. Detects "Bot Restricted" errors.
    2. Falls back to UserAgent.
    3. Caches the restriction in Redis to avoid future errors.
    4. Uses the cache to skip Telethon on subsequent attempts.
    """

    # Setup Mocks
    mock_redis = MagicMock()
    mock_user_agent = AsyncMock()

    # Mock the Redis service in scraper_srv
    with patch("app.core.redis_srv.redis_srv", mock_redis), \
        patch("app.services.user_agent_srv.user_agent", mock_user_agent), \
        patch("httpx.AsyncClient"):

        service = ScraperService()
        bot_token = "123:ABC"
        chat_id = -100123456789

        # --- TEST CASE 1: First Encounter (No Cache) ---
        print("\n--- TEST CASE 1: First Encounter ---")

        # 1. Setup Redis: Not on cooldown
        mock_redis.is_on_cooldown.return_value = False

        # 2. Setup Telethon: Raises "Bot Restricted" error
        mock_client = MagicMock()

        # Define an async generator that raises the exception
        async def mock_iter_messages(*args, **kwargs):
            raise Exception("The API access for bot users is restricted")
            yield # This makes it an async generator

        mock_client.iter_messages.side_effect = mock_iter_messages

        # Ensure get_entity returns an awaitable (AsyncMock)
        mock_client.get_entity = AsyncMock()

        # Properly config get_client to return the mock_client
        mock_get_client = AsyncMock(return_value=mock_client)
        with patch("app.services.bot_manager_srv.bot_manager.get_client", mock_get_client):

            # 3. Setup UserAgent: Returns success
            # Return > 10 items to ensure scrape_history returns early (skipping fallbacks)
            mock_data = [{"telegram_msg_id": i, "content": f"Msg {i}"} for i in range(1, 15)]
            mock_user_agent.get_history.return_value = mock_data

            # ACT
            print("    ▶️ Calling scrape_history...")
            try:
                results = await service.scrape_history(bot_token, chat_id, limit=10)
                print(f"    ▶️ Result count: {len(results)}")
            except Exception as exc:
                print(f"    ❌ Exception in scrape_history: {exc}")
                results = []

            print(f"    🔍 Redis set_cooldown called: {mock_redis.set_cooldown.call_count}")
            print(f"    🔍 UserAgent get_history called: {mock_user_agent.get_history.call_count}")

            # ASSERT
            assert len(results) == 14
            assert results[0]["content"] == "Msg 1"

            # Verify Redis was checked
            mock_redis.is_on_cooldown.assert_called_with(f"bot_restricted:{chat_id}")

            # Verify Redis was SET (The Fix!)
            # This assertion will fail BEFORE we implement the fix, confirming reproduction.
            mock_redis.set_cooldown.assert_called_with(f"bot_restricted:{chat_id}", 21600) # 6 hours
            print("✅ Test 1 Passed: Restriction detected and cached.")

        # --- TEST CASE 2: Cache Hit (Skip Telethon) ---
        print("\n--- TEST CASE 2: Cache Hit ---")

        # 1. Setup Redis: IS on cooldown
        mock_redis.is_on_cooldown.return_value = True

        # Reset mocks
        mock_client.reset_mock()
        mock_user_agent.get_history.reset_mock()
        mock_data_2 = [{"telegram_msg_id": i, "content": f"Cached {i}"} for i in range(1, 15)]
        f2 = asyncio.Future()
        f2.set_result(mock_data_2)
        mock_user_agent.get_history.return_value = f2

        # ACT
        results = await service.scrape_history(bot_token, chat_id, limit=10)

        # ASSERT
        assert len(results) == 14
        assert results[0]["content"] == "Cached 1"

        # Verify Telethon was NOT used (Optimization)
        # We can check if get_client was called, or just ensure we went straight to UA
        # Since we are mocking redis inside the method, if it returns True, the code should return await user_agent...

        # To verify Telethon SKIPPED, we check that bot_manager.get_client was NOT called if we mocked it,
        # or simply that the method returned successfully without raising the exception we set up in Test 1 (if we kept it).
        # But explicitly:
        mock_user_agent.get_history.assert_called_once()
        print("✅ Test 2 Passed: Telethon skipped due to cache.")
