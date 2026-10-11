"""Dobles de prueba: permiten ejecutar los grafos, la fachada y la API sin PostgreSQL ni APIs externas.

Replican las reglas de aislamiento del repositorio real: todo filtra por user_id.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.core.exceptions import AuthError
from app.schemas import (
    ApiCallRecord,
    Chunk,
    OperationStats,
    ParsedStatement,
    RetrievedChunk,
    StatementDetail,
    StatementSummary,
    TokenUsage,
    UserAccount,
)
from app.services.llm import LLMProvider

FIXTURE = Path(__file__).parent / "fixtures" / "nu_statement_sample.txt"


class FakeExtractor:
    def extract(self, path) -> str:
        return FIXTURE.read_text(encoding="utf-8")


class InMemoryRepo:
    """Misma interfaz que StatementRepository."""

    def __init__(self):
        self.statements: dict[int, SimpleNamespace] = {}
        self.chunks: list[tuple[int, int, Chunk, list[float]]] = []  # (user_id, statement_id, chunk, vector)
        self._next_id = 1

    def _own(self, user_id, sid):
        st = self.statements.get(sid)
        return st if st and st.user_id == user_id else None

    def find_by_hash(self, user_id, file_hash):
        return next((sid for sid, st in self.statements.items() if st.user_id == user_id and st.hash == file_hash), None)

    def save_statement(self, user_id, parsed: ParsedStatement, file_hash, filename):
        sid, self._next_id = self._next_id, self._next_id + 1
        self.statements[sid] = SimpleNamespace(user_id=user_id, parsed=parsed, hash=file_hash, filename=filename)
        return sid

    def count_statements(self, user_id):
        return sum(st.user_id == user_id for st in self.statements.values())

    def owns(self, user_id, sid):
        return self._own(user_id, sid) is not None

    def list_statements(self, user_id):
        return [
            StatementSummary(id=sid, filename=st.filename, **st.parsed.model_dump(
                include={"card_last4", "period_start", "period_end", "due_date", "pay_no_interest"}))
            for sid, st in self.statements.items() if st.user_id == user_id
        ]  # fmt: skip

    def get_statement_detail(self, user_id, sid):
        st = self._own(user_id, sid)
        return StatementDetail(id=sid, filename=st.filename, **st.parsed.model_dump()) if st else None

    def latest_statement_id(self, user_id):
        return max((sid for sid, st in self.statements.items() if st.user_id == user_id), default=None)

    def delete_statement(self, user_id, sid):
        if not self._own(user_id, sid):
            return False
        del self.statements[sid]
        self.chunks = [c for c in self.chunks if c[1] != sid]
        return True

    def add_chunks(self, user_id, statement_id, chunks, vectors, embedding_model):
        self.chunks += [(user_id, statement_id, c, v) for c, v in zip(chunks, vectors)]

    def get_chunks_by_kind(self, user_id, statement_id, kind):
        return [
            RetrievedChunk(statement_id=sid, kind=c.kind, position=c.position, content=c.content, score=1.0)
            for uid, sid, c, _ in self.chunks if uid == user_id and sid == statement_id and c.kind == kind
        ]  # fmt: skip

    def search_chunks(self, user_id, vector, top_k, statement_id=None):
        scored = [
            (sum(a * b for a, b in zip(vector, v)), sid, c)
            for uid, sid, c, v in self.chunks if uid == user_id and statement_id in (None, sid)
        ]  # fmt: skip
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            RetrievedChunk(statement_id=sid, kind=c.kind, position=c.position, content=c.content, score=s)
            for s, sid, c in scored[:top_k]
        ]  # fmt: skip


class ListSink:
    """Sink de MetricsService + las consultas de MetricsRepository."""

    def __init__(self):
        self.rows: list[dict] = []

    def add(self, **fields):
        self.rows.append({**fields, "created_at": datetime.now(timezone.utc)})

    def _rows(self, user_id):
        return [r for r in self.rows if user_id is None or r.get("user_id") == user_id]

    def count_since(self, user_id, operation, since):
        return sum(1 for r in self._rows(user_id) if r["operation"] == operation and r["created_at"] >= since)

    def summary(self, user_id):
        ops = sorted({r["operation"] for r in self._rows(user_id)})
        return [
            OperationStats(operation=op, calls=sum(r["operation"] == op for r in self._rows(user_id)), errors=0,
                           avg_latency_ms=0, p95_latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0)
            for op in ops
        ]  # fmt: skip

    def recent(self, user_id, limit=50):
        return [
            ApiCallRecord(created_at=str(r["created_at"]), operation=r["operation"], provider=r["provider"],
                          model=r["model"], status=r["status"], latency_ms=r["latency_ms"],
                          input_tokens=r["input_tokens"], output_tokens=r["output_tokens"], request_id=r["request_id"])
            for r in reversed(self._rows(user_id))
        ][:limit]  # fmt: skip


class InMemoryUserRepo:
    """Misma interfaz que UserRepository."""

    def __init__(self):
        self.users: dict[int, UserAccount] = {}
        self.sessions: dict[str, tuple[int, datetime, str | None]] = {}

    def get_by_username(self, username):
        return next((u for u in self.users.values() if u.username == username), None)

    def get_by_id(self, user_id):
        return self.users.get(user_id)

    def count_users(self):
        return len(self.users)

    def create(self, username, password_hash, role="user"):
        if self.get_by_username(username):
            raise AuthError("Ese nombre de usuario ya está en uso.")
        user = UserAccount(id=len(self.users) + 1, username=username, password_hash=password_hash, role=role)
        self.users[user.id] = user
        return user

    def set_password(self, user_id, password_hash):
        self.users[user_id] = self.users[user_id].model_copy(update={"password_hash": password_hash})

    def set_role(self, user_id, role):
        self.users[user_id] = self.users[user_id].model_copy(update={"role": role})

    def touch_login(self, user_id):
        pass

    def create_session(self, user_id, token_hash, expires_at, user_agent):
        self.sessions[token_hash] = (user_id, expires_at, user_agent)

    def get_session(self, token_hash):
        found = self.sessions.get(token_hash)
        return (self.users[found[0]], found[1]) if found else None

    def extend_session(self, token_hash, expires_at):
        uid, _, ua = self.sessions[token_hash]
        self.sessions[token_hash] = (uid, expires_at, ua)

    def delete_session(self, token_hash):
        self.sessions.pop(token_hash, None)

    def delete_user_sessions(self, user_id, except_token_hash=None):
        self.sessions = {h: s for h, s in self.sessions.items() if s[0] != user_id or h == except_token_hash}


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
