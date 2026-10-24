from app.core.config import settings


def test_auto_archive_media_enabled_by_default():
    assert settings.AUTO_ARCHIVE_MEDIA is True


def test_auto_invite_captured_bots_disabled_by_default():
    assert getattr(settings, "AUTO_INVITE_CAPTURED_BOTS", False) is False
