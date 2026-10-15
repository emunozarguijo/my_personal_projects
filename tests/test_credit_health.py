from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.schemas import StatementDetail, Transaction, TransactionKind
from app.services.credit_health import build_report, report_to_text, score_of
from app.services.pdf_parser import parse_statement_text
from tests.fakes import FIXTURE

D = Decimal


@pytest.fixture(scope="module")
def september() -> StatementDetail:
    p = parse_statement_text(FIXTURE.read_text(encoding="utf-8"))
    return StatementDetail(id=1, filename="sep.pdf", **p.model_dump())


def next_month(prev: StatementDetail, *, paid: Decimal, paid_on: date, balance: Decimal, available: Decimal,
               interest="0", sid=2, gap_days=1) -> StatementDetail:  # fmt: skip
    start = prev.period_end + timedelta(days=gap_days)
    end = start + timedelta(days=29)
    payment = Transaction(operation_date=paid_on, charge_date=paid_on, description="¡Gracias por tu pago!",
                          amount=-paid, kind=TransactionKind.PAYMENT)  # fmt: skip
    return prev.model_copy(update={
        "id": sid, "period_start": start, "period_end": end, "cut_date": end, "due_date": end + timedelta(days=12),
        "payments": paid, "pay_no_interest": balance, "available_credit": available, "previous_balance": prev.pay_no_interest,
        "interest": D(interest), "iva": D(0), "commissions": D(0), "installments": [], "transactions": [payment],
    })  # fmt: skip


def ind(report, key):
    return next(i for i in report.indicators if i.key == key)


def test_single_month(september):
    r = build_report([september])
    assert r.statement_id == 1 and r.months_available == 1
    assert ind(r, "utilization").status == "bad" and ind(r, "utilization").value == pytest.approx(70.0, abs=0.1)
    pb = ind(r, "payment_behavior")
    assert pb.status == "good" and "Estimado" in pb.detail          # pagó el adeudo anterior completo
    assert ind(r, "punctuality").status == "na" and ind(r, "debt_trend").status == "na"
    assert r.score == 53 and r.level == "En riesgo"
    assert any("17,510.97" in x and "21/09/2026" in x for x in r.recommendations)
    assert "no es tu score de Buró" in r.notes[0]


def test_paid_in_full_on_time_and_debt_down(september):
    oct_ = next_month(september, paid=D("17510.97"), paid_on=date(2026, 9, 20), balance=D("4000"), available=D("21000"))
    r = build_report([september, oct_])
    assert ind(r, "payment_behavior").status == "good" and "Estimado" not in ind(r, "payment_behavior").detail
    assert ind(r, "punctuality").status == "good"
    assert ind(r, "debt_trend").status == "good"
    assert ind(r, "utilization").status == "good"       # 4,000 / 25,000 = 16%
    assert ind(r, "finance_cost").status == "good"
    assert r.score == 100 and r.level == "Saludable"
    assert [p.statement_id for p in r.series] == [1, 2]


def test_minimum_only_late_and_growing_debt(september):
    oct_ = next_month(september, paid=D("396.06"), paid_on=date(2026, 9, 25), balance=D("24000"),
                      available=D("1000"), interest="600")  # fmt: skip
    r = build_report([september, oct_])
    assert ind(r, "payment_behavior").display == "Solo el mínimo"
    assert ind(r, "punctuality").status == "bad"         # pagó el 25/09, la fecha límite era el 21/09
    assert ind(r, "debt_trend").status == "bad" and ind(r, "finance_cost").status == "bad"
    assert r.score is not None and r.score < 40 and r.level == "Crítica"
    assert any("30%" in x for x in r.recommendations)


def test_non_consecutive_months_skip_trend(september):
    later = next_month(september, paid=D("100"), paid_on=date(2026, 12, 1), balance=D("100"),
                       available=D("24900"), gap_days=60)  # fmt: skip
    r = build_report([september, later])
    assert ind(r, "debt_trend").status == "na"
    assert any("no es consecutivo" in n for n in r.notes)


def test_selects_requested_statement(september):
    oct_ = next_month(september, paid=D("17510.97"), paid_on=date(2026, 9, 20), balance=D("4000"), available=D("21000"))
    assert build_report([september, oct_], statement_id=1).period_end == september.period_end


def test_empty_and_insufficient_data(september):
    assert build_report([]).level == "Sin datos suficientes"
    bare = september.model_copy(update={"credit_limit": None, "previous_balance": None, "installments": []})
    r = build_report([bare])
    assert r.score is None  # sin uso del crédito ni comportamiento de pago no hay índice, solo semáforos


def test_score_weights():
    from app.schemas import HealthIndicator as HI
    mk = lambda k, s, w: HI(key=k, label=k, status=s, display="", detail="", weight=w)  # noqa: E731
    assert score_of([mk("a", "good", 30), mk("b", "bad", 30), mk("c", "na", 15)]) == 50
    assert score_of([mk("a", "warn", 30), mk("x", "bad", 0)]) is None  # < 40 de peso evaluable


def test_text_for_chat(september):
    text = report_to_text(build_report([september]))
    assert "Índice: 53/100" in text and "Uso del crédito: 70%" in text and "Buró" in text
