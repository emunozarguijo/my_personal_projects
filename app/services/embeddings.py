"""Embeddings con proveedores intercambiables (local, OpenAI, fake para pruebas)."""
from __future__ import annotations

import logging
import math
import re
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.core.config import Settings
from app.core.exceptions import ConfigurationError, EmbeddingError
from app.services.metrics import MetricsService, Stopwatch

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    tokens: int
    estimated_tokens: bool = False


class EmbeddingProvider(ABC):
    name: str
    model: str
    dim: int

    @abstractmethod
    def embed(self, texts: list[str]) -> EmbeddingResult: ...


class LocalEmbeddingProvider(EmbeddingProvider):
    name = "local"

    def __init__(self, model: str, dim: int):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ConfigurationError(
                "Falta sentence-transformers: pip install -r requirements-local-embeddings.txt "
                "(o usa EMBEDDING_PROVIDER=openai)"
            ) from exc
        self.model, self.dim = model, dim
        self._model = SentenceTransformer(model)

    def embed(self, texts: list[str]) -> EmbeddingResult:
        vecs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        tokens = sum(max(1, len(t) // 4) for t in texts)  # estimación
        return EmbeddingResult([v.tolist() for v in vecs], tokens, estimated_tokens=True)


class OpenAIEmbeddingProvider(EmbeddingProvider):
    name = "openai"

    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise ConfigurationError("Define OPENAI_API_KEY para usar embeddings de OpenAI")
        from openai import OpenAI

        self.model, self.dim = settings.openai_embedding_model, settings.embedding_dim
        self._client = OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )

    def embed(self, texts: list[str]) -> EmbeddingResult:
        kwargs = {"dimensions": self.dim} if "text-embedding-3" in self.model else {}
        resp = self._client.embeddings.create(model=self.model, input=texts, **kwargs)
        return EmbeddingResult([d.embedding for d in resp.data], resp.usage.prompt_tokens)


class FakeEmbeddingProvider(EmbeddingProvider):
    """Bolsa de palabras con hashing: determinista y sin red. Solo para pruebas / demos sin modelo."""

    name = "fake"
    model = "fake-hash"

    def __init__(self, dim: int):
        self.dim = dim

    def embed(self, texts: list[str]) -> EmbeddingResult:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in re.findall(r"\w+", t.lower()):
                v[zlib.crc32(tok.encode()) % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return EmbeddingResult(out, sum(len(t.split()) for t in texts), estimated_tokens=True)


def build_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "openai":
        return OpenAIEmbeddingProvider(settings)
    if settings.embedding_provider == "fake":
        return FakeEmbeddingProvider(settings.embedding_dim)
    return LocalEmbeddingProvider(settings.embedding_model, settings.embedding_dim)


class EmbeddingService:
    """Envuelve al proveedor: valida dimensiones, divide en lotes y registra métricas."""

    BATCH = 64

    def __init__(self, provider: EmbeddingProvider, metrics: MetricsService):
        self.provider = provider
        self.metrics = metrics

    @property
    def model(self) -> str:
        return self.provider.model

    def _embed(self, texts: list[str], purpose: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        tokens, error = 0, None
        with Stopwatch() as sw:
            try:
                for i in range(0, len(texts), self.BATCH):
                    res = self.provider.embed(texts[i : i + self.BATCH])
                    vectors.extend(res.vectors)
                    tokens += res.tokens
                bad = [len(v) for v in vectors if len(v) != self.provider.dim]
                if bad:
                    raise EmbeddingError(
                        f"El modelo devolvió dimensión {bad[0]} pero EMBEDDING_DIM={self.provider.dim}. "
                        "Ajusta EMBEDDING_DIM y recrea la tabla document_chunks."
                    )
            except Exception as exc:
                error = exc
        self.metrics.record(
            operation="embedding", provider=self.provider.name, model=self.provider.model,
            latency_ms=sw.ms, status="error" if error else "ok", input_tokens=tokens,
            error=str(error) if error else None, meta={"purpose": purpose, "n_texts": len(texts)},
        )  # fmt: skip
        if error:
            if isinstance(error, EmbeddingError):
                raise error
            raise EmbeddingError(f"Falló el proveedor de embeddings: {error}") from error
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]
