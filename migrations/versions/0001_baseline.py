"""Esquema inicial (un solo usuario), tal como lo creaba create_all antes de usar Alembic.

En una BD que ya tenía estas tablas, app.migrate la marca como aplicada (stamp) en lugar de ejecutarla.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

MONEY = sa.Numeric(14, 2)


def _schema() -> str | None:
    return context.config.attributes.get("schema")


def _fk(table: str) -> str:
    schema = _schema()
    return f"{schema}.{table}.id" if schema else f"{table}.id"


def upgrade() -> None:
    schema = _schema()
    dim = context.config.attributes["settings"].embedding_dim
    now = sa.text("now()")

    op.create_table(
        "statements",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("bank", sa.String(32), nullable=False),
        sa.Column("card_last4", sa.String(4)),
        sa.Column("holder_name", sa.String(200)),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("cut_date", sa.Date, nullable=False),
        sa.Column("due_date", sa.Date),
        sa.Column("days_in_period", sa.Integer),
        *[
            sa.Column(name, MONEY)
            for name in (
                "previous_balance regular_charges installment_capital interest commissions iva payments "
                "pay_no_interest min_payment min_plus_installments credit_limit available_credit"
            ).split()
        ],
        sa.Column("cat", sa.Numeric(7, 2)),
        sa.Column("annual_rate", sa.Numeric(7, 2)),
        sa.Column("interest_last_12m", MONEY),
        sa.Column("total_charges", MONEY),
        sa.Column("total_payments", MONEY),
        sa.Column("extra", JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        schema=schema,
    )
    op.create_index("ix_statements_file_hash", "statements", ["file_hash"], unique=True, schema=schema)

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("statement_id", sa.Integer, sa.ForeignKey(_fk("statements"), ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("operation_date", sa.Date, nullable=False),
        sa.Column("charge_date", sa.Date, nullable=False),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("detail", sa.Text),
        sa.Column("currency", sa.String(3), nullable=False),
        schema=schema,
    )
    op.create_index("ix_transactions_statement_id", "transactions", ["statement_id"], schema=schema)
    op.create_index("ix_transactions_kind", "transactions", ["kind"], schema=schema)

    op.create_table(
        "installments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("statement_id", sa.Integer, sa.ForeignKey(_fk("statements"), ondelete="CASCADE"), nullable=False),
        sa.Column("operation_date", sa.Date, nullable=False),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("original_amount", MONEY, nullable=False),
        sa.Column("pending_balance", MONEY, nullable=False),
        sa.Column("period_interest", MONEY, nullable=False),
        sa.Column("period_iva", MONEY, nullable=False),
        sa.Column("required_payment", MONEY, nullable=False),
        sa.Column("current_installment", sa.Integer, nullable=False),
        sa.Column("total_installments", sa.Integer, nullable=False),
        sa.Column("annual_rate", sa.Numeric(7, 2), nullable=False),
        schema=schema,
    )
    op.create_index("ix_installments_statement_id", "installments", ["statement_id"], schema=schema)

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("statement_id", sa.Integer, sa.ForeignKey(_fk("statements"), ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("meta", JSONB, nullable=False),
        sa.Column("embedding", Vector(dim), nullable=False),
        sa.Column("embedding_model", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        schema=schema,
    )
    op.create_index("ix_document_chunks_statement_id", "document_chunks", ["statement_id"], schema=schema)
    op.create_index("ix_document_chunks_kind", "document_chunks", ["kind"], schema=schema)
    op.create_index(
        "ix_document_chunks_embedding_hnsw", "document_chunks", ["embedding"], schema=schema,
        postgresql_using="hnsw", postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )  # fmt: skip

    op.create_table(
        "api_metrics",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column("request_id", sa.String(32), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("latency_ms", sa.Float, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("cost_usd", sa.Float, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("meta", JSONB, nullable=False),
        schema=schema,
    )
    op.create_index("ix_api_metrics_created_at", "api_metrics", ["created_at"], schema=schema)
    op.create_index("ix_api_metrics_request_id", "api_metrics", ["request_id"], schema=schema)
    op.create_index("ix_api_metrics_operation", "api_metrics", ["operation"], schema=schema)


def downgrade() -> None:
    schema = _schema()
    for table in ("api_metrics", "document_chunks", "installments", "transactions", "statements"):
        op.drop_table(table, schema=schema)
