"""Categorías de gasto: category / category_source en transactions y correcciones por usuario.

Los movimientos ya existentes quedan sin categoría; se clasifican con `python -m app.categorize_backfill`.

Revision ID: 0003_categories
Revises: 0002_multitenant
Create Date: 2026-10-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0003_categories"
down_revision = "0002_multitenant"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return context.config.attributes.get("schema")


def upgrade() -> None:
    schema = _schema()
    op.add_column("transactions", sa.Column("category", sa.String(30)), schema=schema)
    op.add_column("transactions", sa.Column("category_source", sa.String(10)), schema=schema)

    users_fk = f"{schema}.users.id" if schema else "users.id"
    op.create_table(
        "merchant_categories",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey(users_fk, ondelete="CASCADE"), nullable=False),
        sa.Column("merchant_key", sa.String(120), nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("user_id", "merchant_key", name="uq_merchant_categories_user_merchant"),
        schema=schema,
    )
    op.create_index("ix_merchant_categories_user_id", "merchant_categories", ["user_id"], schema=schema)


def downgrade() -> None:
    schema = _schema()
    op.drop_table("merchant_categories", schema=schema)
    op.drop_column("transactions", "category_source", schema=schema)
    op.drop_column("transactions", "category", schema=schema)
