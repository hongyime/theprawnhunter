from app.core.security import security


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
