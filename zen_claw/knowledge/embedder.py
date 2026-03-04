"""Embedding providers for RAG."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


@dataclass
class LocalEmbedder:
    model_name: str = "BAAI/bge-m3"

    def __post_init__(self) -> None:
        self._model = None

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._get_model()
        vectors = model.encode(texts, normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]


@dataclass
class OpenAIEmbedder:
    model_name: str = "text-embedding-3-small"
    api_key: str | None = None
    api_base: str | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        import httpx
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        base = (self.api_base or "https://api.openai.com/v1").rstrip("/")
        resp = httpx.post(
            f"{base}/embeddings",
            json={"model": self.model_name, "input": texts},
            headers=headers,
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", [])
        return [list(map(float, item.get("embedding", []))) for item in items]
