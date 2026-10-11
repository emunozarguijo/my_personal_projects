"""Multiusuario: cuentas, sesiones y user_id en los datos.

Los datos que ya existían se asignan a un usuario admin (ADMIN_USERNAME, por defecto "admin"). Si ADMIN_PASSWORD
está definido se usa como su contraseña; si no, la cuenta queda sin contraseña hasta ejecutar
`python -m app.main --reset-password <usuario>`.

Revision ID: 0002_multitenant
Revises: 0001_baseline
Create Date: 2026-10-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0002_multitenant"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return context.config.attributes.get("schema")


def _q(table: str) -> str:
    schema = _schema()
    return f'"{schema}".{table}' if schema else table


def _admin_password_hash() -> str | None:
    password = context.config.attributes["settings"].admin_password
    if not password:
        return None
    from pwdlib import PasswordHash

    return PasswordHash.recommended().hash(password.get_secret_value())


def upgrade() -> None:
    schema = _schema()
    settings = context.config.attributes["settings"]
    now = sa.text("now()")

    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("username", sa.String(32), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("role", sa.String(10), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        schema=schema,
    )
    users_fk = f"{schema}.users.id" if schema else "users.id"
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey(users_fk, ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent", sa.String(200)),
        schema=schema,
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"], schema=schema)

    for table, on_delete in (("statements", "CASCADE"), ("document_chunks", "CASCADE"), ("api_metrics", "SET NULL")):
        op.add_column(table, sa.Column("user_id", sa.Integer, nullable=True), schema=schema)
        op.create_foreign_key(
            f"{table}_user_id_fkey", table, "users", ["user_id"], ["id"],
            ondelete=on_delete, source_schema=schema, referent_schema=schema,
        )  # fmt: skip

    # --- Datos previos → cuenta admin
    conn = op.get_bind()
    has_data = conn.execute(
        sa.text(f"SELECT EXISTS (SELECT 1 FROM {_q('statements')}) OR EXISTS (SELECT 1 FROM {_q('api_metrics')})")
    ).scalar()
    if has_data:
        admin_id = conn.execute(
            sa.text(f"INSERT INTO {_q('users')} (username, password_hash, role) VALUES (:u, :p, 'admin') RETURNING id"),
            {"u": settings.admin_username.strip().lower(), "p": _admin_password_hash()},
        ).scalar()
        conn.execute(sa.text(f"UPDATE {_q('statements')} SET user_id = :id"), {"id": admin_id})
        conn.execute(sa.text(f"UPDATE {_q('api_metrics')} SET user_id = :id"), {"id": admin_id})
        conn.execute(
            sa.text(
                f"UPDATE {_q('document_chunks')} c SET user_id = s.user_id "
                f"FROM {_q('statements')} s WHERE s.id = c.statement_id"
            )
        )

    op.alter_column("statements", "user_id", nullable=False, schema=schema)
    op.alter_column("document_chunks", "user_id", nullable=False, schema=schema)

    # El mismo PDF ya no es duplicado entre cuentas distintas, solo dentro de la misma.
    op.drop_index("ix_statements_file_hash", table_name="statements", schema=schema)
    op.create_unique_constraint("uq_statements_user_file_hash", "statements", ["user_id", "file_hash"], schema=schema)
    op.create_index("ix_statements_user_id", "statements", ["user_id"], schema=schema)
    op.create_index("ix_document_chunks_user_id", "document_chunks", ["user_id"], schema=schema)
    op.create_index("ix_api_metrics_user_id_created_at", "api_metrics", ["user_id", "created_at"], schema=schema)


def downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_api_metrics_user_id_created_at", table_name="api_metrics", schema=schema)
    op.drop_index("ix_document_chunks_user_id", table_name="document_chunks", schema=schema)
    op.drop_index("ix_statements_user_id", table_name="statements", schema=schema)
    op.drop_constraint("uq_statements_user_file_hash", "statements", schema=schema)
    # Falla si dos cuentas cargaron el mismo PDF: hay que borrar uno antes de volver a un solo usuario.
    op.create_index("ix_statements_file_hash", "statements", ["file_hash"], unique=True, schema=schema)
    for table in ("api_metrics", "document_chunks", "statements"):
        op.drop_constraint(f"{table}_user_id_fkey", table, schema=schema)
        op.drop_column(table, "user_id", schema=schema)
    op.drop_table("user_sessions", schema=schema)
    op.drop_table("users", schema=schema)
