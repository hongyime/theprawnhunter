"""Token encryption for stored bot tokens.

Uses cryptography.fernet.MultiFernet so we can rotate keys without breaking
existing rows:
  - ENCRYPTION_KEY: primary key. New writes encrypt with this.
  - ENCRYPTION_KEY_LEGACY: comma-separated list of previous keys used to
    decrypt already-stored ciphertext. Optional.

Rotation runbook:
  1. Generate a new key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  2. Move current ENCRYPTION_KEY to ENCRYPTION_KEY_LEGACY (comma-append if legacy already set)
  3. Set ENCRYPTION_KEY to the new key
  4. Restart workers — decryption still works via MultiFernet fallback
  5. Run scripts/rotate_credentials.py to re-encrypt all stored ciphertext under the new primary
  6. When re-encryption is fully done, remove the old key from ENCRYPTION_KEY_LEGACY

Never delete a legacy key while rows encrypted with it still exist —
you'd lose access to those tokens permanently.
"""

from cryptography.fernet import Fernet, MultiFernet

from app.core.config import settings


class SecurityService:
    def __init__(self, primary_key: str, legacy_keys: list[str] | None = None):
        if not primary_key:
            raise ValueError("ENCRYPTION_KEY is not set.")

        keys = [Fernet(primary_key.encode())]
        for lk in legacy_keys or []:
            if lk and lk.strip():
                try:
                    keys.append(Fernet(lk.strip().encode()))
                except Exception:
                    # Malformed key in legacy list — skip; we don't want to fail
                    # the whole startup for one bad legacy value.
                    pass

        # MultiFernet encrypts with the FIRST key and decrypts with any of them.
        self.fernet = MultiFernet(keys) if len(keys) > 1 else keys[0]

    def encrypt(self, data: str) -> str:
        """Encrypts a string using the primary key."""
        return self.fernet.encrypt(data.encode()).decode()

    def decrypt(self, token: str) -> str:
        """Decrypts a token string; tries the primary key first, then any
        legacy keys. Raises InvalidToken if no key in the chain works."""
        return self.fernet.decrypt(token.encode()).decode()

    def rotate(self, token: str) -> str:
        """Re-encrypt an existing token under the primary key. Requires
        MultiFernet (i.e. at least one legacy key configured). If we're
        single-key, rotate is a no-op that returns the input unchanged.
        """
        if isinstance(self.fernet, MultiFernet):
            return self.fernet.rotate(token.encode()).decode()
        return token


def _parse_legacy_keys(raw: str) -> list[str]:
    """Split ENCRYPTION_KEY_LEGACY on comma; whitespace-tolerant."""
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


# Global instance
security = SecurityService(
    primary_key=settings.ENCRYPTION_KEY,
    legacy_keys=_parse_legacy_keys(getattr(settings, "ENCRYPTION_KEY_LEGACY", "")),
)
