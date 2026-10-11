"""Logging estructurado (JSON a archivo, legible en consola) con request_id por operación."""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from app.core.config import Settings

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar("user_id", default=None)
_configured = False


def current_request_id() -> str:
    return _request_id.get()


def current_user_id() -> int | None:
    return _user_id.get()


@contextmanager
def request_context(request_id: str | None = None, user_id: int | None = None):
    """Asocia request_id y usuario a todos los logs/métricas generados dentro del bloque (por hilo)."""
    rid_token = _request_id.set(request_id or uuid.uuid4().hex[:12])
    uid_token = _user_id.set(user_id if user_id is not None else _user_id.get())
    try:
        yield _request_id.get()
    finally:
        _user_id.reset(uid_token)
        _request_id.reset(rid_token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": _request_id.get(),
            "user_id": _user_id.get(),
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if ctx:
            payload.update(ctx)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} [{_request_id.get()}] {record.name}: {record.getMessage()}"
        ctx = getattr(record, "ctx", None)
        if ctx:
            base += " " + " ".join(f"{k}={v}" for k, v in ctx.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(settings: Settings) -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_ConsoleFormatter())
    root.addHandler(console)

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        settings.log_dir / "app.jsonl", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    # Librerías ruidosas
    for noisy in ("httpx", "httpcore", "urllib3", "pdfminer", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    """logger.info con campos estructurados: log_event(log, "llm_call", latency_ms=12)."""
    logger.log(level, event, extra={"ctx": fields})
