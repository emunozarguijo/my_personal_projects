"""Modelos ORM (SQLAlchemy 2.0 + pgvector)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.config import get_settings

Money = Numeric(14, 2)
JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)  # siempre en minúsculas
    password_hash: Mapped[str | None] = mapped_column(String(255))  # None → no puede iniciar sesión
    role: Mapped[str] = mapped_column(String(10), default="user", server_default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(Base):
    """Sesión web. Solo se guarda el SHA-256 del token; el token vive en la cookie del navegador."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(200))


class Statement(Base):
    __tablename__ = "statements"
    # El mismo PDF puede pertenecer a varios usuarios; solo es duplicado dentro de la misma cuenta.
    __table_args__ = (UniqueConstraint("user_id", "file_hash", name="uq_statements_user_file_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    file_hash: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    bank: Mapped[str] = mapped_column(String(32), default="nu")
    card_last4: Mapped[str | None] = mapped_column(String(4))
    holder_name: Mapped[str | None] = mapped_column(String(200))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    cut_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    days_in_period: Mapped[int | None] = mapped_column(Integer)

    previous_balance: Mapped[Decimal | None] = mapped_column(Money)
    regular_charges: Mapped[Decimal | None] = mapped_column(Money)
    installment_capital: Mapped[Decimal | None] = mapped_column(Money)
    interest: Mapped[Decimal | None] = mapped_column(Money)
    commissions: Mapped[Decimal | None] = mapped_column(Money)
    iva: Mapped[Decimal | None] = mapped_column(Money)
    payments: Mapped[Decimal | None] = mapped_column(Money)
    pay_no_interest: Mapped[Decimal | None] = mapped_column(Money)
    min_payment: Mapped[Decimal | None] = mapped_column(Money)
    min_plus_installments: Mapped[Decimal | None] = mapped_column(Money)
    credit_limit: Mapped[Decimal | None] = mapped_column(Money)
    available_credit: Mapped[Decimal | None] = mapped_column(Money)
    cat: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    annual_rate: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    interest_last_12m: Mapped[Decimal | None] = mapped_column(Money)
    total_charges: Mapped[Decimal | None] = mapped_column(Money)
    total_payments: Mapped[Decimal | None] = mapped_column(Money)

    extra: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    transactions: Mapped[list["TransactionRow"]] = relationship(
        back_populates="statement", cascade="all, delete-orphan", order_by="TransactionRow.position"
    )
    installments: Mapped[list["InstallmentRow"]] = relationship(
        back_populates="statement", cascade="all, delete-orphan"
    )
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="statement", cascade="all, delete-orphan"
    )


class TransactionRow(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    operation_date: Mapped[date] = mapped_column(Date)
    charge_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(300))
    amount: Mapped[Decimal] = mapped_column(Money)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    detail: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(3), default="MXN")
    category: Mapped[str | None] = mapped_column(String(30))
    category_source: Mapped[str | None] = mapped_column(String(10))

    statement: Mapped[Statement] = relationship(back_populates="transactions")


class MerchantCategory(Base):
    """Corrección de categoría que hizo un usuario para un comercio; se aplica a sus cargos futuros."""

    __tablename__ = "merchant_categories"
    __table_args__ = (UniqueConstraint("user_id", "merchant_key", name="uq_merchant_categories_user_merchant"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    merchant_key: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(30))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class InstallmentRow(Base):
    __tablename__ = "installments"

    id: Mapped[int] = mapped_column(primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id", ondelete="CASCADE"), index=True)
    operation_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(300))
    original_amount: Mapped[Decimal] = mapped_column(Money)
    pending_balance: Mapped[Decimal] = mapped_column(Money)
    period_interest: Mapped[Decimal] = mapped_column(Money)
    period_iva: Mapped[Decimal] = mapped_column(Money)
    required_payment: Mapped[Decimal] = mapped_column(Money)
    current_installment: Mapped[int] = mapped_column(Integer)
    total_installments: Mapped[int] = mapped_column(Integer)
    annual_rate: Mapped[Decimal] = mapped_column(Numeric(7, 2))

    statement: Mapped[Statement] = relationship(back_populates="installments")


class DocumentChunk(Base):
    """Fragmento de texto + embedding para RAG."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Copia de statements.user_id: la búsqueda vectorial filtra por usuario sin JOIN.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    embedding: Mapped[list[float]] = mapped_column(Vector(get_settings().embedding_dim))
    embedding_model: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    statement: Mapped[Statement] = relationship(back_populates="chunks")


class ApiMetric(Base):
    """Una fila por llamada instrumentada: chat, embedding, retrieval o ingestion."""

    __tablename__ = "api_metrics"
    __table_args__ = (Index("ix_api_metrics_user_id_created_at", "user_id", "created_at"),)  # cuotas diarias

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    request_id: Mapped[str] = mapped_column(String(32), index=True)
    operation: Mapped[str] = mapped_column(String(20), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(10), default="ok")
    latency_ms: Mapped[float] = mapped_column(Float)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


class Income(Base):
    """Ingreso registrado a mano. `recurring` = se repite cada mes desde `date` hasta `end_date` (si existe)."""

    __tablename__ = "incomes"
    __table_args__ = (Index("ix_incomes_user_id_date", "user_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)
    kind: Mapped[str] = mapped_column(String(20), default="salario")
    description: Mapped[str] = mapped_column(String(120), default="")
    recurring: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    end_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CashExpense(Base):
    """Gasto en efectivo registrado a mano (los gastos con tarjeta salen de los estados de cuenta)."""

    __tablename__ = "cash_expenses"
    __table_args__ = (Index("ix_cash_expenses_user_id_date", "user_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money)
    description: Mapped[str] = mapped_column(String(120), default="")
    category: Mapped[str] = mapped_column(String(30))
    category_source: Mapped[str] = mapped_column(String(10), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
