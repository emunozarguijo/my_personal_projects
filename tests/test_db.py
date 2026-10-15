"""Pruebas contra PostgreSQL + pgvector reales: migraciones y filtros por usuario en SQL.

Solo corren con RUN_DB_TESTS=1 (en CI, contra un Postgres desechable). Crean y borran las tablas en DB_SCHEMA,
por eso se niegan a correr si ese esquema ya tiene datos de la app.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.skipif(os.getenv("RUN_DB_TESTS") != "1", reason="define RUN_DB_TESTS=1 para usar PostgreSQL")

APP_TABLES = ("statements", "document_chunks", "api_metrics", "users", "alembic_version")


@pytest.fixture(scope="module")
def ctx():
    from alembic import command

    from app import migrate
    from app.core.config import Settings
    from app.core.database import Database

    settings = Settings(admin_username="Admin", embedding_provider="fake")
    db = Database(settings)
    db.ensure_schema_exists()
    schema = settings.db_schema
    existing = set(inspect(db.engine).get_table_names(schema=schema)) & set(APP_TABLES)
    if existing:
        pytest.fail(f"El esquema {schema or 'public'} ya tiene tablas de la app {sorted(existing)}: usa uno vacío.")

    cfg = migrate.alembic_config(settings)
    q = (lambda t: f'"{schema}".{t}') if schema else (lambda t: t)
    yield settings, db, cfg, q
    command.downgrade(cfg, "base")
    with db.engine.begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS {q('alembic_version')}"))


def _run(db, sql, **params):
    with db.engine.begin() as c:
        return c.execute(text(sql), params)


def test_migration_assigns_legacy_data_to_admin(ctx):
    from alembic import command

    from app import migrate

    settings, db, cfg, q = ctx
    command.upgrade(cfg, "0001_baseline")
    _run(db, f"DROP TABLE {q('alembic_version')}")  # simula la BD creada con create_all, sin Alembic
    sid = _run(db, f"""INSERT INTO {q('statements')} (file_hash, filename, bank, period_start, period_end, cut_date, extra)
                       VALUES ('h1', 'sep.pdf', 'nu', '2026-08-10', '2026-09-09', '2026-09-09', '{{}}') RETURNING id""").scalar()
    _run(db, f"""INSERT INTO {q('document_chunks')} (statement_id, kind, position, content, meta, embedding, embedding_model)
                 VALUES (:sid, 'summary', 0, 'x', '{{}}', :v, 'fake')""", sid=sid, v=str([0.1] * settings.embedding_dim))

    migrate.upgrade(settings)

    admin = _run(db, f"SELECT id, username, role FROM {q('users')}").one()
    assert admin.username == "admin" and admin.role == "admin"
    assert _run(db, f"SELECT user_id FROM {q('statements')}").scalar() == admin.id
    assert _run(db, f"SELECT user_id FROM {q('document_chunks')}").scalar() == admin.id
    db.verify_ready(migrate.head_revision(settings))
    _run(db, f"DELETE FROM {q('users')}")  # deja la BD limpia para las demás pruebas (CASCADE)
    assert _run(db, f"SELECT count(*) FROM {q('statements')}").scalar() == 0


@pytest.fixture
def repos(ctx):
    from app import migrate
    from app.services.repository import MetricsRepository, StatementRepository, UserRepository

    settings, db, _, q = ctx
    migrate.upgrade(settings)  # idempotente: no depende del orden de las pruebas
    yield StatementRepository(db), MetricsRepository(db), UserRepository(db)
    _run(db, f"DELETE FROM {q('users')}")
    _run(db, f"DELETE FROM {q('api_metrics')}")


@pytest.fixture
def parsed():
    from app.services.pdf_parser import parse_statement_text
    from tests.fakes import FIXTURE

    return parse_statement_text(FIXTURE.read_text(encoding="utf-8"))


def _index(statements, settings, user_id, parsed, file_hash):
    from app.services.chunking import StatementChunker
    from app.services.embeddings import FakeEmbeddingProvider

    sid = statements.save_statement(user_id, parsed, file_hash, "estado.pdf")
    chunks = StatementChunker(10).chunk(parsed)
    vectors = FakeEmbeddingProvider(settings.embedding_dim).embed([c.content for c in chunks]).vectors
    statements.add_chunks(user_id, sid, chunks, vectors, "fake")
    return sid, vectors[0]


def test_sql_filters_isolate_users(ctx, repos, parsed):
    settings, *_ = ctx
    statements, _, users = repos
    ana, beto = users.create("ana", None), users.create("beto", None)
    a_sid, vector = _index(statements, settings, ana.id, parsed, "mismo-hash")
    b_sid, _ = _index(statements, settings, beto.id, parsed, "mismo-hash")  # mismo PDF, otra cuenta: permitido

    assert [s.id for s in statements.list_statements(ana.id)] == [a_sid]
    assert statements.get_statement_detail(beto.id, a_sid) is None
    assert not statements.owns(beto.id, a_sid)
    assert statements.find_by_hash(beto.id, "mismo-hash") == b_sid
    assert statements.latest_statement_id(beto.id) == b_sid
    assert statements.get_chunks_by_kind(beto.id, a_sid, "summary") == []
    hits = statements.search_chunks(beto.id, vector, top_k=50)
    assert hits and {h.statement_id for h in hits} == {b_sid}
    assert statements.search_chunks(beto.id, vector, top_k=50, statement_id=a_sid) == []
    assert statements.delete_statement(beto.id, a_sid) is False
    assert statements.owns(ana.id, a_sid)

    with pytest.raises(IntegrityError):  # dentro de la misma cuenta sí es duplicado
        statements.save_statement(ana.id, parsed, "mismo-hash", "otra.pdf")


def test_metrics_quota_counts_are_per_user(repos):
    from datetime import datetime, timedelta, timezone

    _, metrics, users = repos
    ana, beto = users.create("ana", None), users.create("beto", None)
    row = dict(request_id="r", operation="chat", provider="p", model="m", status="ok", latency_ms=1.0,
               input_tokens=1, output_tokens=1, cost_usd=0.0, meta={})  # fmt: skip
    for _ in range(3):
        metrics.add(user_id=ana.id, **row)
    metrics.add(user_id=beto.id, **row)
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    assert metrics.count_since(ana.id, "chat", since) == 3
    assert metrics.count_since(beto.id, "chat", since) == 1
    assert sum(s.calls for s in metrics.summary(beto.id)) == 1
    assert sum(s.calls for s in metrics.summary(None)) == 4


def test_sessions_and_user_cascade(ctx, repos, parsed):
    from datetime import datetime, timedelta, timezone

    settings, db, _, q = ctx
    statements, _, users = repos
    ana = users.create("ana", "hash")
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    users.create_session(ana.id, "t" * 64, soon, "pytest")
    user, expires = users.get_session("t" * 64)
    assert user.username == "ana" and expires == soon
    users.delete_user_sessions(ana.id)
    assert users.get_session("t" * 64) is None

    _index(statements, settings, ana.id, parsed, "h")
    _run(db, f"DELETE FROM {q('users')} WHERE id = :id", id=ana.id)
    assert _run(db, f"SELECT count(*) FROM {q('document_chunks')}").scalar() == 0


def test_categories_in_sql_are_isolated_and_upserted(ctx, repos, parsed):
    from app.services.categorizer import TransactionCategorizer

    settings, *_ = ctx
    statements, _, users = repos
    ana, beto = users.create("ana", None), users.create("beto", None)
    categorized = parsed.model_copy(update={"transactions": TransactionCategorizer(statements).categorize(ana.id, parsed.transactions)})
    a_sid = statements.save_statement(ana.id, categorized, "h-ana", "a.pdf")
    b_sid = statements.save_statement(beto.id, parsed, "h-beto", "b.pdf")

    a_tx = statements.get_statement_detail(ana.id, a_sid).transactions
    sears = [t for t in a_tx if t.description == "Sears Metepec"]
    assert len(sears) == 2 and {t.category for t in sears} == {"hogar"} and all(t.id for t in sears)

    assert statements.set_transaction_category(beto.id, sears[0].id, "salud") == 0      # no es suyo
    assert statements.set_transaction_category(ana.id, sears[0].id, "salud") == 2
    assert statements.set_transaction_category(ana.id, sears[1].id, "ropa_cuidado") == 2  # upsert, no duplica
    assert statements.get_merchant_categories(ana.id, ["sears metepec"]) == {"sears metepec": "ropa_cuidado"}
    assert statements.get_merchant_categories(beto.id, ["sears metepec"]) == {}

    b_tx = statements.get_statement_detail(beto.id, b_sid).transactions
    statements.update_categories(beto.id, {sears[0].id: ("salud", "rule"), b_tx[4].id: ("hogar", "rule")})
    assert next(t for t in statements.get_statement_detail(ana.id, a_sid).transactions if t.id == sears[0].id).category == "ropa_cuidado"
    assert next(t for t in statements.get_statement_detail(beto.id, b_sid).transactions if t.id == b_tx[4].id).category == "hogar"

    assert [s.id for s in statements.list_statement_details(ana.id)] == [a_sid]
    assert sorted(statements.list_user_ids_with_statements()) == sorted([ana.id, beto.id])
