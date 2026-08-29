from app.services.scanners import ExaService


def test_exa_service_returns_none_when_no_keys(monkeypatch):
    from app.services import scanners

    monkeypatch.setattr(scanners.settings, "EXA_API_KEY", None)
    monkeypatch.setattr(scanners.settings, "EXA_API_KEY_2", None)
    monkeypatch.setattr(scanners.settings, "EXA_API_KEY_3", None)

    assert ExaService()._get_api_key() is None


def test_exa_service_rotates_across_configured_keys(monkeypatch):
    from app.services import scanners

    monkeypatch.setattr(scanners.settings, "EXA_API_KEY", "key-1")
    monkeypatch.setattr(scanners.settings, "EXA_API_KEY_2", "key-2")
    monkeypatch.setattr(scanners.settings, "EXA_API_KEY_3", "")

    seen_keys = []

    def fake_choice(keys):
        seen_keys.extend(keys)
        return keys[-1]

    monkeypatch.setattr(scanners.random, "choice", fake_choice)

    assert ExaService()._get_api_key() == "key-2"
    assert seen_keys == ["key-1", "key-2"]
