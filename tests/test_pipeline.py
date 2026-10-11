from types import SimpleNamespace

import pytest

from app.pipeline.graph import build_ingestion_graph, build_qa_graph
from app.pipeline.nodes import IngestionNodes, QANodes
from app.services.chunking import StatementChunker
from app.services.embeddings import EmbeddingService, FakeEmbeddingProvider
from app.services.llm import LLMService
from app.services.metrics import MetricsService
from tests.fakes import FakeExtractor, FakeLLMProvider, InMemoryRepo, ListSink
from app.services.retrieval import Retriever


@pytest.fixture
def env(tmp_path):
    pdf = tmp_path / "estado.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")
    sink, repo = ListSink(), InMemoryRepo()
    metrics = MetricsService(sink, SimpleNamespace(price_input_per_mtok=3.0, price_output_per_mtok=15.0))
    embeddings = EmbeddingService(FakeEmbeddingProvider(64), metrics)
    llm_provider = FakeLLMProvider()
    llm = LLMService(llm_provider, metrics)
    ingest = build_ingestion_graph(IngestionNodes(FakeExtractor(), repo, embeddings, StatementChunker(10)))
    qa = build_qa_graph(QANodes(Retriever(repo, embeddings, metrics, top_k=3), llm))
    return SimpleNamespace(pdf=pdf, sink=sink, repo=repo, ingest=ingest, qa=qa, llm=llm_provider)


def test_ingestion_persists_and_indexes(env):
    state = env.ingest.invoke({"path": str(env.pdf), "timings": {}})
    assert not state.get("error")
    assert state["statement_id"] == 1 and state["n_chunks"] == len(env.repo.chunks) > 4
    assert set(state["timings"]) >= {"validate_file", "parse_statement", "embed_and_store"}
    assert {r["operation"] for r in env.sink.rows} == {"embedding"}


def test_second_ingestion_is_detected_as_duplicate(env):
    env.ingest.invoke({"path": str(env.pdf), "timings": {}})
    state = env.ingest.invoke({"path": str(env.pdf), "timings": {}})
    assert state["duplicate"] is True
    assert len(env.repo.statements) == 1


def test_invalid_file_stops_graph(env, tmp_path):
    bad = tmp_path / "x.txt"
    bad.write_text("hola")
    state = env.ingest.invoke({"path": str(bad), "timings": {}})
    assert "pdf" in state["error"].lower()
    assert "extract_text" not in state["timings"]


def test_question_answering_records_metrics(env):
    env.ingest.invoke({"path": str(env.pdf), "timings": {}})
    state = env.qa.invoke({"question": "¿Cuánto debo pagar para no generar intereses?", "statement_id": 1, "history": [], "timings": {}})
    assert not state.get("error")
    assert "17,510.97" in state["answer"]
    assert state["chunks"][0].kind == "summary"          # el resumen siempre va primero
    assert "<contexto>" in env.llm.last_messages[-1]["content"]
    chat = next(r for r in env.sink.rows if r["operation"] == "chat")
    assert (chat["input_tokens"], chat["output_tokens"]) == (1200, 40)
    assert chat["cost_usd"] == pytest.approx((1200 * 3 + 40 * 15) / 1e6)
    assert {r["operation"] for r in env.sink.rows} >= {"retrieval", "embedding", "chat"}


def test_llm_failure_is_reported_not_raised(env):
    env.ingest.invoke({"path": str(env.pdf), "timings": {}})
    env.llm.fail = True
    state = env.qa.invoke({"question": "hola", "statement_id": 1, "history": [], "timings": {}})
    assert "LLM" in state["error"]
    assert any(r["status"] == "error" for r in env.sink.rows if r["operation"] == "chat")
