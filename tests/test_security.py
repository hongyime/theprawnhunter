"""Tests for SecurityService encryption/decryption.

These tests explicitly disable ``PLAINTEXT_TOKEN_MODE`` so they exercise the
Fernet path even when the running environment has opted into uniform plaintext
storage (SEC-001 operator override, 2026-09-06).
"""
import pytest

from app.core.config import settings
from app.core.security import security


@pytest.fixture(autouse=True)
def _force_encryption(monkeypatch):
    """Force encrypt path for security tests regardless of runtime setting."""
    monkeypatch.setattr(settings, "PLAINTEXT_TOKEN_MODE", False)


def test_encryption_decryption():
    original_text = "Hello World 123"
    encrypted = security.encrypt(original_text)

    assert encrypted != original_text
    assert len(encrypted) > 0

    decrypted = security.decrypt(encrypted)
    assert decrypted == original_text


def test_different_outputs():
    # Fernet produces different output for same input
    text = "secret"
    enc1 = security.encrypt(text)
    enc2 = security.encrypt(text)
    assert enc1 != enc2
    assert security.decrypt(enc1) == security.decrypt(enc2)


def test_plaintext_mode_bypasses_encryption(monkeypatch):
    """When PLAINTEXT_TOKEN_MODE=True, encrypt() is a no-op that returns
    the input unchanged. SEC-001 operator override."""
    monkeypatch.setattr(settings, "PLAINTEXT_TOKEN_MODE", True)
    text = "1234567890:AAtokenlike"
    assert security.encrypt(text) == text
