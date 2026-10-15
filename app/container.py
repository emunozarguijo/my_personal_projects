"""Composition root: aquí (y solo aquí) se crean y conectan las dependencias."""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.core.database import Database
from app.core.logging import setup_logging
from app.pipeline.graph import build_ingestion_graph, build_qa_graph
from app.pipeline.nodes import IngestionNodes, QANodes
from app.services.assistant import StatementAssistant, UsageLimits
from app.services.auth import AuthService, LoginThrottle
from app.services.categorizer import AnthropicCategoryClassifier, TransactionCategorizer
from app.services.chunking import StatementChunker
from app.services.credit_health import build_report, report_to_text
from app.services.embeddings import EmbeddingService, build_provider
from app.services.finance import FinanceService, summary_to_text
from app.services.llm import LLMService, build_llm_provider
from app.services.metrics import MetricsService
from app.services.pdf_parser import PdfTextExtractor
from app.services.repository import FinanceRepository, MetricsRepository, StatementRepository, UserRepository
from app.services.retrieval import Retriever


def build_categorizer(settings: Settings, overrides, metrics: MetricsService) -> TransactionCategorizer:
    """Reglas siempre; LLM (Haiku) solo si está habilitado y hay API key de Anthropic."""
    classifier = None
    if settings.categorizer_llm and settings.anthropic_api_key:
        classifier = AnthropicCategoryClassifier(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.categorizer_model,
            metrics=metrics,
            price_in=settings.categorizer_price_input_per_mtok,
            price_out=settings.categorizer_price_output_per_mtok,
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )
    return TransactionCategorizer(overrides, classifier, settings.categorizer_min_confidence)


@dataclass
class AppServices:
    assistant: StatementAssistant
    auth: AuthService
    finance: FinanceService | None = None


def build_services(settings: Settings | None = None, check_db: bool = True) -> AppServices:
    settings = settings or get_settings()
    setup_logging(settings)

    db = Database(settings)
    if check_db:
        from app.migrate import head_revision

        db.verify_ready(head_revision(settings))

    statements = StatementRepository(db)
    metrics_repo = MetricsRepository(db)
    metrics = MetricsService(metrics_repo, settings)

    embeddings = EmbeddingService(build_provider(settings), metrics)
    llm = LLMService(build_llm_provider(settings), metrics)
    retriever = Retriever(statements, embeddings, metrics, top_k=settings.rag_top_k)

    ingestion_nodes = IngestionNodes(
        extractor=PdfTextExtractor(settings.max_pdf_mb),
        repo=statements,
        embeddings=embeddings,
        chunker=StatementChunker(settings.chunk_tx_per_chunk, settings.redact_pii),
        max_mb=settings.max_pdf_mb,
        categorizer=build_categorizer(settings, statements, metrics),
    )

    finance = FinanceService(FinanceRepository(db), statements, settings.max_manual_entries_per_day)

    def insights(user_id: int, statement_id: int | None) -> str:
        credit = report_to_text(build_report(statements.list_statement_details(user_id), statement_id))
        return f"{credit}\n\n{summary_to_text(finance.month(user_id))}"

    assistant = StatementAssistant(
        ingestion_graph=build_ingestion_graph(ingestion_nodes),
        qa_graph=build_qa_graph(QANodes(retriever, llm, insights=insights)),
        statements=statements,
        metrics_repo=metrics_repo,
        metrics=metrics,
        limits=UsageLimits(
            max_asks_per_day=settings.max_asks_per_day,
            max_uploads_per_day=settings.max_uploads_per_day,
            max_statements_per_user=settings.max_statements_per_user,
        ),
    )
    invite = settings.signup_invite_code.get_secret_value().strip() if settings.signup_invite_code else None
    auth = AuthService(
        UserRepository(db),
        LoginThrottle(settings.login_max_attempts, settings.login_window_minutes * 60),
        session_ttl_hours=settings.session_ttl_hours,
        invite_code=invite or None,
        max_users=settings.max_users,
    )
    return AppServices(assistant=assistant, auth=auth, finance=finance)
