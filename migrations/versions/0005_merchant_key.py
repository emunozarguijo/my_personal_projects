"""transactions.merchant_key: nombre de comercio normalizado (para corregir categorías con un solo UPDATE).

Llena la columna para los movimientos existentes con la misma normalización que usa la app.
Nota: importa `normalize_merchant` del código actual; si esa función cambiara, esta migración seguiría usando la versión
con la que se ejecute (aceptable: la columna se recalcula al guardar cada estado de cuenta nuevo).

Revision ID: 0005_merchant_key
Revises: 0004_manual_finance
Create Date: 2026-10-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0005_merchant_key"
down_revision = "0004_manual_finance"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return context.config.attributes.get("schema")


def upgrade() -> None:
    from app.services.categorizer import normalize_merchant

    schema = _schema()
    table = f'"{schema}".transactions' if schema else "transactions"
    op.add_column("transactions", sa.Column("merchant_key", sa.String(120)), schema=schema)

    conn = op.get_bind()
    rows = conn.execute(sa.text(f"SELECT id, description FROM {table}")).all()
    if rows:
        conn.execute(
            sa.text(f"UPDATE {table} SET merchant_key = :k WHERE id = :i"),
            [{"i": r.id, "k": normalize_merchant(r.description)} for r in rows],
        )
    op.create_index("ix_transactions_merchant_key", "transactions", ["merchant_key"], schema=schema)


def downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_transactions_merchant_key", table_name="transactions", schema=schema)
    op.drop_column("transactions", "merchant_key", schema=schema)
