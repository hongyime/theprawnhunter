"""Config coverage for the validation queue settings."""
import pytest

from app.core.config import Settings

_VALIDATION_VARS = (
    "VALIDATION_DISPATCH_BATCH",
    "VALIDATION_DISPATCH_INTERVAL_SECONDS",
    "VALIDATION_LEASE_SECONDS",
    "VALIDATION_QUEUE_MAX_ATTEMPTS",
)


@pytest.fixture
def clean_validation_env(monkeypatch):
    for name in _VALIDATION_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults(clean_validation_env):
    settings = Settings()

    assert settings.VALIDATION_DISPATCH_BATCH == 50
    assert settings.VALIDATION_DISPATCH_INTERVAL_SECONDS == 15
    assert settings.VALIDATION_LEASE_SECONDS == 300
    assert settings.VALIDATION_QUEUE_MAX_ATTEMPTS == 3


def test_env_override_respected(clean_validation_env, monkeypatch):
    monkeypatch.setenv("VALIDATION_DISPATCH_BATCH", "75")
    monkeypatch.setenv("VALIDATION_DISPATCH_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("VALIDATION_LEASE_SECONDS", "600")
    monkeypatch.setenv("VALIDATION_QUEUE_MAX_ATTEMPTS", "5")

    settings = Settings()

    assert settings.VALIDATION_DISPATCH_BATCH == 75
    assert settings.VALIDATION_DISPATCH_INTERVAL_SECONDS == 30
    assert settings.VALIDATION_LEASE_SECONDS == 600
    assert settings.VALIDATION_QUEUE_MAX_ATTEMPTS == 5


@pytest.mark.parametrize("field", _VALIDATION_VARS)
def test_negative_values_rejected(clean_validation_env, field):
    with pytest.raises(ValueError):
        Settings(**{field: -1})


def test_zero_max_attempts_allowed(clean_validation_env):
    settings = Settings(VALIDATION_QUEUE_MAX_ATTEMPTS=0)

    assert settings.VALIDATION_QUEUE_MAX_ATTEMPTS == 0
