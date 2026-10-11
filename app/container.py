"""Composition root: aquí (y solo aquí) se crean y conectan las dependencias."""
from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.database import Database
from app.core.logging import setup_logging
from app.pipeline.graph import build_ingestion_graph, build_qa_graph
from app.pipeline.nodes import IngestionNodes, QANodes
from app.services.assistant import StatementAssistant
from app.services.chunking import StatementChunker
from app.services.embeddings import EmbeddingService, build_provider
from app.services.llm import LLMService, build_llm_provider
from app.services.metrics import MetricsService
from app.services.pdf_parser import PdfTextExtractor
from app.services.repository import MetricsRepository, StatementRepository
from app.services.retrieval import Retriever


def build_assistant(settings: Settings | None = None, init_db: bool = True) -> StatementAssistant:
    settings = settings or get_settings()
    setup_logging(settings)

    db = Database(settings)
    if init_db:
        db.init_schema()

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
    )
    return StatementAssistant(
        ingestion_graph=build_ingestion_graph(ingestion_nodes),
        qa_graph=build_qa_graph(QANodes(retriever, llm)),
        statements=statements,
        metrics_repo=metrics_repo,
        metrics=metrics,
    )
