"""Acceso a datos (PostgreSQL + pgvector). Es la única capa que conoce SQLAlchemy."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, func, select

from app.core.database import Database
from app.core.models import (
    ApiMetric,
    DocumentChunk,
    InstallmentRow,
    Statement,
    TransactionRow,
)
from app.schemas import (
    ApiCallRecord,
    Chunk,
    Installment,
    OperationStats,
    ParsedStatement,
    RetrievedChunk,
    StatementDetail,
    StatementSummary,
    Transaction,
)

logger = logging.getLogger(__name__)

_STATEMENT_FIELDS = (
    "bank card_last4 holder_name period_start period_end cut_date due_date days_in_period previous_balance "
    "regular_charges installment_capital interest commissions iva payments pay_no_interest min_payment "
    "min_plus_installments credit_limit available_credit cat annual_rate interest_last_12m total_charges "
    "total_payments"
).split()


class StatementRepository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- statements
    def find_by_hash(self, file_hash: str) -> int | None:
        with self.db.session() as s:
            return s.scalar(select(Statement.id).where(Statement.file_hash == file_hash))

    def save_statement(self, parsed: ParsedStatement, file_hash: str, filename: str) -> int:
        with self.db.session() as s:
            row = Statement(
                file_hash=file_hash,
                filename=filename,
                extra={
                    "warnings": parsed.warnings,
                    "payment_scenarios": [sc.model_dump(mode="json") for sc in parsed.payment_scenarios],
                },
                **{f: getattr(parsed, f) for f in _STATEMENT_FIELDS},
            )
            row.transactions = [
                TransactionRow(position=i, **t.model_dump(mode="python") | {"kind": t.kind.value})
                for i, t in enumerate(parsed.transactions)
            ]
            row.installments = [InstallmentRow(**i.model_dump()) for i in parsed.installments]
            s.add(row)
            s.flush()
            return row.id

    def list_statements(self) -> list[StatementSummary]:
        with self.db.session() as s:
            rows = s.scalars(select(Statement).order_by(Statement.period_end.desc())).all()
            return [
                StatementSummary(
                    id=r.id, filename=r.filename, card_last4=r.card_last4, period_start=r.period_start,
                    period_end=r.period_end, due_date=r.due_date, pay_no_interest=r.pay_no_interest,
                )  # fmt: skip
                for r in rows
            ]

    def get_statement_detail(self, statement_id: int) -> StatementDetail | None:
        with self.db.session() as s:
            row = s.get(Statement, statement_id)
            if row is None:
                return None
            extra = row.extra or {}
            return StatementDetail(
                id=row.id,
                filename=row.filename,
                **{f: getattr(row, f) for f in _STATEMENT_FIELDS},
                payment_scenarios=extra.get("payment_scenarios", []),
                warnings=extra.get("warnings", []),
                transactions=[Transaction.model_validate(t, from_attributes=True) for t in row.transactions],
                installments=[Installment.model_validate(i, from_attributes=True) for i in row.installments],
            )

    def latest_statement_id(self) -> int | None:
        with self.db.session() as s:
            return s.scalar(select(Statement.id).order_by(Statement.period_end.desc()).limit(1))

    def delete_statement(self, statement_id: int) -> None:
        with self.db.session() as s:
            s.execute(delete(Statement).where(Statement.id == statement_id))

    # ------------------------------------------------------------- chunks / vectors
    def add_chunks(
        self, statement_id: int, chunks: list[Chunk], vectors: list[list[float]], embedding_model: str
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks y vectors deben tener la misma longitud")
        with self.db.session() as s:
            s.add_all(
                DocumentChunk(
                    statement_id=statement_id, kind=c.kind, position=c.position, content=c.content,
                    meta=c.meta, embedding=v, embedding_model=embedding_model,
                )  # fmt: skip
                for c, v in zip(chunks, vectors)
            )

    def get_chunks_by_kind(self, statement_id: int, kind: str) -> list[RetrievedChunk]:
        with self.db.session() as s:
            rows = s.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.statement_id == statement_id, DocumentChunk.kind == kind)
                .order_by(DocumentChunk.position)
            ).all()
            return [self._to_chunk(r, 1.0) for r in rows]

    def search_chunks(
        self, vector: list[float], top_k: int, statement_id: int | None = None
    ) -> list[RetrievedChunk]:
        distance = DocumentChunk.embedding.cosine_distance(vector).label("distance")
        stmt = select(DocumentChunk, distance).order_by(distance).limit(top_k)
        if statement_id is not None:
            stmt = stmt.where(DocumentChunk.statement_id == statement_id)
        with self.db.session() as s:
            return [self._to_chunk(row, 1.0 - float(dist)) for row, dist in s.execute(stmt).all()]

    @staticmethod
    def _to_chunk(row: DocumentChunk, score: float) -> RetrievedChunk:
        return RetrievedChunk(
            statement_id=row.statement_id, kind=row.kind, position=row.position,
            content=row.content, meta=row.meta or {}, score=score,
        )  # fmt: skip


class MetricsRepository:
    """Implementa el 'sink' que usa MetricsService."""

    def __init__(self, db: Database):
        self.db = db

    def add(self, **fields: Any) -> None:
        with self.db.session() as s:
            s.add(ApiMetric(**fields))

    def summary(self) -> list[OperationStats]:
        p95 = func.percentile_cont(0.95).within_group(ApiMetric.latency_ms)
        stmt = select(
            ApiMetric.operation,
            func.count(),
            func.count().filter(ApiMetric.status == "error"),
            func.avg(ApiMetric.latency_ms),
            p95,
            func.coalesce(func.sum(ApiMetric.input_tokens), 0),
            func.coalesce(func.sum(ApiMetric.output_tokens), 0),
            func.coalesce(func.sum(ApiMetric.cost_usd), 0.0),
        ).group_by(ApiMetric.operation).order_by(ApiMetric.operation)
        with self.db.session() as s:
            return [
                OperationStats(
                    operation=op, calls=n, errors=err, avg_latency_ms=float(avg or 0),
                    p95_latency_ms=float(p or 0), input_tokens=int(tin), output_tokens=int(tout),
                    cost_usd=float(cost),
                )  # fmt: skip
                for op, n, err, avg, p, tin, tout, cost in s.execute(stmt).all()
            ]

    def recent(self, limit: int = 50) -> list[ApiCallRecord]:
        with self.db.session() as s:
            rows = s.scalars(select(ApiMetric).order_by(ApiMetric.id.desc()).limit(limit)).all()
            return [
                ApiCallRecord(
                    created_at=r.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                    operation=r.operation, provider=r.provider, model=r.model, status=r.status,
                    latency_ms=r.latency_ms, input_tokens=r.input_tokens,
                    output_tokens=r.output_tokens, request_id=r.request_id,
                )  # fmt: skip
                for r in rows
            ]
