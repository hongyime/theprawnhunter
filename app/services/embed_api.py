"""Local sentence-embedding HTTP service.

Runs as the standalone ``embed-api`` Compose service (profile ``heavy``) so the
lean API/worker images never need PyTorch. The service exposes a tiny contract:

    POST /embed  {"texts": ["..."]}  ->  384-d vectors
    GET  /health                     ->  {"status": "ok", "model_loaded": bool}

Design notes:
  * ``sentence_transformers`` is imported lazily inside the loader. Importing
    this module must stay cheap and safe in the lean image / test environment.
  * The model is loaded once, under a lock, on the first request.
  * Encoding runs in ``asyncio.to_thread`` so a request cannot block the event
    loop while the (CPU-bound) model works.
  * Callers enforce the 30 s request budget; the service itself just answers.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_NAME = "all-MiniLM-L6-v2"
DIMENSIONS = 384
MAX_BATCH = 64
MAX_TOTAL_CHARS = 32_000


class EmbedRequest(BaseModel):
    """Exactly 1..64 non-empty texts, bounded in total size."""

    texts: list[str] = Field(..., min_length=1, max_length=MAX_BATCH)

    @field_validator("texts")
    @classmethod
    def _validate_texts(cls, texts: list[str]) -> list[str]:
        total = 0
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise ValueError("each text must be a non-empty string")
            total += len(text)
        if total > MAX_TOTAL_CHARS:
            raise ValueError(f"total text length must be <= {MAX_TOTAL_CHARS} characters")
        return texts


class EmbedResponse(BaseModel):
    model: str = MODEL_NAME
    dimensions: int = DIMENSIONS
    vectors: list[list[float]]


app = FastAPI(
    title="Prawn Hunter Embedding API",
    description="Local all-MiniLM-L6-v2 embeddings for semantic search.",
    version="1.0.0",
)

_model: Any = None
_model_lock = threading.Lock()


def _load_model() -> Any:
    """Lazily construct the SentenceTransformer singleton (import stays local)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(MODEL_ID)
    return _model


def _embed_sync(texts: list[str]) -> list[list[float]]:
    """Encode texts on the calling thread; returns plain Python float vectors."""
    model = _load_model()
    vectors = model.encode(texts, normalize_embeddings=True, batch_size=MAX_BATCH)
    return [[float(value) for value in vector] for vector in vectors]


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "model_loaded": _model is not None}


@app.post("/embed", response_model=EmbedResponse)
async def embed(request: EmbedRequest) -> EmbedResponse:
    vectors = await asyncio.to_thread(_embed_sync, list(request.texts))
    return EmbedResponse(vectors=vectors)


__all__ = [
    "DIMENSIONS",
    "MAX_BATCH",
    "MAX_TOTAL_CHARS",
    "MODEL_ID",
    "MODEL_NAME",
    "EmbedRequest",
    "EmbedResponse",
    "app",
]
