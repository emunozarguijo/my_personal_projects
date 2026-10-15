"""Clasifica las compras que se cargaron antes de existir las categorías:  python -m app.categorize_backfill

Solo toca compras sin categoría (nunca las corregidas por el usuario). Es idempotente: se puede correr varias veces.
Usa la misma cascada que la carga de PDFs (correcciones del usuario → reglas → LLM).
"""
from __future__ import annotations

import sys

from app.container import build_categorizer
from app.core.config import get_settings
from app.core.database import Database
from app.core.exceptions import AppError
from app.core.logging import request_context, setup_logging
from app.migrate import head_revision
from app.schemas import TransactionKind
from app.services.metrics import MetricsService
from app.services.repository import MetricsRepository, StatementRepository


def backfill() -> dict[int, int]:
    settings = get_settings()
    setup_logging(settings)
    db = Database(settings)
    db.verify_ready(head_revision(settings))
    statements = StatementRepository(db)
    categorizer = build_categorizer(settings, statements, MetricsService(MetricsRepository(db), settings))

    done: dict[int, int] = {}
    for user_id in statements.list_user_ids_with_statements():
        with request_context(user_id=user_id):
            pending = [
                t for st in statements.list_statement_details(user_id) for t in st.transactions
                if t.kind == TransactionKind.PURCHASE and t.category is None
            ]  # fmt: skip
            if not pending:
                continue
            categorized = categorizer.categorize(user_id, pending)
            statements.update_categories(user_id, {t.id: (t.category, t.category_source) for t in categorized})
            done[user_id] = len(categorized)
    return done


def main() -> int:
    try:
        done = backfill()
    except AppError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    total = sum(done.values())
    print(f"Compras clasificadas: {total} en {len(done)} cuenta(s)." if total else "No había compras sin clasificar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
