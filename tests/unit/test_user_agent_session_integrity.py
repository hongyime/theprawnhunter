import sqlite3

from app.services._user_agent.common import _is_session_file_healthy


def _telethon_session(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions (dc_id integer primary key)")
        conn.execute("INSERT INTO sessions VALUES (1)")


def test_transient_sqlite_lock_does_not_quarantine_a_session(tmp_path, monkeypatch):
    session = tmp_path / "account.session"
    _telethon_session(session)

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", locked)

    assert _is_session_file_healthy(str(session)) is False
    assert session.exists()
    assert not list(tmp_path.glob("*.corrupt.*"))


def test_invalid_sqlite_file_is_quarantined(tmp_path):
    session = tmp_path / "account.session"
    session.write_bytes(b"not a SQLite database")

    assert _is_session_file_healthy(str(session)) is False
    assert not session.exists()
    assert len(list(tmp_path.glob("account.session.corrupt.*"))) == 1


def test_valid_telethon_session_remains_discoverable(tmp_path):
    session = tmp_path / "account.session"
    _telethon_session(session)

    assert _is_session_file_healthy(str(session)) is True
    assert session.exists()
