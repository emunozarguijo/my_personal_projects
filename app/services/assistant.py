"""Fachada que usan la API, la GUI y la CLI: oculta LangGraph, repositorios y servicios.

Todos los métodos reciben el `user_id` dueño de los datos. La API lo toma de la sesión; la GUI y la CLI usan
`for_user(...)`, que devuelve la misma fachada ya atada a un usuario.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from app.core.exceptions import QuotaExceededError
from app.core.logging import log_event, request_context
from app.schemas import (
    ApiCallRecord,
    AskResult,
    IngestResult,
    OperationStats,
    StatementDetail,
    StatementSummary,
)
from app.services.metrics import MetricsService, Stopwatch

logger = logging.getLogger(__name__)

NOT_FOUND = "Ese estado de cuenta no existe."


@dataclass(frozen=True)
class UsageLimits:
    """Límites por usuario (0 = sin límite)."""

    max_asks_per_day: int = 0
    max_uploads_per_day: int = 0
    max_statements_per_user: int = 0


class StatementAssistant:
    def __init__(
        self, ingestion_graph, qa_graph, statements, metrics_repo, metrics: MetricsService,
        limits: UsageLimits = UsageLimits(),
    ):  # fmt: skip
        self._ingest = ingestion_graph
        self._qa = qa_graph
        self.statements = statements
        self.metrics_repo = metrics_repo
        self._metrics = metrics
        self.limits = limits

    def for_user(self, user_id: int) -> "UserAssistant":
        return UserAssistant(self, user_id)

    # ------------------------------------------------------------- límites de uso
    def ensure_quota(self, user_id: int, action: Literal["ask", "upload"]) -> None:
        """Lanza QuotaExceededError si el usuario ya no puede hacer `action` hoy (día UTC)."""
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if action == "ask":
            limit = self.limits.max_asks_per_day
            if limit and self.metrics_repo.count_since(user_id, "chat", today) >= limit:
                raise QuotaExceededError(f"Llegaste al límite de {limit} preguntas por día. Vuelve mañana.")
            return
        limit = self.limits.max_uploads_per_day
        if limit and self.metrics_repo.count_since(user_id, "ingestion", today) >= limit:
            raise QuotaExceededError(f"Llegaste al límite de {limit} cargas de PDF por día. Vuelve mañana.")
        cap = self.limits.max_statements_per_user
        if cap and self.statements.count_statements(user_id) >= cap:
            raise QuotaExceededError(
                f"Tu cuenta ya tiene {cap} estados de cuenta, el máximo. Elimina alguno para cargar otro."
            )

    # ------------------------------------------------------------- ingesta
    def ingest_pdf(self, user_id: int, path: str) -> IngestResult:
        with request_context(user_id=user_id), Stopwatch() as sw:
            log_event(logger, "ingest.start", path=path)
            state = self._ingest.invoke({"user_id": user_id, "path": path, "timings": {}})
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
    def ask(
        self, user_id: int, question: str, statement_id: int | None = None, history: list[dict] | None = None
    ) -> AskResult:
        question = question.strip()
        if not question:
            return AskResult(ok=False, error="Escribe una pregunta.")
        if statement_id is None:
            statement_id = self.statements.latest_statement_id(user_id)
            if statement_id is None:
                return AskResult(ok=False, error="Primero carga un estado de cuenta en PDF.")
        elif not self.statements.owns(user_id, statement_id):
            return AskResult(ok=False, error=NOT_FOUND)

        with request_context(user_id=user_id), Stopwatch() as sw:
            log_event(logger, "ask.start", statement_id=statement_id, question_chars=len(question))
            state = self._qa.invoke({
                "user_id": user_id, "question": question, "statement_id": statement_id,
                "history": history or [], "timings": {},
            })  # fmt: skip
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
    def owns(self, user_id: int, statement_id: int) -> bool:
        return self.statements.owns(user_id, statement_id)

    def list_statements(self, user_id: int) -> list[StatementSummary]:
        return self.statements.list_statements(user_id)

    def statement_detail(self, user_id: int, statement_id: int) -> StatementDetail | None:
        return self.statements.get_statement_detail(user_id, statement_id)

    def delete_statement(self, user_id: int, statement_id: int) -> bool:
        return self.statements.delete_statement(user_id, statement_id)

    def overview(self, user_id: int, statement_id: int) -> str:
        chunks = self.statements.get_chunks_by_kind(user_id, statement_id, "summary")
        return chunks[0].content if chunks else "Sin información."

    def metrics_summary(self, user_id: int | None) -> list[OperationStats]:
        """`user_id=None` → todas las cuentas (solo para admin; lo decide quien llama)."""
        return self.metrics_repo.summary(user_id)

    def recent_calls(self, user_id: int | None, limit: int = 50) -> list[ApiCallRecord]:
        return self.metrics_repo.recent(user_id, limit)


class UserAssistant:
    """La fachada atada a un usuario: misma interfaz que usaba la GUI/CLI antes del multiusuario."""

    def __init__(self, assistant: StatementAssistant, user_id: int):
        self._a = assistant
        self.user_id = user_id

    def ingest_pdf(self, path: str) -> IngestResult:
        return self._a.ingest_pdf(self.user_id, path)

    def ask(self, question: str, statement_id: int | None = None, history: list[dict] | None = None) -> AskResult:
        return self._a.ask(self.user_id, question, statement_id, history)

    def list_statements(self) -> list[StatementSummary]:
        return self._a.list_statements(self.user_id)

    def statement_detail(self, statement_id: int) -> StatementDetail | None:
        return self._a.statement_detail(self.user_id, statement_id)

    def delete_statement(self, statement_id: int) -> bool:
        return self._a.delete_statement(self.user_id, statement_id)

    def overview(self, statement_id: int) -> str:
        return self._a.overview(self.user_id, statement_id)

    def metrics_summary(self) -> list[OperationStats]:
        return self._a.metrics_summary(self.user_id)

    def recent_calls(self, limit: int = 50) -> list[ApiCallRecord]:
        return self._a.recent_calls(self.user_id, limit)
