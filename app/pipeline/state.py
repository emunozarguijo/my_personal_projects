"""Estados de los grafos de LangGraph. Cada nodo devuelve solo las claves que modifica."""
from __future__ import annotations

from typing import TypedDict

from app.schemas import Chunk, LLMResponse, ParsedStatement, RetrievedChunk


class IngestionState(TypedDict, total=False):
    path: str
    filename: str
    file_hash: str
    text: str
    parsed: ParsedStatement
    statement_id: int
    duplicate: bool
    chunks: list[Chunk]
    n_chunks: int
    error: str
    timings: dict[str, float]


class QAState(TypedDict, total=False):
    question: str
    statement_id: int | None
    history: list[dict[str, str]]
    chunks: list[RetrievedChunk]
    system: str
    messages: list[dict[str, str]]
    llm: LLMResponse
    answer: str
    error: str
    timings: dict[str, float]
