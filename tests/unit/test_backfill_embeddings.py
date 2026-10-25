"""Tests for the embedding backfill CLI and the incremental embed hook.

The model and embed-api HTTP calls are always mocked; no network or DB is used.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from app.services import embedding_hook

CLI_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "backfill_embeddings.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("embedding_backfill_cli", CLI_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vector(seed: int) -> list[float]:
    return [float(seed + 1)] * 384


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table_name):
        self.db = db
        self.table_name = table_name
        self.op = None
        self.filters = []
        self.order_column = None
        self.limit_value = None
        self.payload = None
        self.on_conflict = None

    def select(self, _columns):
        self.op = "select"
        return self

    def order(self, column):
        self.order_column = column
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def gt(self, column, value):
        self.filters.append((column, "gt", value))
        return self

    def in_(self, column, values):
        self.filters.append((column, "in", set(values)))
        return self

    def upsert(self, payload, on_conflict=None):
        self.op = "upsert"
        self.payload = payload
        self.on_conflict = on_conflict
        return self

    def execute(self):
        rows = self.db.tables[self.table_name]
        if self.op == "select":
            selected = [dict(row) for row in rows]
            for column, kind, value in self.filters:
                if kind == "gt":
                    selected = [row for row in selected if row[column] > value]
                elif kind == "in":
                    selected = [row for row in selected if row[column] in value]
            if self.order_column:
                selected.sort(key=lambda row: row[self.order_column])
            if self.limit_value is not None:
                selected = selected[: self.limit_value]
            return _Result(selected)

        assert self.op == "upsert"
        payloads = self.payload if isinstance(self.payload, list) else [self.payload]
        self.db.upsert_calls.append(
            {
                "table": self.table_name,
                "rows": payloads,
                "on_conflict": self.on_conflict,
            }
        )
        by_id = {row.get("message_id"): row for row in rows}
        for payload in payloads:
            by_id[payload["message_id"]] = dict(payload)
        self.db.tables[self.table_name] = list(by_id.values())
        return _Result(payloads)


class _FakeDb:
    def __init__(self, messages, embeddings=None):
        self.tables = {
            "exfiltrated_messages": [dict(row) for row in messages],
            "message_embeddings": [dict(row) for row in (embeddings or [])],
        }
        self.upsert_calls = []

    def table(self, name):
        return _Query(self, name)


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _FakeHttp:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.fail:
            raise httpx.ConnectError("embed-api down")
        texts = (json or {}).get("texts", [])
        return _FakeResponse(
            {
                "model": "all-MiniLM-L6-v2",
                "dimensions": 384,
                "vectors": [_vector(index) for index in range(len(texts))],
            }
        )


def _messages(count: int, start: int = 1) -> list[dict]:
    return [
        {"id": f"{index:04d}", "content": f"message {index}"}
        for index in range(start, start + count)
    ]


def test_cli_defaults_to_dry_run():
    args = _load_cli()._parser().parse_args([])

    assert args.apply is False
    assert args.batch_size == 64
    assert args.timeout == 30.0


def test_cli_apply_is_explicit_only():
    cli = _load_cli()

    assert cli._parser().parse_args(["--apply"]).apply is True
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["--dry-run", "--apply"])


def test_dry_run_excludes_embedded_rows_and_never_writes():
    cli = _load_cli()
    db = _FakeDb(
        _messages(3),
        embeddings=[{"message_id": "0001", "embedding": [0.0] * 384, "model": "x"}],
    )
    http = _FakeHttp()

    stats = cli._run(
        apply=False,
        base_url="http://embed.test",
        client=db,
        http_client=http,
    )

    assert stats["scanned"] == 3
    assert stats["eligible"] == 2
    assert stats["embedded"] == 0
    assert http.calls == []
    assert db.upsert_calls == []


def test_apply_embeds_only_missing_rows_with_384_dimensions():
    cli = _load_cli()
    db = _FakeDb(
        _messages(3),
        embeddings=[{"message_id": "0001", "embedding": [0.0] * 384, "model": "x"}],
    )
    http = _FakeHttp()

    stats = cli._run(
        apply=True,
        base_url="http://embed.test",
        client=db,
        http_client=http,
    )

    assert stats["embedded"] == 2
    assert stats["stopped"] is False
    assert len(http.calls) == 1
    assert http.calls[0]["json"]["texts"] == ["message 2", "message 3"]
    assert http.calls[0]["timeout"] == 30.0

    assert len(db.upsert_calls) == 1
    upsert = db.upsert_calls[0]
    assert upsert["table"] == "message_embeddings"
    assert upsert["on_conflict"] == "message_id"
    assert [row["message_id"] for row in upsert["rows"]] == ["0002", "0003"]
    assert all(len(row["embedding"]) == 384 for row in upsert["rows"])
    assert all(row["model"] == "all-MiniLM-L6-v2" for row in upsert["rows"])


def test_requests_are_batched_at_most_64():
    cli = _load_cli()
    db = _FakeDb(_messages(70))
    http = _FakeHttp()

    stats = cli._run(
        apply=True,
        base_url="http://embed.test",
        client=db,
        http_client=http,
        batch_size=100,
    )

    assert stats["embedded"] == 70
    assert [len(call["json"]["texts"]) for call in http.calls] == [64, 6]


def test_embed_api_down_stops_cleanly_without_upserts():
    cli = _load_cli()
    db = _FakeDb(_messages(3))
    http = _FakeHttp(fail=True)

    stats = cli._run(
        apply=True,
        base_url="http://embed.test",
        client=db,
        http_client=http,
    )

    assert stats["stopped"] is True
    assert stats["embedded"] == 0
    assert stats["resume_after"] is None
    assert "embed-api down" in stats["error"]
    assert db.upsert_calls == []


def test_main_prints_resume_marker_on_embed_api_failure(monkeypatch, capsys):
    cli = _load_cli()
    stopped = {
        "scanned": 3,
        "eligible": 3,
        "embedded": 0,
        "skipped_empty": 0,
        "batches": 1,
        "resume_after": None,
        "stopped": True,
        "error": "connect refused",
    }
    monkeypatch.setattr(cli, "_run", lambda **_kwargs: stopped)

    assert cli.main(["--apply"]) == 1

    output = capsys.readouterr().out
    assert "RESUME_MARKER after_message_id=none" in output
    assert "Re-run the same command to resume" in output


def test_main_dry_run_default_makes_no_changes(monkeypatch, capsys):
    cli = _load_cli()
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return {
            "scanned": 0,
            "eligible": 0,
            "embedded": 0,
            "skipped_empty": 0,
            "batches": 0,
            "resume_after": None,
            "stopped": False,
            "error": None,
        }

    monkeypatch.setattr(cli, "_run", fake_run)

    assert cli.main([]) == 0

    assert seen["apply"] is False
    assert "No changes were made" in capsys.readouterr().out


def test_empty_content_is_skipped_without_embedding():
    cli = _load_cli()
    db = _FakeDb(
        [
            {"id": "0001", "content": "   "},
            {"id": "0002", "content": "real content"},
        ]
    )
    http = _FakeHttp()

    stats = cli._run(
        apply=True,
        base_url="http://embed.test",
        client=db,
        http_client=http,
    )

    assert stats["skipped_empty"] == 1
    assert stats["embedded"] == 1
    assert http.calls[0]["json"]["texts"] == ["real content"]


def test_incremental_hook_is_disabled_without_env(monkeypatch):
    monkeypatch.delenv(embedding_hook.EMBED_API_URL_ENV, raising=False)
    http = _FakeHttp()
    db = _FakeDb([])

    assert (
        embedding_hook.embed_message_best_effort("0001", "hello", db=db, http_client=http) is False
    )
    assert http.calls == []
    assert db.upsert_calls == []


def test_apply_embeds_transcript_and_ocr_with_content():
    cli = _load_cli()
    db = _FakeDb(
        [
            {
                "id": "0001",
                "content": "voice caption",
                "file_meta": {
                    "transcript_text": "hello from asr",
                    "ocr_text": "hello from ocr",
                },
            }
        ]
    )
    http = _FakeHttp()

    stats = cli._run(
        apply=True,
        base_url="http://embed.test",
        client=db,
        http_client=http,
    )

    assert stats["embedded"] == 1
    assert http.calls[0]["json"]["texts"] == ["voice caption\nhello from asr\nhello from ocr"]


def test_empty_content_with_transcript_is_still_eligible():
    cli = _load_cli()
    db = _FakeDb([{"id": "0001", "content": None, "file_meta": {"transcript_text": "spoken"}}])
    http = _FakeHttp()

    stats = cli._run(
        apply=True,
        base_url="http://embed.test",
        client=db,
        http_client=http,
    )

    assert stats["skipped_empty"] == 0
    assert http.calls[0]["json"]["texts"] == ["spoken"]


def test_incremental_hook_upserts_vector(monkeypatch):
    monkeypatch.setenv(embedding_hook.EMBED_API_URL_ENV, "http://embed.test/")
    http = _FakeHttp()
    db = _FakeDb([])

    assert (
        embedding_hook.embed_message_best_effort("0001", "hello", db=db, http_client=http) is True
    )

    assert http.calls[0]["url"] == "http://embed.test/embed"
    assert http.calls[0]["json"] == {"texts": ["hello"]}
    upsert = db.upsert_calls[0]
    assert upsert["table"] == "message_embeddings"
    assert upsert["on_conflict"] == "message_id"
    assert upsert["rows"][0]["message_id"] == "0001"
    assert len(upsert["rows"][0]["embedding"]) == 384


def test_incremental_hook_embeds_transcript_and_ocr(monkeypatch):
    monkeypatch.setenv(embedding_hook.EMBED_API_URL_ENV, "http://embed.test")
    http = _FakeHttp()
    db = _FakeDb([])

    assert (
        embedding_hook.embed_message_best_effort(
            "0001",
            "caption",
            file_meta={"transcript_text": "asr text", "ocr_text": "ocr text"},
            db=db,
            http_client=http,
        )
        is True
    )

    assert http.calls[0]["json"] == {"texts": ["caption\nasr text\nocr text"]}


def test_incremental_hook_swallows_http_failure(monkeypatch):
    monkeypatch.setenv(embedding_hook.EMBED_API_URL_ENV, "http://embed.test")
    http = _FakeHttp(fail=True)
    db = _FakeDb([])

    assert (
        embedding_hook.embed_message_best_effort("0001", "hello", db=db, http_client=http) is False
    )
    assert db.upsert_calls == []


def test_broadcast_capture_survives_hook_failure(monkeypatch, caplog):
    from app.workers.tasks.flows import broadcast

    monkeypatch.setattr(
        embedding_hook,
        "embed_message_best_effort",
        MagicMock(side_effect=RuntimeError("embed-api down")),
    )

    with caplog.at_level("WARNING"):
        result = broadcast._embed_message({"id": "0001", "content": "hello"})

    assert result is None
    assert "embedding hook skipped" in caplog.text


@pytest.mark.asyncio
async def test_scrape_capture_survives_hook_failure(monkeypatch, caplog):
    from app.workers.tasks.flows import scrape

    monkeypatch.setattr(
        embedding_hook,
        "embed_message_best_effort",
        MagicMock(side_effect=RuntimeError("embed-api down")),
    )

    with caplog.at_level("WARNING"):
        await scrape._embed_stored_rows([{"id": "0002", "content": "hello"}])

    assert "embedding hook skipped" in caplog.text
