"""API web con FastAPI (alternativa a la GUI de Tkinter). Reutiliza la misma fachada StatementAssistant.

  uvicorn app.api:app --reload      →  http://127.0.0.1:8000   (docs: /docs)

Los endpoints son `def` (no `async def`) a propósito: el pipeline es bloqueante (PDF, BD, LLM) y
FastAPI los ejecuta en un pool de hilos sin congelar el servidor.
"""
from __future__ import annotations

import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.container import build_assistant
from app.core.config import get_settings
from app.schemas import (
    ApiCallRecord,
    AskResult,
    IngestResult,
    OperationStats,
    StatementDetail,
    StatementSummary,
)
from app.services.assistant import StatementAssistant


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = get_settings()
    app.state.settings.upload_dir.mkdir(parents=True, exist_ok=True)
    app.state.assistant = build_assistant(app.state.settings)  # se crea una sola vez al arrancar
    yield


app = FastAPI(
    title="Asistente de estado de cuenta",
    description="Sube un estado de cuenta en PDF y hazle preguntas (RAG + LangGraph)",
    lifespan=lifespan,
)


def get_assistant(request: Request) -> StatementAssistant:
    return request.app.state.assistant


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    statement_id: int | None = None
    history: list[dict[str, str]] = Field(default_factory=list)


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", Path(name).name)[:120] or "estado.pdf"


# ------------------------------------------------------------------ páginas
WEB_DIR = Path(__file__).parent / "ui" / "web"


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ------------------------------------------------------------------ estados de cuenta
@app.post("/upload-pdf/", response_model=IngestResult)
def upload_pdf(file: UploadFile = File(...), assistant: StatementAssistant = Depends(get_assistant)):
    settings = get_settings()
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un formato .pdf válido.")

    folder = settings.upload_dir / uuid4().hex  # carpeta única: evita pisar archivos con el mismo nombre
    folder.mkdir(parents=True)
    dest = folder / _safe_name(file.filename)
    limit, size = settings.max_pdf_mb * 1024 * 1024, 0
    try:
        with dest.open("wb") as out:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail=f"El PDF excede {settings.max_pdf_mb} MB.")
                out.write(chunk)
        result = assistant.ingest_pdf(str(dest))
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    if not result.ok or result.duplicate:
        shutil.rmtree(folder, ignore_errors=True)  # no conservamos archivos fallidos o repetidos
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error)
    return result


@app.get("/statements", response_model=list[StatementSummary])
def list_statements(assistant: StatementAssistant = Depends(get_assistant)):
    return assistant.list_statements()


@app.get("/statements/{statement_id}", response_model=StatementDetail)
def statement_detail(statement_id: int, assistant: StatementAssistant = Depends(get_assistant)):
    detail = assistant.statement_detail(statement_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Ese estado de cuenta ya no existe.")
    return detail


@app.get("/statements/{statement_id}/overview")
def overview(statement_id: int, assistant: StatementAssistant = Depends(get_assistant)) -> dict:
    return {"statement_id": statement_id, "overview": assistant.overview(statement_id)}


@app.delete("/statements/{statement_id}", status_code=204)
def delete_statement(statement_id: int, assistant: StatementAssistant = Depends(get_assistant)) -> None:
    assistant.delete_statement(statement_id)


# ------------------------------------------------------------------ preguntas y métricas
@app.post("/ask", response_model=AskResult)
def ask(body: AskRequest, assistant: StatementAssistant = Depends(get_assistant)):
    result = assistant.ask(body.question, body.statement_id, body.history)
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error)
    return result


@app.get("/metrics", response_model=list[OperationStats])
def metrics(assistant: StatementAssistant = Depends(get_assistant)):
    return assistant.metrics_summary()


@app.get("/metrics/recent", response_model=list[ApiCallRecord])
def metrics_recent(limit: int = 50, assistant: StatementAssistant = Depends(get_assistant)):
    return assistant.recent_calls(min(max(limit, 1), 200))
