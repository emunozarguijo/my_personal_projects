"""Recuperación (RAG): embedding de la pregunta + búsqueda vectorial + resumen siempre presente."""
from __future__ import annotations

import logging
from typing import Protocol

from app.schemas import RetrievedChunk
from app.services.embeddings import EmbeddingService
from app.services.metrics import MetricsService, Stopwatch

logger = logging.getLogger(__name__)


class ChunkStore(Protocol):
    def search_chunks(
        self, user_id: int, vector: list[float], top_k: int, statement_id: int | None = None
    ) -> list[RetrievedChunk]: ...
    def get_chunks_by_kind(self, user_id: int, statement_id: int, kind: str) -> list[RetrievedChunk]: ...


class Retriever:
    def __init__(self, store: ChunkStore, embeddings: EmbeddingService, metrics: MetricsService, top_k: int = 6):
        self.store = store
        self.embeddings = embeddings
        self.metrics = metrics
        self.top_k = top_k

    def retrieve(self, user_id: int, question: str, statement_id: int | None = None) -> list[RetrievedChunk]:
        with Stopwatch() as sw:
            vector = self.embeddings.embed_query(question)
            hits = self.store.search_chunks(user_id, vector, self.top_k, statement_id)

            # El resumen (fechas, pagos, tasas) responde la mayoría de las preguntas: siempre va primero.
            summaries: list[RetrievedChunk] = []
            if statement_id is not None:
                summaries = self.store.get_chunks_by_kind(user_id, statement_id, "summary")
            seen = {(c.statement_id, c.kind, c.position) for c in summaries}
            chunks = summaries + [h for h in hits if (h.statement_id, h.kind, h.position) not in seen]

        self.metrics.record(
            operation="retrieval", provider="pgvector", model=self.embeddings.model, latency_ms=sw.ms,
            meta={"top_k": self.top_k, "returned": len(chunks), "best_score": max((h.score for h in hits), default=0)},
        )  # fmt: skip
        logger.info("Recuperados %d fragmentos en %.0f ms", len(chunks), sw.ms)
        return chunks
