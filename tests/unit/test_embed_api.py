"""Tests for the local embedding HTTP service (model mocked, no downloads)."""

from __future__ import annotations

import importlib
import sys
import types

import pytest
from fastapi.testclient import TestClient

from app.services import embed_api


def _fake_vectors(count: int, dims: int = embed_api.DIMENSIONS) -> list[list[float]]:
    return [[float((row + 1) * (column + 1)) / 1000 for column in range(dims)] for row in range(count)]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(embed_api, "_model", None)
    return TestClient(embed_api.app)


def test_embed_returns_deterministic_384_vectors(client, monkeypatch):
    calls: list[list[str]] = []

    def fake_embed(texts):
        calls.append(texts)
        return _fake_vectors(len(texts))

    monkeypatch.setattr(embed_api, "_embed_sync", fake_embed)

    response = client.post("/embed", json={"texts": ["alpha", "beta"]})

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "all-MiniLM-L6-v2"
    assert body["dimensions"] == 384
    assert body["vectors"] == _fake_vectors(2)
    assert all(len(vector) == 384 for vector in body["vectors"])
    assert calls == [["alpha", "beta"]]


def test_health_reports_lazy_model_state(client, monkeypatch):
    assert client.get("/health").json() == {"status": "ok", "model_loaded": False}

    monkeypatch.setattr(embed_api, "_model", object())

    assert client.get("/health").json() == {"status": "ok", "model_loaded": True}


@pytest.mark.parametrize(
    "texts",
    [
        [],
        [""],
        ["   "],
        ["ok"] * 65,
        ["a" * 32_001],
        [None],
    ],
)
def test_invalid_payloads_are_rejected(client, texts):
    response = client.post("/embed", json={"texts": texts})

    assert response.status_code == 422


def test_missing_texts_is_rejected(client):
    assert client.post("/embed", json={}).status_code == 422


def test_exactly_64_texts_are_accepted(client, monkeypatch):
    monkeypatch.setattr(embed_api, "_embed_sync", lambda texts: _fake_vectors(len(texts)))

    response = client.post("/embed", json={"texts": [f"text-{i}" for i in range(64)]})

    assert response.status_code == 200
    assert len(response.json()["vectors"]) == 64


def test_sentence_transformers_is_imported_lazily(client, monkeypatch):
    seen: dict[str, object] = {}
    fake_module = types.ModuleType("sentence_transformers")

    class _FakeModel:
        def encode(self, texts, normalize_embeddings=True, batch_size=64):
            seen["texts"] = list(texts)
            seen["normalize"] = normalize_embeddings
            return _fake_vectors(len(texts))

    class _FakeSentenceTransformer:
        def __init__(self, model_id):
            seen["model_id"] = model_id
            self.model = _FakeModel()

        def encode(self, *args, **kwargs):
            return self.model.encode(*args, **kwargs)

    fake_module.SentenceTransformer = _FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    monkeypatch.setattr(embed_api, "_model", None)

    response = client.post("/embed", json={"texts": ["lazy load"]})

    assert response.status_code == 200
    assert seen["model_id"] == embed_api.MODEL_ID
    assert seen["normalize"] is True
    assert seen["texts"] == ["lazy load"]


def test_module_import_does_not_require_sentence_transformers(monkeypatch):
    monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)

    module = importlib.reload(embed_api)

    assert "sentence_transformers" not in sys.modules
    assert module.MODEL_ID == "sentence-transformers/all-MiniLM-L6-v2"
