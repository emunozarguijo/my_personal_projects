"""Dobles de prueba: permiten ejecutar los grafos completos sin PostgreSQL ni APIs externas."""
from __future__ import annotations

from pathlib import Path

from app.schemas import Chunk, ParsedStatement, RetrievedChunk, TokenUsage
from app.services.llm import LLMProvider

FIXTURE = Path(__file__).parent / "fixtures" / "nu_statement_sample.txt"


class FakeExtractor:
    def extract(self, path) -> str:
        return FIXTURE.read_text(encoding="utf-8")


class InMemoryRepo:
    """Implementa la misma interfaz que StatementRepository (la parte que usan los nodos)."""

    def __init__(self):
        self.statements: dict[int, tuple[ParsedStatement, str, str]] = {}
        self.chunks: list[tuple[int, Chunk, list[float]]] = []

    def find_by_hash(self, file_hash):
        return next((sid for sid, (_, h, _) in self.statements.items() if h == file_hash), None)

    def save_statement(self, parsed, file_hash, filename):
        sid = len(self.statements) + 1
        self.statements[sid] = (parsed, file_hash, filename)
        return sid

    def latest_statement_id(self):
        return max(self.statements, default=None)

    def add_chunks(self, statement_id, chunks, vectors, embedding_model):
        self.chunks += [(statement_id, c, v) for c, v in zip(chunks, vectors)]

    def get_chunks_by_kind(self, statement_id, kind):
        return [
            RetrievedChunk(statement_id=sid, kind=c.kind, position=c.position, content=c.content, score=1.0)
            for sid, c, _ in self.chunks if sid == statement_id and c.kind == kind
        ]  # fmt: skip

    def search_chunks(self, vector, top_k, statement_id=None):
        scored = [
            (sum(a * b for a, b in zip(vector, v)), sid, c)
            for sid, c, v in self.chunks if statement_id in (None, sid)
        ]  # fmt: skip
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            RetrievedChunk(statement_id=sid, kind=c.kind, position=c.position, content=c.content, score=s)
            for s, sid, c in scored[:top_k]
        ]  # fmt: skip


class ListSink:
    def __init__(self):
        self.rows: list[dict] = []

    def add(self, **fields):
        self.rows.append(fields)


class FakeLLMProvider(LLMProvider):
    name, model = "fake", "fake-llm"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.last_messages = None

    def complete(self, system, messages):
        if self.fail:
            raise RuntimeError("boom")
        self.last_messages = messages
        return "Debes pagar $17,510.97 antes del 21/09/2026 [1].", TokenUsage(input_tokens=1200, output_tokens=40)
