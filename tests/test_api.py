"""La API completa con dos cuentas: aislamiento entre usuarios, cuotas, sesiones y defensa CSRF.
Sin PostgreSQL ni APIs externas (dobles de tests/fakes.py)."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.container import AppServices
from app.core.config import Settings
from app.pipeline.graph import build_ingestion_graph, build_qa_graph
from app.pipeline.nodes import IngestionNodes, QANodes
from app.services.assistant import StatementAssistant, UsageLimits
from app.services.auth import AuthService, LoginThrottle
from app.services.categorizer import TransactionCategorizer
from app.services.chunking import StatementChunker
from app.services.embeddings import EmbeddingService, FakeEmbeddingProvider
from app.services.llm import LLMService
from app.services.metrics import MetricsService
from app.services.retrieval import Retriever
from app.services.finance import FinanceService
from tests.fakes import FakeExtractor, FakeLLMProvider, InMemoryFinanceRepo, InMemoryRepo, InMemoryUserRepo, ListSink

PDF = ("estado.pdf", b"%PDF-1.7 fake", "application/pdf")
PASSWORD = "una-clave-larga"
INVITE = "pase-2026"


@pytest.fixture
def services():
    repo, sink = InMemoryRepo(), ListSink()
    metrics = MetricsService(sink, SimpleNamespace(price_input_per_mtok=0, price_output_per_mtok=0))
    embeddings = EmbeddingService(FakeEmbeddingProvider(64), metrics)
    llm = LLMService(FakeLLMProvider(), metrics)
    assistant = StatementAssistant(
        build_ingestion_graph(IngestionNodes(FakeExtractor(), repo, embeddings, StatementChunker(10),
                                             categorizer=TransactionCategorizer(repo))),
        build_qa_graph(QANodes(Retriever(repo, embeddings, metrics, top_k=3), llm)),
        repo, sink, metrics,
        limits=UsageLimits(max_asks_per_day=2, max_uploads_per_day=5, max_statements_per_user=3),
    )  # fmt: skip
    auth = AuthService(InMemoryUserRepo(), LoginThrottle(3, 900), invite_code=INVITE, max_users=10)
    return AppServices(assistant=assistant, auth=auth, finance=FinanceService(InMemoryFinanceRepo(), repo))


@pytest.fixture
def app(services, tmp_path):
    settings = Settings(_env_file=None, app_env="test", upload_dir=tmp_path / "uploads", log_dir=tmp_path / "logs")
    return create_app(settings, services_factory=lambda _: services)


def _client(app, username, role=None, services=None):
    c = TestClient(app)
    c.__enter__()
    r = c.post("/auth/register", json={"username": username, "password": PASSWORD, "invite_code": INVITE})
    assert r.status_code == 201, r.text
    if role:
        services.auth.set_role(username, role)
    return c


@pytest.fixture
def ana(app):
    c = _client(app, "ana")
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def beto(app):
    c = _client(app, "beto")
    yield c
    c.__exit__(None, None, None)


def _upload(client):
    r = client.post("/upload-pdf/", files={"file": PDF})
    assert r.status_code == 200, r.text
    return r.json()["statement_id"]


# ------------------------------------------------------------------ acceso
def test_public_and_protected_routes(app):
    with TestClient(app) as anon:
        assert anon.get("/health").status_code == 200
        assert anon.get("/").status_code == 200
        assert anon.get("/auth/config").json() == {"invite_required": True}
        for path in ("/statements", "/statements/1", "/metrics", "/auth/me"):
            assert anon.get(path).status_code == 401, path
        assert anon.post("/ask", json={"question": "hola"}).status_code == 401
        assert anon.post("/upload-pdf/", files={"file": PDF}).status_code == 401


def test_register_sets_httponly_cookie(app):
    with TestClient(app) as c:
        bad = c.post("/auth/register", json={"username": "ana", "password": PASSWORD, "invite_code": "x"})
        assert bad.status_code == 400 and "invitación" in bad.json()["detail"]
        r = c.post("/auth/register", json={"username": "Ana", "password": PASSWORD, "invite_code": INVITE})
        assert r.status_code == 201 and r.json() == {"id": 1, "username": "ana", "role": "user"}
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie
        assert "password" not in r.text and "hash" not in r.text
        assert c.get("/auth/me").json()["username"] == "ana"


# ------------------------------------------------------------------ aislamiento
def test_users_cannot_see_or_touch_each_others_statements(ana, beto):
    sid = _upload(ana)
    assert [s["id"] for s in ana.get("/statements").json()] == [sid]
    assert beto.get("/statements").json() == []
    assert beto.get(f"/statements/{sid}").status_code == 404
    assert beto.get(f"/statements/{sid}/overview").status_code == 404
    assert beto.delete(f"/statements/{sid}").status_code == 404
    assert beto.post("/ask", json={"question": "¿Cuánto debo?", "statement_id": sid}).status_code == 404
    r = beto.post("/ask", json={"question": "¿Cuánto debo?"})  # sin statement_id: no cae en los datos de Ana
    assert r.status_code == 422 and "Primero carga" in r.json()["detail"]
    assert ana.get(f"/statements/{sid}").status_code == 200  # sigue intacto


def test_same_pdf_is_independent_per_user(ana, beto):
    a, b = _upload(ana), _upload(beto)
    assert a != b
    assert beto.delete(f"/statements/{b}").status_code == 204
    assert ana.get(f"/statements/{a}").status_code == 200


def test_ask_uses_only_own_data(ana, beto):
    sid = _upload(ana)
    _upload(beto)
    r = ana.post("/ask", json={"question": "¿Cuánto debo pagar?", "statement_id": sid})
    assert r.status_code == 200
    assert {s["statement_id"] for s in r.json()["sources"]} == {sid}


def test_metrics_are_per_user_and_global_only_for_admin(app, services, ana):
    _upload(ana)
    assert ana.get("/metrics?scope=all").status_code == 403
    assert {m["operation"] for m in ana.get("/metrics").json()} >= {"ingestion"}
    with TestClient(app) as other:
        other.post("/auth/register", json={"username": "beto", "password": PASSWORD, "invite_code": INVITE})
        assert other.get("/metrics").json() == []
        assert other.get("/metrics/recent").json() == []
        services.auth.set_role("beto", "admin")
        assert other.get("/metrics?scope=all").status_code == 200
        assert len(other.get("/metrics/recent?scope=all").json()) > 0


# ------------------------------------------------------------------ cuotas
def test_daily_ask_quota(ana):
    sid = _upload(ana)
    for _ in range(2):
        assert ana.post("/ask", json={"question": "hola", "statement_id": sid}).status_code == 200
    r = ana.post("/ask", json={"question": "hola", "statement_id": sid})
    assert r.status_code == 429 and "límite" in r.json()["detail"]


def test_statement_cap_per_user(ana):
    for i in range(3):  # bytes distintos → hash distinto → no son duplicados
        r = ana.post("/upload-pdf/", files={"file": (f"e{i}.pdf", b"%PDF-1.7 " + bytes([i]), "application/pdf")})
        assert r.status_code == 200
    r = ana.post("/upload-pdf/", files={"file": ("e9.pdf", b"%PDF-1.7 9", "application/pdf")})
    assert r.status_code == 429 and "máximo" in r.json()["detail"]


# ------------------------------------------------------------------ sesiones y seguridad
def test_logout_invalidates_session(ana):
    assert ana.post("/auth/logout").status_code == 204
    assert ana.get("/auth/me").status_code == 401


def test_login_failures_are_rate_limited(app, ana):
    with TestClient(app) as c:
        for _ in range(3):
            assert c.post("/auth/login", json={"username": "ana", "password": "mala-clave-1"}).status_code == 401
        assert c.post("/auth/login", json={"username": "ana", "password": PASSWORD}).status_code == 429


def test_cross_origin_writes_are_rejected(ana):
    evil = {"Origin": "https://sitio-malicioso.example"}
    assert ana.post("/auth/logout", headers=evil).status_code == 403
    assert ana.get("/auth/me").status_code == 200
    same = {"Origin": "http://testserver"}
    assert ana.post("/auth/logout", headers=same).status_code == 204


def test_change_password_keeps_current_session_only(app, ana):
    with TestClient(app) as other_device:
        assert other_device.post("/auth/login", json={"username": "ana", "password": PASSWORD}).status_code == 200
        r = ana.post("/auth/password", json={"current_password": "x" * 10, "new_password": "otra-clave-larga"})
        assert r.status_code == 400
        r = ana.post("/auth/password", json={"current_password": PASSWORD, "new_password": "otra-clave-larga"})
        assert r.status_code == 204
        assert ana.get("/auth/me").status_code == 200
        assert other_device.get("/auth/me").status_code == 401


# ------------------------------------------------------------------ salud crediticia y categorías
def test_credit_health_is_per_user(app, ana, beto):
    sid = _upload(ana)
    with TestClient(app) as anon:
        assert anon.get("/credit-health").status_code == 401
    r = ana.get("/credit-health").json()
    assert r["statement_id"] == sid and r["score"] == 53 and r["level"] == "En riesgo"
    assert {i["key"] for i in r["indicators"]} >= {"utilization", "payment_behavior", "debt_trend"}
    assert beto.get(f"/credit-health?statement_id={sid}").status_code == 404
    empty = beto.get("/credit-health").json()
    assert empty["statement_id"] is None and empty["score"] is None


def test_category_correction_applies_to_merchant_and_is_isolated(ana, beto):
    sid = _upload(ana)
    _upload(beto)
    txs = ana.get(f"/statements/{sid}").json()["transactions"]
    sears = [t for t in txs if t["description"].startswith("Sears Metepec")]
    assert len(sears) == 2 and all(t["category"] == "hogar" for t in sears)

    assert beto.patch(f"/transactions/{sears[0]['id']}/category", json={"category": "salud"}).status_code == 404
    assert ana.patch(f"/transactions/{sears[0]['id']}/category", json={"category": "casino"}).status_code == 422
    r = ana.patch(f"/transactions/{sears[0]['id']}/category", json={"category": "ropa_cuidado"})
    assert r.status_code == 200 and r.json()["updated"] == 2   # ambos cargos de Sears Metepec

    after = ana.get(f"/statements/{sid}").json()["transactions"]
    assert {t["category_source"] for t in after if t["description"].startswith("Sears Metepec")} == {"user"}
    beto_sid = beto.get("/statements").json()[0]["id"]
    beto_sears = [t for t in beto.get(f"/statements/{beto_sid}").json()["transactions"] if t["description"] == "Sears Metepec"]
    assert all(t["category"] == "hogar" for t in beto_sears)    # la corrección de Ana no afecta a Beto


def test_categories_catalog(ana):
    cats = ana.get("/categories").json()
    keys = [c["key"] for c in cats]
    assert "salud" in keys and "sin_clasificar" not in keys
    assert next(c for c in cats if c["key"] == "educacion")["deductible"] is True


# ------------------------------------------------------------------ finanzas del mes
def test_finance_requires_session(app):
    with TestClient(app) as anon:
        assert anon.get("/finance/month").status_code == 401
        assert anon.post("/cash-expenses", json={"date": "2026-10-01", "amount": "10"}).status_code == 401


def test_manual_finance_flow_and_isolation(ana, beto):
    r = ana.post("/incomes", json={"date": "2026-10-01", "amount": "20000", "kind": "salario", "recurring": True})
    assert r.status_code == 201
    inc_id = r.json()["id"]
    e = ana.post("/cash-expenses", json={"date": "2026-10-03", "amount": "350.50", "description": "Farmacia Similares"}).json()
    assert e["category"] == "salud" and e["category_source"] == "rule"

    m = ana.get("/finance/month?month=2026-10").json()
    assert m["income_total"] == "20000.00" and m["cash_total"] == "350.50"
    assert m["savings_rate"] == pytest.approx((20000 - 350.5) / 20000 * 100)
    assert {x["source"] for x in m["movements"]} == {"ingreso", "efectivo"}
    assert ana.get("/finance/month?month=2026-11").json()["income_total"] == "20000.00"   # recurrente

    assert beto.get("/finance/month?month=2026-10").json()["movements"] == []
    assert beto.delete(f"/cash-expenses/{e['id']}").status_code == 404
    assert beto.patch(f"/incomes/{inc_id}", json={"amount": "1"}).status_code == 404

    assert ana.patch(f"/cash-expenses/{e['id']}", json={"category": "hogar"}).json()["category"] == "hogar"
    assert ana.patch(f"/incomes/{inc_id}", json={"end_date": "2026-10-31"}).status_code == 200
    assert ana.get("/finance/month?month=2026-11").json()["income_total"] == "0"
    assert ana.delete(f"/cash-expenses/{e['id']}").status_code == 204


def test_manual_finance_validation(ana):
    assert ana.post("/cash-expenses", json={"date": "2026-10-01", "amount": "-5"}).status_code == 422
    assert ana.post("/cash-expenses", json={"date": "2026-10-01", "amount": "5", "category": "casino"}).status_code == 422
    assert ana.post("/incomes", json={"date": "2026-10-01", "amount": "5", "kind": "loteria"}).status_code == 422
    assert ana.get("/finance/month?month=2026-13").status_code == 422


def test_month_includes_nu_purchases(ana):
    _upload(ana)  # estado de cuenta del 10/08 al 09/09/2026
    m = ana.get("/finance/month?month=2026-08").json()
    assert float(m["nu_total"]) > 0 and float(m["card_payments"]) == pytest.approx(11553.87)
    assert any(x["source"] == "nu" for x in m["movements"])


def test_frontend_is_always_revalidated(app):
    with TestClient(app) as c:
        for path in ("/", "/static/js/helpers.js", "/static/css/styles.css"):
            r = c.get(path)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path
        etag = c.get("/static/js/helpers.js").headers["etag"]
        assert c.get("/static/js/helpers.js", headers={"If-None-Match": etag}).status_code == 304
        assert "cache-control" not in c.get("/health").headers


def test_assets_are_versioned_by_content(app, monkeypatch):
    import app.api as api_module

    with TestClient(app) as c:
        html = c.get("/").text
        versions = set(__import__("re").findall(r'/static/[^"]+\.(?:js|css)\?v=([0-9a-f]{10})"', html))
        assert len(versions) == 1                          # todos los archivos con la misma huella
        assert '/static/js/helpers.js"' not in html          # ninguno sin versión
        monkeypatch.setattr(api_module, "assets_version", lambda: "0123456789")
        assert "/static/js/finance.js?v=0123456789" in c.get("/").text   # cambia cuando cambian los archivos


def test_assets_version_is_cached_until_a_file_changes(monkeypatch, tmp_path):
    import app.api as api_module

    (tmp_path / "a.js").write_text("uno", encoding="utf-8")
    monkeypatch.setattr(api_module, "FRONTEND_DIR", tmp_path)
    api_module._assets_cache.clear()
    reads = []
    original = type(tmp_path).read_bytes
    monkeypatch.setattr(type(tmp_path), "read_bytes", lambda self: reads.append(self.name) or original(self))
    v1 = api_module.assets_version()
    assert api_module.assets_version() == v1 and reads == ["a.js"]          # segunda vez: sin volver a leer
    (tmp_path / "a.js").write_text("dos, más largo", encoding="utf-8")
    assert api_module.assets_version() != v1                                 # cambió el archivo → nueva huella
