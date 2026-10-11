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

ANA, BETO = 1, 2


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


def _ingest(env, user_id=ANA):
    return env.ingest.invoke({"user_id": user_id, "path": str(env.pdf), "timings": {}})


def _ask(env, question, statement_id, user_id=ANA):
    return env.qa.invoke(
        {"user_id": user_id, "question": question, "statement_id": statement_id, "history": [], "timings": {}}
    )


def test_ingestion_persists_and_indexes(env):
    state = _ingest(env)
    assert not state.get("error")
    assert state["statement_id"] == 1 and state["n_chunks"] == len(env.repo.chunks) > 4
    assert set(state["timings"]) >= {"validate_file", "parse_statement", "embed_and_store"}
    assert {r["operation"] for r in env.sink.rows} == {"embedding"}
    assert {uid for uid, *_ in env.repo.chunks} == {ANA}


def test_second_ingestion_is_detected_as_duplicate(env):
    _ingest(env)
    state = _ingest(env)
    assert state["duplicate"] is True
    assert len(env.repo.statements) == 1


def test_same_pdf_for_another_user_is_not_a_duplicate(env):
    first, second = _ingest(env, ANA), _ingest(env, BETO)
    assert not second.get("duplicate")
    assert first["statement_id"] != second["statement_id"]
    assert env.repo.count_statements(ANA) == env.repo.count_statements(BETO) == 1


def test_invalid_file_stops_graph(env, tmp_path):
    bad = tmp_path / "x.txt"
    bad.write_text("hola")
    state = env.ingest.invoke({"user_id": ANA, "path": str(bad), "timings": {}})
    assert "pdf" in state["error"].lower()
    assert "extract_text" not in state["timings"]


def test_question_answering_records_metrics(env):
    _ingest(env)
    state = _ask(env, "¿Cuánto debo pagar para no generar intereses?", 1)
    assert not state.get("error")
    assert "17,510.97" in state["answer"]
    assert state["chunks"][0].kind == "summary"          # el resumen siempre va primero
    assert "<contexto>" in env.llm.last_messages[-1]["content"]
    chat = next(r for r in env.sink.rows if r["operation"] == "chat")
    assert (chat["input_tokens"], chat["output_tokens"]) == (1200, 40)
    assert chat["cost_usd"] == pytest.approx((1200 * 3 + 40 * 15) / 1e6)
    assert {r["operation"] for r in env.sink.rows} >= {"retrieval", "embedding", "chat"}


def test_retrieval_never_crosses_users(env):
    ana_sid = _ingest(env, ANA)["statement_id"]
    _ingest(env, BETO)
    state = _ask(env, "¿Cuánto debo pagar?", None, user_id=BETO)  # sin statement_id: busca en todo lo de Beto
    assert state["chunks"]
    assert all(c.statement_id != ana_sid for c in state["chunks"])


def test_llm_failure_is_reported_not_raised(env):
    _ingest(env)
    env.llm.fail = True
    state = _ask(env, "hola", 1)
    assert "LLM" in state["error"]
    assert any(r["status"] == "error" for r in env.sink.rows if r["operation"] == "chat")
