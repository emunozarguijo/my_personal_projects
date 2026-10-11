"""Nodos del pipeline. Cada nodo es un método de una clase con sus dependencias inyectadas,
así se pueden probar con dobles y reutilizar en cualquier grafo de LangGraph."""
from __future__ import annotations

import functools
import hashlib
import logging
from pathlib import Path
from typing import Callable

from app.core.exceptions import AppError, PdfExtractionError
from app.core.logging import log_event
from app.core.prompts import SYSTEM_PROMPT, build_user_prompt
from app.pipeline.state import IngestionState, QAState
from app.services.chunking import StatementChunker
from app.services.embeddings import EmbeddingService
from app.services.llm import LLMService
from app.services.metrics import Stopwatch
from app.services.pdf_parser import parse_statement_text
from app.services.retrieval import Retriever

logger = logging.getLogger(__name__)


def traced(name: str):
    """Mide tiempo, loguea y convierte excepciones en state['error'] (el grafo enruta a END)."""

    def decorator(fn: Callable):
        @functools.wraps(fn)
        def wrapper(self, state):
            if state.get("error"):
                return {}
            with Stopwatch() as sw:
                try:
                    update = fn(self, state) or {}
                    error = None
                except AppError as exc:
                    update, error = {}, str(exc)
                    log_event(logger, f"node.{name}.failed", logging.WARNING, error=error)
                except Exception as exc:  # error inesperado: se loguea con traceback
                    update, error = {}, f"Error inesperado: {exc}"
                    logger.exception("node.%s falló", name)
            timings = {**state.get("timings", {}), name: round(sw.ms, 1)}
            log_event(logger, f"node.{name}", ms=round(sw.ms, 1), ok=error is None)
            out = {**update, "timings": timings}
            if error:
                out["error"] = error
            return out

        return wrapper

    return decorator


class IngestionNodes:
    def __init__(self, extractor, repo, embeddings: EmbeddingService, chunker: StatementChunker, max_mb: int = 20):
        self.extractor = extractor
        self.repo = repo
        self.embeddings = embeddings
        self.chunker = chunker
        self.max_bytes = max_mb * 1024 * 1024

    @traced("validate_file")
    def validate_file(self, state: IngestionState):
        path = Path(state["path"])
        if not path.is_file():
            raise PdfExtractionError(f"No existe el archivo: {path}")
        if path.suffix.lower() != ".pdf":
            raise PdfExtractionError("El archivo debe ser un .pdf")
        data = path.read_bytes()
        if len(data) > self.max_bytes:
            raise PdfExtractionError("El PDF es demasiado grande")
        if not data.startswith(b"%PDF-"):
            raise PdfExtractionError("El archivo no parece un PDF válido")
        return {"filename": path.name, "file_hash": hashlib.sha256(data).hexdigest()}

    @traced("extract_text")
    def extract_text(self, state: IngestionState):
        return {"text": self.extractor.extract(state["path"])}

    @traced("parse_statement")
    def parse_statement(self, state: IngestionState):
        parsed = parse_statement_text(state["text"])
        for w in parsed.warnings:
            log_event(logger, "parse.warning", logging.WARNING, warning=w)
        return {"parsed": parsed}

    @traced("check_duplicate")
    def check_duplicate(self, state: IngestionState):
        existing = self.repo.find_by_hash(state["file_hash"])
        return {"duplicate": True, "statement_id": existing} if existing else {"duplicate": False}

    @traced("persist_statement")
    def persist_statement(self, state: IngestionState):
        sid = self.repo.save_statement(state["parsed"], state["file_hash"], state["filename"])
        return {"statement_id": sid}

    @traced("build_chunks")
    def build_chunks(self, state: IngestionState):
        return {"chunks": self.chunker.chunk(state["parsed"])}

    @traced("embed_and_store")
    def embed_and_store(self, state: IngestionState):
        chunks = state["chunks"]
        vectors = self.embeddings.embed_documents([c.content for c in chunks])
        self.repo.add_chunks(state["statement_id"], chunks, vectors, self.embeddings.model)
        return {"n_chunks": len(chunks)}


class QANodes:
    def __init__(self, retriever: Retriever, llm: LLMService, max_history_turns: int = 6):
        self.retriever = retriever
        self.llm = llm
        self.max_history = max_history_turns * 2

    @traced("retrieve")
    def retrieve(self, state: QAState):
        return {"chunks": self.retriever.retrieve(state["question"], state.get("statement_id"))}

    @traced("build_prompt")
    def build_prompt(self, state: QAState):
        history = state.get("history", [])[-self.max_history :]
        user = build_user_prompt(state["question"], state.get("chunks", []))
        return {"system": SYSTEM_PROMPT, "messages": [*history, {"role": "user", "content": user}]}

    @traced("generate")
    def generate(self, state: QAState):
        resp = self.llm.generate(state["system"], state["messages"])
        return {"llm": resp, "answer": resp.text}
