from datetime import date
from decimal import Decimal

import pytest

from app.core.exceptions import QuotaExceededError
from app.schemas import CashExpenseItem, IncomeItem, StatementDetail
from app.services.categorizer import TransactionCategorizer
from app.services.finance import (
    CashExpenseIn,
    FinanceService,
    IncomeIn,
    build_month,
    month_bounds,
    nu_gaps,
    occurrence,
    summary_to_text,
)
from app.services.pdf_parser import parse_statement_text
from tests.fakes import FIXTURE, InMemoryFinanceRepo, InMemoryRepo

D = Decimal
TODAY = date(2026, 10, 14)


@pytest.fixture(scope="module")
def september() -> StatementDetail:
    """Estado de cuenta del 10/08 al 09/09/2026, con compras categorizadas."""
    p = parse_statement_text(FIXTURE.read_text(encoding="utf-8"))
    p = p.model_copy(update={"transactions": TransactionCategorizer(InMemoryRepo()).categorize(1, p.transactions)})
    return StatementDetail(id=1, filename="sep.pdf", **p.model_dump())


def income(amount="25000", when=date(2026, 7, 15), recurring=True, end=None, iid=1):
    return IncomeItem(id=iid, date=when, amount=D(amount), kind="salario", description="Sueldo",
                      recurring=recurring, end_date=end)  # fmt: skip


def cash(amount, when, category="supermercado", cid=10):
    return CashExpenseItem(id=cid, date=when, amount=D(amount), description="x", category=category)


def ind(summary, key):
    return next(i for i in summary.indicators if i.key == key)


# ------------------------------------------------------------------ fechas
def test_month_bounds_and_validation():
    assert month_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    with pytest.raises(ValueError):
        month_bounds("2026-13")


def test_recurring_income_occurrences():
    salary = income(when=date(2026, 1, 31))
    assert occurrence(salary, date(2026, 2, 1), date(2026, 2, 28)) == date(2026, 2, 28)  # día 31 → fin de febrero
    assert occurrence(salary, date(2025, 12, 1), date(2025, 12, 31)) is None             # antes de empezar
    ended = income(when=date(2026, 1, 15), end=date(2026, 3, 31))
    assert occurrence(ended, date(2026, 3, 1), date(2026, 3, 31)) == date(2026, 3, 15)
    assert occurrence(ended, date(2026, 4, 1), date(2026, 4, 30)) is None
    once = income(recurring=False, when=date(2026, 5, 2))
    assert occurrence(once, date(2026, 6, 1), date(2026, 6, 30)) is None


def test_nu_gaps(september):
    assert nu_gaps([september], date(2026, 8, 1), date(2026, 8, 31)) == [(date(2026, 8, 1), date(2026, 8, 9))]
    assert nu_gaps([], date(2026, 10, 1), date(2026, 10, 14)) == [(date(2026, 10, 1), date(2026, 10, 14))]


# ------------------------------------------------------------------ resumen mensual
def test_month_combines_cash_and_nu_without_double_counting(september):
    s = build_month("2026-08", [income()], [cash("850", date(2026, 8, 3)), cash("300", date(2026, 8, 20), "transporte", 11)],
                    [september], TODAY)  # fmt: skip
    purchases_aug = sum(t.amount for t in september.transactions
                        if t.kind.value == "purchase" and date(2026, 8, 1) <= t.operation_date <= date(2026, 8, 31))  # fmt: skip
    assert s.income_total == D("25000") and s.cash_total == D("1150")
    assert s.nu_total == purchases_aug
    assert s.card_payments == D("11553.87")                        # pago a la tarjeta: informativo, no gasto
    assert s.expense_total == s.cash_total + s.nu_total + s.finance_cost
    assert s.balance == s.income_total - s.expense_total
    sources = {m.source for m in s.movements}
    assert sources == {"ingreso", "efectivo", "nu"}
    assert not any("Plan de pagos fijos" in m.description for m in s.movements)  # mensualidades no se suman
    assert any("01/08/2026 al 09/08/2026" in n for n in s.notes)  # días del mes aún sin estado de cuenta
    by = {c.category: c for c in s.categories}
    assert by["supermercado"].cash == D("850") and by["supermercado"].nu == 0


def test_indicators_and_score(september):
    s = build_month("2026-08", [income()], [cash("850", date(2026, 8, 3))], [september], TODAY)
    assert ind(s, "savings_rate").status == "good"
    assert ind(s, "debt_to_income").status == "bad"                 # 17,510.97 / 25,000 = 70%
    assert ind(s, "credit").value == 53
    assert s.score is not None and 0 <= s.score <= 100


def test_without_income_flow_indicators_are_na():
    s = build_month("2026-10", [], [cash("120", date(2026, 10, 2))], [], TODAY)
    assert s.savings_rate is None and s.score is None
    assert {i.status for i in s.indicators} == {"na"}
    assert s.recommendations == ["Registra tu ingreso de este mes para ver cuánto te sobra y tu tasa de ahorro."]


def test_overspending_recommendation():
    s = build_month("2026-10", [income("10000", date(2026, 10, 1), recurring=False)],
                    [cash("12000", date(2026, 10, 5), "entretenimiento")], [], TODAY)  # fmt: skip
    assert s.balance == D("-2000") and ind(s, "savings_rate").status == "bad"
    assert any("gastaste $2,000.00 más" in r for r in s.recommendations)
    assert "FINANZAS DEL MES OCTUBRE 2026" in summary_to_text(s)


# ------------------------------------------------------------------ servicio
@pytest.fixture
def service():
    return FinanceService(InMemoryFinanceRepo(), InMemoryRepo(), max_entries_per_day=3, today=lambda: TODAY)


def test_service_suggests_category_and_validates(service):
    e = service.add_cash_expense(1, CashExpenseIn(date=TODAY, amount=D("85.50"), description="Farmacia del Ahorro"))
    assert (e.category, e.category_source) == ("salud", "rule")
    e2 = service.add_cash_expense(1, CashExpenseIn(date=TODAY, amount=D("40"), description="algo raro"))
    assert e2.category == "sin_clasificar"
    with pytest.raises(ValueError):
        service.add_cash_expense(1, CashExpenseIn(date=TODAY, amount=D("1"), category="casino"))
    service.add_cash_expense(1, CashExpenseIn(date=TODAY, amount=D("1")))      # 3.º válido (el inválido no cuenta)
    with pytest.raises(QuotaExceededError):
        service.add_cash_expense(1, CashExpenseIn(date=TODAY, amount=D("1")))   # 4.º registro del día (límite 3)


def test_service_isolates_users(service):
    from app.services.finance import IncomeUpdate

    inc = service.add_income(1, IncomeIn(date=date(2026, 10, 1), amount=D("20000"), recurring=True))
    assert service.update_income(2, inc.id, IncomeUpdate(amount=D("1"))) is None
    assert service.delete_income(2, inc.id) is False
    assert service.month(2, "2026-10").income_total == 0
    assert service.month(1, "2026-11").income_total == D("20000")          # recurrente
    service.update_income(1, inc.id, IncomeUpdate(end_date=date(2026, 10, 31)))
    assert service.month(1, "2026-11").income_total == 0


def test_invalid_dates_and_amounts(service):
    with pytest.raises(ValueError):
        service.add_income(1, IncomeIn(date=date(1999, 1, 1), amount=D("1")))
    with pytest.raises(ValueError):
        service.add_income(1, IncomeIn(date=TODAY, amount=D("1"), recurring=True, end_date=date(2026, 1, 1)))
    with pytest.raises(Exception):
        CashExpenseIn(date=TODAY, amount=D("-5"))
