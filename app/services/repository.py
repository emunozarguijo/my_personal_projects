"""Acceso a datos (PostgreSQL + pgvector). Es la única capa que conoce SQLAlchemy.

Aislamiento multiusuario: todo método que lee o escribe datos de un usuario recibe `user_id` como primer
argumento y lo usa en el WHERE. Un registro de otro usuario se trata igual que uno inexistente.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.core.database import Database
from app.core.exceptions import AuthError
from app.core.models import (
    ApiMetric,
    CashExpense,
    DocumentChunk,
    Income,
    InstallmentRow,
    MerchantCategory,
    Statement,
    TransactionRow,
    User,
    UserSession,
)
from app.schemas import (
    ApiCallRecord,
    CashExpenseItem,
    Chunk,
    IncomeItem,
    Installment,
    OperationStats,
    ParsedStatement,
    RetrievedChunk,
    StatementDetail,
    StatementSummary,
    Transaction,
    UserAccount,
)
from app.services.categorizer import normalize_merchant

logger = logging.getLogger(__name__)

_STATEMENT_FIELDS = (
    "bank card_last4 holder_name period_start period_end cut_date due_date days_in_period previous_balance "
    "regular_charges installment_capital interest commissions iva payments pay_no_interest min_payment "
    "min_plus_installments credit_limit available_credit cat annual_rate interest_last_12m total_charges "
    "total_payments"
).split()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StatementRepository:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- statements
    def find_by_hash(self, user_id: int, file_hash: str) -> int | None:
        with self.db.session() as s:
            return s.scalar(
                select(Statement.id).where(Statement.user_id == user_id, Statement.file_hash == file_hash)
            )

    def save_statement(self, user_id: int, parsed: ParsedStatement, file_hash: str, filename: str) -> int:
        with self.db.session() as s:
            row = Statement(
                user_id=user_id,
                file_hash=file_hash,
                filename=filename,
                extra={
                    "warnings": parsed.warnings,
                    "payment_scenarios": [sc.model_dump(mode="json") for sc in parsed.payment_scenarios],
                },
                **{f: getattr(parsed, f) for f in _STATEMENT_FIELDS},
            )
            row.transactions = [
                TransactionRow(
                    position=i, **t.model_dump(mode="python", exclude={"id", "deductible"}) | {"kind": t.kind.value}
                )
                for i, t in enumerate(parsed.transactions)
            ]
            row.installments = [InstallmentRow(**i.model_dump()) for i in parsed.installments]
            s.add(row)
            s.flush()
            return row.id

    def count_statements(self, user_id: int) -> int:
        with self.db.session() as s:
            return s.scalar(select(func.count()).select_from(Statement).where(Statement.user_id == user_id)) or 0

    def owns(self, user_id: int, statement_id: int) -> bool:
        with self.db.session() as s:
            return s.scalar(
                select(Statement.id).where(Statement.id == statement_id, Statement.user_id == user_id)
            ) is not None

    def list_statements(self, user_id: int) -> list[StatementSummary]:
        with self.db.session() as s:
            rows = s.scalars(
                select(Statement).where(Statement.user_id == user_id).order_by(Statement.period_end.desc())
            ).all()
            return [
                StatementSummary(
                    id=r.id, filename=r.filename, card_last4=r.card_last4, period_start=r.period_start,
                    period_end=r.period_end, due_date=r.due_date, pay_no_interest=r.pay_no_interest,
                )  # fmt: skip
                for r in rows
            ]

    @staticmethod
    def _detail(row: Statement) -> StatementDetail:
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

    def get_statement_detail(self, user_id: int, statement_id: int) -> StatementDetail | None:
        with self.db.session() as s:
            row = s.scalar(select(Statement).where(Statement.id == statement_id, Statement.user_id == user_id))
            return self._detail(row) if row else None

    def list_statement_details(self, user_id: int) -> list[StatementDetail]:
        """Todos los estados de cuenta del usuario, del más antiguo al más reciente (para tendencias)."""
        with self.db.session() as s:
            rows = s.scalars(
                select(Statement).where(Statement.user_id == user_id).order_by(Statement.period_end)
            ).all()
            return [self._detail(r) for r in rows]

    def list_user_ids_with_statements(self) -> list[int]:
        with self.db.session() as s:
            return list(s.scalars(select(Statement.user_id).distinct()).all())

    # ------------------------------------------------------------- categorías
    def get_merchant_categories(self, user_id: int, merchant_keys: list[str]) -> dict[str, str]:
        if not merchant_keys:
            return {}
        with self.db.session() as s:
            rows = s.execute(
                select(MerchantCategory.merchant_key, MerchantCategory.category).where(
                    MerchantCategory.user_id == user_id, MerchantCategory.merchant_key.in_(merchant_keys)
                )
            ).all()
            return dict(rows)

    def set_transaction_category(self, user_id: int, transaction_id: int, category: str) -> int:
        """Corrige un cargo y recuerda la categoría para ese comercio: se aplica a todos los cargos del usuario
        con el mismo comercio (y a los futuros). Devuelve cuántos movimientos cambió; 0 si no es suyo."""
        with self.db.session() as s:
            tx = s.scalar(
                select(TransactionRow)
                .join(Statement, Statement.id == TransactionRow.statement_id)
                .where(TransactionRow.id == transaction_id, Statement.user_id == user_id)
            )
            if tx is None:
                return 0
            key = normalize_merchant(tx.description)
            s.execute(
                pg_insert(MerchantCategory)
                .values(user_id=user_id, merchant_key=key, category=category)
                .on_conflict_do_update(
                    constraint="uq_merchant_categories_user_merchant",
                    set_={"category": category, "updated_at": func.now()},
                )
            )
            same = s.scalars(
                select(TransactionRow)
                .join(Statement, Statement.id == TransactionRow.statement_id)
                .where(Statement.user_id == user_id, TransactionRow.kind == "purchase")
            ).all()
            changed = 0
            for row in same:
                if normalize_merchant(row.description) == key:
                    row.category, row.category_source = category, "user"
                    changed += 1
            return changed

    def update_categories(self, user_id: int, categories: dict[int, tuple[str, str]]) -> None:
        """{transaction_id: (categoría, origen)}; ignora ids que no sean del usuario."""
        if not categories:
            return
        with self.db.session() as s:
            own = set(s.scalars(
                select(TransactionRow.id)
                .join(Statement, Statement.id == TransactionRow.statement_id)
                .where(Statement.user_id == user_id, TransactionRow.id.in_(list(categories)))
            ).all())  # fmt: skip
            for tx_id in own:
                cat, source = categories[tx_id]
                s.execute(update(TransactionRow).where(TransactionRow.id == tx_id)
                          .values(category=cat, category_source=source))  # fmt: skip

    def latest_statement_id(self, user_id: int) -> int | None:
        with self.db.session() as s:
            return s.scalar(
                select(Statement.id)
                .where(Statement.user_id == user_id)
                .order_by(Statement.period_end.desc())
                .limit(1)
            )

    def delete_statement(self, user_id: int, statement_id: int) -> bool:
        with self.db.session() as s:
            result = s.execute(
                delete(Statement).where(Statement.id == statement_id, Statement.user_id == user_id)
            )
            return result.rowcount > 0

    # ------------------------------------------------------------- chunks / vectors
    def add_chunks(
        self, user_id: int, statement_id: int, chunks: list[Chunk], vectors: list[list[float]], embedding_model: str
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks y vectors deben tener la misma longitud")
        with self.db.session() as s:
            s.add_all(
                DocumentChunk(
                    user_id=user_id, statement_id=statement_id, kind=c.kind, position=c.position,
                    content=c.content, meta=c.meta, embedding=v, embedding_model=embedding_model,
                )  # fmt: skip
                for c, v in zip(chunks, vectors)
            )

    def get_chunks_by_kind(self, user_id: int, statement_id: int, kind: str) -> list[RetrievedChunk]:
        with self.db.session() as s:
            rows = s.scalars(
                select(DocumentChunk)
                .where(
                    DocumentChunk.user_id == user_id,
                    DocumentChunk.statement_id == statement_id,
                    DocumentChunk.kind == kind,
                )
                .order_by(DocumentChunk.position)
            ).all()
            return [self._to_chunk(r, 1.0) for r in rows]

    def search_chunks(
        self, user_id: int, vector: list[float], top_k: int, statement_id: int | None = None
    ) -> list[RetrievedChunk]:
        distance = DocumentChunk.embedding.cosine_distance(vector).label("distance")
        stmt = select(DocumentChunk, distance).where(DocumentChunk.user_id == user_id).order_by(distance).limit(top_k)
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
    """Implementa el 'sink' que usa MetricsService. `user_id=None` en las consultas = todas las cuentas (admin)."""

    def __init__(self, db: Database):
        self.db = db

    def add(self, **fields: Any) -> None:
        with self.db.session() as s:
            s.add(ApiMetric(**fields))

    def count_since(self, user_id: int, operation: str, since: datetime) -> int:
        with self.db.session() as s:
            return s.scalar(
                select(func.count())
                .select_from(ApiMetric)
                .where(ApiMetric.user_id == user_id, ApiMetric.operation == operation, ApiMetric.created_at >= since)
            ) or 0

    def summary(self, user_id: int | None) -> list[OperationStats]:
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
        if user_id is not None:
            stmt = stmt.where(ApiMetric.user_id == user_id)
        with self.db.session() as s:
            return [
                OperationStats(
                    operation=op, calls=n, errors=err, avg_latency_ms=float(avg or 0),
                    p95_latency_ms=float(p or 0), input_tokens=int(tin), output_tokens=int(tout),
                    cost_usd=float(cost),
                )  # fmt: skip
                for op, n, err, avg, p, tin, tout, cost in s.execute(stmt).all()
            ]

    def recent(self, user_id: int | None, limit: int = 50) -> list[ApiCallRecord]:
        stmt = select(ApiMetric).order_by(ApiMetric.id.desc()).limit(limit)
        if user_id is not None:
            stmt = stmt.where(ApiMetric.user_id == user_id)
        with self.db.session() as s:
            rows = s.scalars(stmt).all()
            return [
                ApiCallRecord(
                    created_at=r.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                    operation=r.operation, provider=r.provider, model=r.model, status=r.status,
                    latency_ms=r.latency_ms, input_tokens=r.input_tokens,
                    output_tokens=r.output_tokens, request_id=r.request_id,
                )  # fmt: skip
                for r in rows
            ]


class UserRepository:
    """Cuentas y sesiones. Los nombres de usuario llegan ya normalizados (minúsculas) desde AuthService."""

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _account(row: User) -> UserAccount:
        return UserAccount(
            id=row.id, username=row.username, role=row.role, is_active=row.is_active,
            password_hash=row.password_hash,
        )  # fmt: skip

    def get_by_username(self, username: str) -> UserAccount | None:
        with self.db.session() as s:
            row = s.scalar(select(User).where(User.username == username))
            return self._account(row) if row else None

    def get_by_id(self, user_id: int) -> UserAccount | None:
        with self.db.session() as s:
            row = s.get(User, user_id)
            return self._account(row) if row else None

    def count_users(self) -> int:
        with self.db.session() as s:
            return s.scalar(select(func.count()).select_from(User)) or 0

    def create(self, username: str, password_hash: str | None, role: str = "user") -> UserAccount:
        try:
            with self.db.session() as s:
                row = User(username=username, password_hash=password_hash, role=role)
                s.add(row)
                s.flush()
                return self._account(row)
        except IntegrityError as exc:  # dos registros simultáneos con el mismo nombre
            raise AuthError("Ese nombre de usuario ya está en uso.") from exc

    def set_password(self, user_id: int, password_hash: str) -> None:
        with self.db.session() as s:
            s.execute(update(User).where(User.id == user_id).values(password_hash=password_hash))

    def set_role(self, user_id: int, role: str) -> None:
        with self.db.session() as s:
            s.execute(update(User).where(User.id == user_id).values(role=role))

    def touch_login(self, user_id: int) -> None:
        with self.db.session() as s:
            s.execute(update(User).where(User.id == user_id).values(last_login_at=_utcnow()))

    # ------------------------------------------------------------- sesiones
    def create_session(self, user_id: int, token_hash: str, expires_at: datetime, user_agent: str | None) -> None:
        with self.db.session() as s:
            s.add(UserSession(user_id=user_id, token_hash=token_hash, expires_at=expires_at,
                              user_agent=(user_agent or "")[:200] or None))
            # De paso, limpia sesiones vencidas de este usuario.
            s.execute(delete(UserSession).where(UserSession.user_id == user_id, UserSession.expires_at < _utcnow()))

    def get_session(self, token_hash: str) -> tuple[UserAccount, datetime] | None:
        with self.db.session() as s:
            row = s.execute(
                select(User, UserSession.expires_at)
                .join(UserSession, UserSession.user_id == User.id)
                .where(UserSession.token_hash == token_hash)
            ).first()
            return (self._account(row[0]), row[1]) if row else None

    def extend_session(self, token_hash: str, expires_at: datetime) -> None:
        with self.db.session() as s:
            s.execute(update(UserSession).where(UserSession.token_hash == token_hash).values(expires_at=expires_at))

    def delete_session(self, token_hash: str) -> None:
        with self.db.session() as s:
            s.execute(delete(UserSession).where(UserSession.token_hash == token_hash))

    def delete_user_sessions(self, user_id: int, except_token_hash: str | None = None) -> None:
        stmt = delete(UserSession).where(UserSession.user_id == user_id)
        if except_token_hash:
            stmt = stmt.where(UserSession.token_hash != except_token_hash)
        with self.db.session() as s:
            s.execute(stmt)


class FinanceRepository:
    """Ingresos y gastos en efectivo registrados a mano. Siempre filtrado por usuario."""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- ingresos
    def add_income(self, user_id: int, **fields) -> IncomeItem:
        with self.db.session() as s:
            row = Income(user_id=user_id, **fields)
            s.add(row)
            s.flush()
            return IncomeItem.model_validate(row, from_attributes=True)

    def update_income(self, user_id: int, income_id: int, **fields) -> IncomeItem | None:
        with self.db.session() as s:
            row = s.scalar(select(Income).where(Income.id == income_id, Income.user_id == user_id))
            if row is None:
                return None
            for k, v in fields.items():
                setattr(row, k, v)
            s.flush()
            return IncomeItem.model_validate(row, from_attributes=True)

    def delete_income(self, user_id: int, income_id: int) -> bool:
        with self.db.session() as s:
            return s.execute(delete(Income).where(Income.id == income_id, Income.user_id == user_id)).rowcount > 0

    def incomes_for_range(self, user_id: int, start: date, end: date) -> list[IncomeItem]:
        """Ingresos únicos dentro del rango + recurrentes activos en algún día del rango."""
        once = and_(Income.recurring.is_(False), Income.date >= start, Income.date <= end)
        repeating = and_(Income.recurring.is_(True), Income.date <= end,
                         or_(Income.end_date.is_(None), Income.end_date >= start))  # fmt: skip
        with self.db.session() as s:
            rows = s.scalars(
                select(Income).where(Income.user_id == user_id, or_(once, repeating)).order_by(Income.date, Income.id)
            ).all()
            return [IncomeItem.model_validate(r, from_attributes=True) for r in rows]

    # ------------------------------------------------------------- gastos en efectivo
    def add_cash_expense(self, user_id: int, **fields) -> CashExpenseItem:
        with self.db.session() as s:
            row = CashExpense(user_id=user_id, **fields)
            s.add(row)
            s.flush()
            return CashExpenseItem.model_validate(row, from_attributes=True)

    def update_cash_expense(self, user_id: int, expense_id: int, **fields) -> CashExpenseItem | None:
        with self.db.session() as s:
            row = s.scalar(select(CashExpense).where(CashExpense.id == expense_id, CashExpense.user_id == user_id))
            if row is None:
                return None
            for k, v in fields.items():
                setattr(row, k, v)
            s.flush()
            return CashExpenseItem.model_validate(row, from_attributes=True)

    def delete_cash_expense(self, user_id: int, expense_id: int) -> bool:
        with self.db.session() as s:
            stmt = delete(CashExpense).where(CashExpense.id == expense_id, CashExpense.user_id == user_id)
            return s.execute(stmt).rowcount > 0

    def cash_for_range(self, user_id: int, start: date, end: date) -> list[CashExpenseItem]:
        with self.db.session() as s:
            rows = s.scalars(
                select(CashExpense)
                .where(CashExpense.user_id == user_id, CashExpense.date >= start, CashExpense.date <= end)
                .order_by(CashExpense.date, CashExpense.id)
            ).all()
            return [CashExpenseItem.model_validate(r, from_attributes=True) for r in rows]

    def count_created_since(self, user_id: int, since: datetime) -> int:
        with self.db.session() as s:
            n_inc = s.scalar(select(func.count()).select_from(Income)
                             .where(Income.user_id == user_id, Income.created_at >= since)) or 0  # fmt: skip
            n_cash = s.scalar(select(func.count()).select_from(CashExpense)
                              .where(CashExpense.user_id == user_id, CashExpense.created_at >= since)) or 0  # fmt: skip
            return n_inc + n_cash
