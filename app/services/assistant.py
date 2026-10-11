"""Fachada que usa la UI/CLI: oculta LangGraph, repositorios y servicios."""
from __future__ import annotations

import logging

from app.core.logging import log_event, request_context
from app.schemas import (
    ApiCallRecord,
    AskResult,
    IngestResult,
    OperationStats,
    StatementSummary,
)
from app.services.metrics import MetricsService, Stopwatch

logger = logging.getLogger(__name__)


class StatementAssistant:
    def __init__(self, ingestion_graph, qa_graph, statements, metrics_repo, metrics: MetricsService):
        self._ingest = ingestion_graph
        self._qa = qa_graph
        self.statements = statements
        self.metrics_repo = metrics_repo
        self._metrics = metrics

    # ------------------------------------------------------------- ingesta
    def ingest_pdf(self, path: str) -> IngestResult:
        with request_context(), Stopwatch() as sw:
            log_event(logger, "ingest.start", path=path)
            state = self._ingest.invoke({"path": path, "timings": {}})
        parsed = state.get("parsed")
        error = state.get("error")
        result = IngestResult(
            ok=error is None,
            statement_id=state.get("statement_id"),
            duplicate=bool(state.get("duplicate")),
            n_transactions=len(parsed.transactions) if parsed else 0,
            n_installments=len(parsed.installments) if parsed else 0,
            n_chunks=state.get("n_chunks", 0),
            warnings=parsed.warnings if parsed else [],
            error=error,
            timings_ms=state.get("timings", {}),
            total_ms=sw.ms,
        )
        self._metrics.record(
            operation="ingestion", provider="local", model="pipeline", latency_ms=sw.ms,
            status="ok" if result.ok else "error", error=error,
            meta={"duplicate": result.duplicate, "chunks": result.n_chunks, "timings": result.timings_ms},
        )  # fmt: skip
        log_event(logger, "ingest.end", ok=result.ok, duplicate=result.duplicate, ms=round(sw.ms, 1), error=error)
        return result

    # ------------------------------------------------------------- preguntas
    def ask(self, question: str, statement_id: int | None = None, history: list[dict] | None = None) -> AskResult:
        question = question.strip()
        if not question:
            return AskResult(ok=False, error="Escribe una pregunta.")
        if statement_id is None:
            statement_id = self.statements.latest_statement_id()
        if statement_id is None:
            return AskResult(ok=False, error="Primero carga un estado de cuenta en PDF.")

        with request_context(), Stopwatch() as sw:
            log_event(logger, "ask.start", statement_id=statement_id, question_chars=len(question))
            state = self._qa.invoke(
                {"question": question, "statement_id": statement_id, "history": history or [], "timings": {}}
            )
        error = state.get("error")
        log_event(logger, "ask.end", ok=error is None, ms=round(sw.ms, 1), error=error)
        return AskResult(
            ok=error is None,
            answer=state.get("answer", ""),
            sources=state.get("chunks", []),
            llm=state.get("llm"),
            error=error,
            timings_ms=state.get("timings", {}),
            total_ms=sw.ms,
        )

    # ------------------------------------------------------------- consultas para la UI
    def list_statements(self) -> list[StatementSummary]:
        return self.statements.list_statements()

    def statement_detail(self, statement_id: int):
        return self.statements.get_statement_detail(statement_id)

    def delete_statement(self, statement_id: int) -> None:
        self.statements.delete_statement(statement_id)

    def overview(self, statement_id: int) -> str:
        chunks = self.statements.get_chunks_by_kind(statement_id, "summary")
        return chunks[0].content if chunks else "Sin información."

    def metrics_summary(self) -> list[OperationStats]:
        return self.metrics_repo.summary()

    def recent_calls(self, limit: int = 50) -> list[ApiCallRecord]:
        return self.metrics_repo.recent(limit)
