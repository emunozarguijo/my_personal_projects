"""API web con FastAPI (alternativa a la GUI de Tkinter). Reutiliza la misma fachada StatementAssistant.

  uvicorn app.api:app --reload      →  http://127.0.0.1:8000   (docs: /docs, fuera de prod)

Multiusuario: cada persona se registra con usuario y contraseña; la sesión viaja en una cookie HttpOnly y todos
los endpoints de datos operan solo sobre los estados de cuenta de la cuenta en sesión.

Los endpoints son `def` (no `async def`) a propósito: el pipeline es bloqueante (PDF, BD, LLM) y
FastAPI los ejecuta en un pool de hilos sin congelar el servidor.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.container import AppServices, build_services
from app.core.config import Settings, get_settings
from app.core.exceptions import AuthError, QuotaExceededError, RateLimitedError
from app.schemas import (
    ApiCallRecord,
    AskResult,
    CreditHealthReport,
    CashExpenseItem,
    IncomeItem,
    IngestResult,
    MonthlySummary,
    OperationStats,
    StatementDetail,
    StatementSummary,
    UserAccount,
    UserPublic,
)
from app.services.assistant import NOT_FOUND, StatementAssistant
from app.services.categorizer import CATEGORIES, DEDUCTIBLE, ESSENTIAL, UNCLASSIFIED
from app.services.finance import CashExpenseIn, CashExpenseUpdate, FinanceService, IncomeIn, IncomeUpdate

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
SESSION_COOKIE = "session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

router = APIRouter()


# ------------------------------------------------------------------ dependencias
def get_services(request: Request) -> AppServices:
    return request.app.state.services


def get_assistant(services: AppServices = Depends(get_services)) -> StatementAssistant:
    return services.assistant


def get_finance(services: AppServices = Depends(get_services)) -> FinanceService:
    return services.finance


def _set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=settings.session_ttl_hours * 3600, httponly=True,
        secure=settings.secure_cookies, samesite="lax", path="/",
    )  # fmt: skip


def current_user(
    request: Request, response: Response, services: AppServices = Depends(get_services)
) -> UserAccount:
    token = request.cookies.get(SESSION_COOKIE)
    found = services.auth.authenticate(token)
    if not found:
        raise HTTPException(status_code=401, detail="Inicia sesión para continuar.")
    user, renewed = found
    if renewed:  # expiración deslizante: el navegador recibe la cookie con el nuevo plazo
        _set_session_cookie(response, request.app.state.settings, token)
    return user


def admin_user(user: UserAccount = Depends(current_user)) -> UserAccount:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Solo para administradores.")
    return user


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "-"


# ------------------------------------------------------------------ cuentas
class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(Credentials):
    invite_code: str | None = Field(default=None, max_length=200)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


@router.get("/auth/config")
def auth_config(services: AppServices = Depends(get_services)) -> dict:
    return {"invite_required": services.auth.invite_required}


@router.post("/auth/register", response_model=UserPublic, status_code=201)
def register(body: RegisterRequest, request: Request, response: Response,
             services: AppServices = Depends(get_services)):  # fmt: skip
    user, token = services.auth.register(
        body.username, body.password, body.invite_code, ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )  # fmt: skip
    _set_session_cookie(response, request.app.state.settings, token)
    return user.public()


@router.post("/auth/login", response_model=UserPublic)
def login(body: Credentials, request: Request, response: Response, services: AppServices = Depends(get_services)):
    try:
        user, token = services.auth.login(
            body.username, body.password, ip=_client_ip(request), user_agent=request.headers.get("user-agent")
        )
    except RateLimitedError:
        raise
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    _set_session_cookie(response, request.app.state.settings, token)
    return user.public()


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, services: AppServices = Depends(get_services)) -> None:
    services.auth.logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/auth/me", response_model=UserPublic)
def me(user: UserAccount = Depends(current_user)):
    return user.public()


@router.post("/auth/password", status_code=204)
def change_password(body: ChangePasswordRequest, request: Request, user: UserAccount = Depends(current_user),
                    services: AppServices = Depends(get_services)) -> None:  # fmt: skip
    services.auth.change_password(user, body.current_password, body.new_password, request.cookies.get(SESSION_COOKIE))


# ------------------------------------------------------------------ páginas
_ASSET_RE = re.compile(r'(/static/[^"?]+\.(?:js|css))"')


_assets_cache: dict[tuple, str] = {}


def assets_version() -> str:
    """Huella de los archivos del frontend: cambia en cuanto cambia cualquiera de ellos.

    Solo se vuelve a leer y hashear si cambió la fecha o el tamaño de algún archivo (consultar eso es casi gratis).
    """
    files = sorted(p for p in FRONTEND_DIR.rglob("*") if p.suffix in (".js", ".css"))
    signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if signature not in _assets_cache:
        digest = hashlib.sha256()
        for path in files:
            digest.update(path.read_bytes())
        _assets_cache.clear()
        _assets_cache[signature] = digest.hexdigest()[:10]
    return _assets_cache[signature]


@router.get("/", include_in_schema=False)
def home() -> HTMLResponse:
    # Cada versión de los .js/.css tiene su propia URL (?v=huella): el navegador nunca mezcla un archivo viejo
    # guardado en caché con uno nuevo, aunque los haya guardado antes de que existiera Cache-Control.
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(_ASSET_RE.sub(rf'\1?v={assets_version()}"', html))


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ------------------------------------------------------------------ estados de cuenta
def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", Path(name).name)[:120] or "estado.pdf"


@router.post("/upload-pdf/", response_model=IngestResult)
def upload_pdf(request: Request, file: UploadFile = File(...), user: UserAccount = Depends(current_user),
               assistant: StatementAssistant = Depends(get_assistant)):  # fmt: skip
    settings: Settings = request.app.state.settings
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="El archivo debe ser un formato .pdf válido.")
    assistant.ensure_quota(user.id, "upload")

    folder = settings.upload_dir / str(user.id) / uuid4().hex  # carpeta por usuario y por carga
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
        result = assistant.ingest_pdf(user.id, str(dest))
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    if not result.ok or result.duplicate:
        shutil.rmtree(folder, ignore_errors=True)  # no conservamos archivos fallidos o repetidos
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error)
    return result


@router.get("/statements", response_model=list[StatementSummary])
def list_statements(user: UserAccount = Depends(current_user), assistant: StatementAssistant = Depends(get_assistant)):
    return assistant.list_statements(user.id)


@router.get("/statements/{statement_id}", response_model=StatementDetail)
def statement_detail(statement_id: int, user: UserAccount = Depends(current_user),
                     assistant: StatementAssistant = Depends(get_assistant)):  # fmt: skip
    detail = assistant.statement_detail(user.id, statement_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return detail


@router.get("/statements/{statement_id}/overview")
def overview(statement_id: int, user: UserAccount = Depends(current_user),
             assistant: StatementAssistant = Depends(get_assistant)) -> dict:  # fmt: skip
    if not assistant.owns(user.id, statement_id):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return {"statement_id": statement_id, "overview": assistant.overview(user.id, statement_id)}


@router.delete("/statements/{statement_id}", status_code=204)
def delete_statement(statement_id: int, user: UserAccount = Depends(current_user),
                     assistant: StatementAssistant = Depends(get_assistant)) -> None:  # fmt: skip
    if not assistant.delete_statement(user.id, statement_id):
        raise HTTPException(status_code=404, detail=NOT_FOUND)


# ------------------------------------------------------------------ salud crediticia y categorías
@router.get("/credit-health", response_model=CreditHealthReport)
def credit_health(statement_id: int | None = None, user: UserAccount = Depends(current_user),
                  assistant: StatementAssistant = Depends(get_assistant)):  # fmt: skip
    if statement_id is not None and not assistant.owns(user.id, statement_id):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return assistant.credit_health(user.id, statement_id)


@router.get("/categories")
def categories() -> list[dict]:
    return [
        {"key": k, "label": v, "essential": k in ESSENTIAL, "deductible": k in DEDUCTIBLE}
        for k, v in CATEGORIES.items() if k != UNCLASSIFIED
    ]  # fmt: skip


class CategoryUpdate(BaseModel):
    category: str = Field(min_length=1, max_length=30)


@router.patch("/transactions/{transaction_id}/category")
def set_category(transaction_id: int, body: CategoryUpdate, user: UserAccount = Depends(current_user),
                 assistant: StatementAssistant = Depends(get_assistant)) -> dict:  # fmt: skip
    try:
        changed = assistant.set_category(user.id, transaction_id, body.category)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=404, detail="Ese movimiento no existe.")
    return {"updated": changed, "category": body.category}


# ------------------------------------------------------------------ finanzas del mes (registro manual)
def _bad_input(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/finance/month", response_model=MonthlySummary)
def finance_month(month: str | None = None, user: UserAccount = Depends(current_user),
                  finance: FinanceService = Depends(get_finance)):  # fmt: skip
    try:
        return finance.month(user.id, month)
    except ValueError as exc:
        raise _bad_input(exc) from exc


@router.post("/incomes", response_model=IncomeItem, status_code=201)
def add_income(body: IncomeIn, user: UserAccount = Depends(current_user), finance: FinanceService = Depends(get_finance)):
    try:
        return finance.add_income(user.id, body)
    except ValueError as exc:
        raise _bad_input(exc) from exc


@router.patch("/incomes/{income_id}", response_model=IncomeItem)
def update_income(income_id: int, body: IncomeUpdate, user: UserAccount = Depends(current_user),
                  finance: FinanceService = Depends(get_finance)):  # fmt: skip
    try:
        item = finance.update_income(user.id, income_id, body)
    except ValueError as exc:
        raise _bad_input(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Ese ingreso no existe.")
    return item


@router.delete("/incomes/{income_id}", status_code=204)
def delete_income(income_id: int, user: UserAccount = Depends(current_user),
                  finance: FinanceService = Depends(get_finance)) -> None:  # fmt: skip
    if not finance.delete_income(user.id, income_id):
        raise HTTPException(status_code=404, detail="Ese ingreso no existe.")


@router.post("/cash-expenses", response_model=CashExpenseItem, status_code=201)
def add_cash_expense(body: CashExpenseIn, user: UserAccount = Depends(current_user),
                     finance: FinanceService = Depends(get_finance)):  # fmt: skip
    try:
        return finance.add_cash_expense(user.id, body)
    except ValueError as exc:
        raise _bad_input(exc) from exc


@router.patch("/cash-expenses/{expense_id}", response_model=CashExpenseItem)
def update_cash_expense(expense_id: int, body: CashExpenseUpdate, user: UserAccount = Depends(current_user),
                        finance: FinanceService = Depends(get_finance)):  # fmt: skip
    try:
        item = finance.update_cash_expense(user.id, expense_id, body)
    except ValueError as exc:
        raise _bad_input(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Ese gasto no existe.")
    return item


@router.delete("/cash-expenses/{expense_id}", status_code=204)
def delete_cash_expense(expense_id: int, user: UserAccount = Depends(current_user),
                        finance: FinanceService = Depends(get_finance)) -> None:  # fmt: skip
    if not finance.delete_cash_expense(user.id, expense_id):
        raise HTTPException(status_code=404, detail="Ese gasto no existe.")


# ------------------------------------------------------------------ preguntas y métricas
class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    statement_id: int | None = None
    history: list[dict[str, str]] = Field(default_factory=list, max_length=40)


@router.post("/ask", response_model=AskResult)
def ask(body: AskRequest, user: UserAccount = Depends(current_user),
        assistant: StatementAssistant = Depends(get_assistant)):  # fmt: skip
    if body.statement_id is not None and not assistant.owns(user.id, body.statement_id):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    assistant.ensure_quota(user.id, "ask")
    result = assistant.ask(user.id, body.question, body.statement_id, body.history)
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error)
    return result


def _metrics_scope(scope: Literal["me", "all"], user: UserAccount) -> int | None:
    if scope == "all":
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Solo para administradores.")
        return None
    return user.id


@router.get("/metrics", response_model=list[OperationStats])
def metrics(scope: Literal["me", "all"] = "me", user: UserAccount = Depends(current_user),
            assistant: StatementAssistant = Depends(get_assistant)):  # fmt: skip
    return assistant.metrics_summary(_metrics_scope(scope, user))


@router.get("/metrics/recent", response_model=list[ApiCallRecord])
def metrics_recent(limit: int = 50, scope: Literal["me", "all"] = "me", user: UserAccount = Depends(current_user),
                   assistant: StatementAssistant = Depends(get_assistant)):  # fmt: skip
    return assistant.recent_calls(_metrics_scope(scope, user), min(max(limit, 1), 200))


# ------------------------------------------------------------------ aplicación
def create_app(
    settings: Settings | None = None,
    services_factory: Callable[[Settings], AppServices] = build_services,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        app.state.settings = settings
        app.state.services = services_factory(settings)  # se crean una sola vez al arrancar
        yield
        if getattr(app.state.services, "close", None):
            app.state.services.close()  # p. ej. escribe las métricas que quedaron en cola

    docs = settings.docs_enabled
    app = FastAPI(
        title="Asistente de estado de cuenta",
        description="Sube tus estados de cuenta en PDF y hazles preguntas (RAG + LangGraph)",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        # Defensa CSRF junto con SameSite=Lax: un sitio ajeno no puede enviar POST/DELETE con la cookie de sesión.
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Origen no permitido."}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def revalidate_frontend(request: Request, call_next):
        # Sin esto el navegador puede reutilizar un helpers.js viejo junto con un finance.js nuevo tras un deploy.
        # no-cache = revalidar siempre (ETag → 304 si no cambió), no "no guardar".
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(QuotaExceededError)
    @app.exception_handler(RateLimitedError)
    async def too_many(_: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=429)

    @app.exception_handler(AuthError)
    async def auth_error(_: Request, exc: AuthError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    app.include_router(router)
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
    return app


app = create_app()
