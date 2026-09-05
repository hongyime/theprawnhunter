"""
Unit tests for retry logic.
Test retry decorator with different scenarios.
"""

import pytest

from app.core.retry import retry


class TestRetryDecorator:
    """Test retry decorator functionality"""

    def test_retry_success_first_attempt(self):
        """Test function succeeds on first try"""
        call_count = 0

        @retry(max_attempts=3)
        def succeed_immediately():
            nonlocal call_count
            call_count += 1
            return "success"

        result = succeed_immediately()

        assert result == "success"
        assert call_count == 1

    def test_retry_success_after_failures(self):
        """Test function succeeds after retries"""
        call_count = 0

        @retry(max_attempts=3, base_delay=0.1)
        def succeed_on_third():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("Temporary failure")
            return "success"

        result = succeed_on_third()

        assert result == "success"
        assert call_count == 3

    def test_retry_exhausts_attempts(self):
        """Test function fails after max attempts"""
        call_count = 0

        @retry(max_attempts=3, base_delay=0.1)
        def always_fails():
            nonlocal call_count
            call_count += 1
            raise ValueError("Always fails")

        with pytest.raises(ValueError):
            always_fails()

        assert call_count == 3

    def test_retry_exponential_backoff(self):
        """Test exponential backoff timing"""
        import time
        call_times = []

        @retry(max_attempts=3, base_delay=0.5, exponential=True)
        def track_timing():
            call_times.append(time.time())
            if len(call_times) < 3:
                raise ConnectionError("Retry")
            return "done"

        result = track_timing()

        assert result == "done"
        # Check delays are increasing (exponential)
        if len(call_times) >= 3:
            delay1 = call_times[1] - call_times[0]
            delay2 = call_times[2] - call_times[1]
            assert delay2 > delay1

    @pytest.mark.asyncio
    async def test_async_retry(self):
        """Test retry with async function"""
        call_count = 0

        @retry(max_attempts=3, base_delay=0.1)
        async def async_succeed_on_second():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("Temporary failure")
            return "async success"

        result = await async_succeed_on_second()

        assert result == "async success"
        assert call_count == 2

    def test_retry_specific_exceptions(self):
        """Test retry only catches specified exceptions"""
        call_count = 0

        @retry(max_attempts=3, base_delay=0.1, exceptions=(ConnectionError,))
        def raise_different_error():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("Should not be caught")
            return "should not reach"

        with pytest.raises(ValueError):
            raise_different_error()

        # Should fail immediately, not retry
        assert call_count == 1


class TestCountrySelection:
    """Test random country selection logic"""

    def test_random_country_selection(self):
        """Test random selection from TARGET_COUNTRIES"""
        import random

        from app.core.config import settings

        # Seed for reproducibility in tests
        random.seed(42)

        selected = random.choice(settings.TARGET_COUNTRIES)

        assert selected in settings.TARGET_COUNTRIES
        assert len(selected) == 2  # Country codes are 2 letters

    def test_country_list_not_empty(self):
        """Test TARGET_COUNTRIES is not empty"""
        from app.core.config import settings

        assert len(settings.TARGET_COUNTRIES) > 0
        assert "US" in settings.TARGET_COUNTRIES
        assert "RU" in settings.TARGET_COUNTRIES


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
