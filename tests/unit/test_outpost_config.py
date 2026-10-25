"""Config coverage for the outpost (multi-IP) settings and gate."""
import pytest

from app.core.config import Settings, outposts_enabled

_OUTPOST_VARS = (
    "OUTPOST_ENABLED",
    "OUTPOST_LEASE_SIZE",
    "OUTPOST_LEASE_SECONDS",
    "OUTPOST_MAX_ACTIVE",
)


@pytest.fixture
def clean_outpost_env(monkeypatch):
    for name in _OUTPOST_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults(clean_outpost_env):
    settings = Settings()

    assert settings.OUTPOST_ENABLED is False
    assert settings.OUTPOST_LEASE_SIZE == 25
    assert settings.OUTPOST_LEASE_SECONDS == 300
    assert settings.OUTPOST_MAX_ACTIVE == 3


def test_outposts_enabled_defaults_false(clean_outpost_env):
    assert outposts_enabled() is False


@pytest.mark.parametrize("raw", ["false", "False", "0"])
def test_string_false_values_parse_false(clean_outpost_env, monkeypatch, raw):
    monkeypatch.setenv("OUTPOST_ENABLED", raw)

    assert Settings().OUTPOST_ENABLED is False


def test_string_true_value_parses_true(clean_outpost_env, monkeypatch):
    monkeypatch.setenv("OUTPOST_ENABLED", "true")

    assert Settings().OUTPOST_ENABLED is True


def test_env_override_respected(clean_outpost_env, monkeypatch):
    monkeypatch.setenv("OUTPOST_LEASE_SIZE", "40")
    monkeypatch.setenv("OUTPOST_LEASE_SECONDS", "120")
    monkeypatch.setenv("OUTPOST_MAX_ACTIVE", "5")

    settings = Settings()

    assert settings.OUTPOST_LEASE_SIZE == 40
    assert settings.OUTPOST_LEASE_SECONDS == 120
    assert settings.OUTPOST_MAX_ACTIVE == 5


@pytest.mark.parametrize(
    "field",
    ["OUTPOST_LEASE_SIZE", "OUTPOST_LEASE_SECONDS", "OUTPOST_MAX_ACTIVE"],
)
def test_negative_values_rejected(clean_outpost_env, field):
    with pytest.raises(ValueError):
        Settings(**{field: -1})
