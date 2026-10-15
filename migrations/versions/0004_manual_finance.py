"""Finanzas manuales: ingresos (únicos o recurrentes) y gastos en efectivo.

Revision ID: 0004_manual_finance
Revises: 0003_categories
Create Date: 2026-10-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0004_manual_finance"
down_revision = "0003_categories"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(14, 2)


def _schema() -> str | None:
    return context.config.attributes.get("schema")


def upgrade() -> None:
    schema = _schema()
    users_fk = f"{schema}.users.id" if schema else "users.id"
    now = sa.text("now()")

    op.create_table(
        "incomes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey(users_fk, ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("description", sa.String(120), nullable=False),
        sa.Column("recurring", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("end_date", sa.Date),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        schema=schema,
    )
    op.create_index("ix_incomes_user_id_date", "incomes", ["user_id", "date"], schema=schema)

    op.create_table(
        "cash_expenses",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey(users_fk, ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("description", sa.String(120), nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("category_source", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        schema=schema,
    )
    op.create_index("ix_cash_expenses_user_id_date", "cash_expenses", ["user_id", "date"], schema=schema)


def downgrade() -> None:
    schema = _schema()
    op.drop_table("cash_expenses", schema=schema)
    op.drop_table("incomes", schema=schema)
