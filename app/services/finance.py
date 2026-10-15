"""Finanzas del mes calendario: ingresos y efectivo (registro manual) + tarjeta Nu (estados de cuenta).

Reglas de conteo (para no contar dos veces):
- Gastos con Nu = compras del estado de cuenta, ubicadas por su fecha de operación.
- Intereses, comisiones e IVA de Nu = costo financiero del mes (sí es gasto: dinero perdido).
- Pagos a la tarjeta NO son gasto (el gasto ya se contó al comprar); se muestran como dato.
- Mensualidades a meses NO se suman de nuevo (la compra se contó cuando se hizo).

Todo se calcula en código; el chat solo recibe el resultado (`summary_to_text`).
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from pydantic import BaseModel, Field

from app.core.exceptions import QuotaExceededError
from app.schemas import (
    CashExpenseItem,
    HealthIndicator,
    IncomeItem,
    IncomeKind,
    MonthCategory,
    MonthlySummary,
    MonthMovement,
    StatementDetail,
    TransactionKind,
)
from app.services.categorizer import CATEGORIES, DEDUCTIBLE, ESSENTIAL, UNCLASSIFIED, normalize_merchant, rule_category
from app.services.chunking import dmy, money
from app.services.credit_health import POINTS, build_report, level_of

Z = Decimal(0)
MAX_AMOUNT = Decimal("10000000")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MONTHS_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
             "noviembre", "diciembre"]  # fmt: skip
CREDIT_WEIGHT = 40
MIN_WEIGHT_FOR_SCORE = 40
MONTH_DISCLAIMER = "Índice orientativo con tus registros y estados de cuenta; no es tu score de Buró de Crédito."


# ---------------------------------------------------------------------- entradas (validación)
class IncomeIn(BaseModel):
    date: date
    amount: Decimal = Field(gt=0, le=MAX_AMOUNT, decimal_places=2)
    kind: IncomeKind = "salario"
    description: str = Field(default="", max_length=120)
    recurring: bool = False
    end_date: date | None = None


class IncomeUpdate(BaseModel):
    # dt.date: un campo llamado `date` con valor por defecto taparía el tipo `date` en las anotaciones siguientes.
    date: dt.date | None = None
    amount: Decimal | None = Field(default=None, gt=0, le=MAX_AMOUNT, decimal_places=2)
    kind: IncomeKind | None = None
    description: str | None = Field(default=None, max_length=120)
    recurring: bool | None = None
    end_date: dt.date | None = None


class CashExpenseIn(BaseModel):
    date: date
    amount: Decimal = Field(gt=0, le=MAX_AMOUNT, decimal_places=2)
    description: str = Field(default="", max_length=120)
    category: str | None = None  # None → se sugiere con las reglas de comercios


class CashExpenseUpdate(BaseModel):
    date: dt.date | None = None
    amount: Decimal | None = Field(default=None, gt=0, le=MAX_AMOUNT, decimal_places=2)
    description: str | None = Field(default=None, max_length=120)
    category: str | None = None


# ---------------------------------------------------------------------- utilidades
def month_bounds(month: str) -> tuple[date, date]:
    if not MONTH_RE.fullmatch(month or ""):
        raise ValueError("El mes debe tener el formato AAAA-MM.")
    y, m = int(month[:4]), int(month[5:])
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def month_label(month: str) -> str:
    return f"{MONTHS_ES[int(month[5:]) - 1]} {month[:4]}"


def occurrence(income: IncomeItem, start: date, end: date) -> date | None:
    """Fecha en que el ingreso cae dentro del mes [start, end], o None."""
    if not income.recurring:
        return income.date if start <= income.date <= end else None
    if income.date > end or (income.end_date and income.end_date < start):
        return None
    when = max(income.date, date(start.year, start.month, min(income.date.day, end.day)))
    if when > end or (income.end_date and when > income.end_date):
        return None
    return when


def nu_gaps(statements: list[StatementDetail], start: date, end: date) -> list[tuple[date, date]]:
    """Tramos de [start, end] que ningún estado de cuenta cargado cubre (ahí faltan cargos Nu)."""
    gaps, cursor = [], start
    for st in sorted(statements, key=lambda s: s.period_start):
        if st.period_end < cursor or st.period_start > end:
            continue
        if st.period_start > cursor:
            gaps.append((cursor, st.period_start - timedelta(days=1)))
        cursor = max(cursor, st.period_end + timedelta(days=1))
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


def _pct(part: Decimal, whole: Decimal) -> float:
    return float(part / whole * 100) if whole else 0.0


def _ind(key, label, status, weight, display="N/D", detail="", value=None) -> HealthIndicator:
    return HealthIndicator(key=key, label=label, status=status, weight=weight, display=display, detail=detail, value=value)


# ---------------------------------------------------------------------- resumen mensual
def build_month(month: str, incomes: list[IncomeItem], cash: list[CashExpenseItem],
                statements: list[StatementDetail], today: date) -> MonthlySummary:  # fmt: skip
    start, end = month_bounds(month)
    movements: list[MonthMovement] = []

    income_total = Z
    for inc in incomes:
        when = occurrence(inc, start, end)
        if when:
            income_total += inc.amount
            movements.append(MonthMovement(source="ingreso", id=inc.id, date=when, amount=inc.amount,
                                           description=inc.description or inc.kind.capitalize(), recurring=inc.recurring))  # fmt: skip

    by_cat: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"cash": Z, "nu": Z})
    cash_total = Z
    for e in cash:
        if start <= e.date <= end:
            cash_total += e.amount
            by_cat[e.category]["cash"] += e.amount
            movements.append(MonthMovement(source="efectivo", id=e.id, date=e.date, amount=e.amount,
                                           description=e.description or CATEGORIES.get(e.category, ""), category=e.category))  # fmt: skip

    nu_total = finance_cost = card_payments = Z
    for st in statements:
        for t in st.transactions:
            if not start <= t.operation_date <= end:
                continue
            if t.kind == TransactionKind.PURCHASE and t.amount > 0:
                cat = t.category if t.category in CATEGORIES else UNCLASSIFIED
                nu_total += t.amount
                by_cat[cat]["nu"] += t.amount
                movements.append(MonthMovement(source="nu", id=t.id, date=t.operation_date, amount=t.amount,
                                               description=t.description, category=cat))  # fmt: skip
            elif t.kind in (TransactionKind.INTEREST, TransactionKind.TAX, TransactionKind.FEE):
                finance_cost += t.amount
            elif t.kind == TransactionKind.PAYMENT:
                card_payments += -t.amount

    spent = cash_total + nu_total
    categories = sorted(
        (MonthCategory(category=c, label=CATEGORIES[c], total=v["cash"] + v["nu"], cash=v["cash"], nu=v["nu"],
                       share=_pct(v["cash"] + v["nu"], spent), essential=c in ESSENTIAL, deductible=c in DEDUCTIBLE)
         for c, v in by_cat.items()),
        key=lambda c: c.total, reverse=True,
    )  # fmt: skip
    expense_total = spent + finance_cost
    balance = income_total - expense_total
    savings = float(balance / income_total * 100) if income_total > 0 else None
    movements.sort(key=lambda m: (m.date, m.source), reverse=True)

    # Estado de cuenta de referencia para el componente crediticio: el más reciente que empieza antes de fin de mes.
    ref = max((s for s in statements if s.period_start <= end), key=lambda s: s.period_end, default=None)
    credit = build_report(statements, ref.id) if ref else None
    indicators = _indicators(income_total, expense_total, categories, finance_cost, savings, ref, credit)

    rated = [i for i in indicators if i.weight and i.status != "na"]
    total_w = sum(i.weight for i in rated)
    score = None
    if total_w >= MIN_WEIGHT_FOR_SCORE:
        pts = sum(i.weight * ((i.value or 0) / 100 if i.key == "credit" else POINTS[i.status]) for i in rated)
        score = round(100 * pts / total_w)

    notes = [MONTH_DISCLAIMER]
    horizon = min(end, today)
    gaps = nu_gaps(statements, start, horizon) if horizon >= start else []
    covered = max((min(s.period_end, end) for s in statements if s.period_start <= end and s.period_end >= start), default=None)
    if gaps:
        spans = ", ".join(dmy(a) if a == b else f"{dmy(a)} al {dmy(b)}" for a, b in gaps)
        notes.append(f"Aún no hay estado de cuenta que cubra del {spans}: tus compras con Nu de esos días no aparecen todavía.")
    notes.append("Las mensualidades a meses no se vuelven a sumar: la compra se contó cuando la hiciste.")

    summary = MonthlySummary(
        month=month, income_total=income_total, cash_total=cash_total, nu_total=nu_total,
        finance_cost=finance_cost, expense_total=expense_total, balance=balance, savings_rate=savings,
        card_payments=card_payments, incomes=[i for i in incomes if occurrence(i, start, end)],
        categories=categories, movements=movements, score=score, level=level_of(score), indicators=indicators,
        nu_covered_until=covered, notes=notes,
    )
    summary.recommendations = _recommendations(summary)
    return summary


def _indicators(income, expenses, categories, finance_cost, savings, ref, credit) -> list[HealthIndicator]:
    out = []
    if income <= 0:
        msg = "Registra tu ingreso del mes para evaluar este indicador."
        out += [_ind("savings_rate", "Tasa de ahorro", "na", 25, detail=msg),
                _ind("essential_share", "Gasto esencial", "na", 15, detail=msg),
                _ind("debt_to_income", "Saldo de tarjeta vs. ingreso", "na", 15, detail=msg),
                _ind("cost_to_income", "Costo del crédito vs. ingreso", "na", 5, detail=msg)]  # fmt: skip
    else:
        status = "good" if savings >= 20 else "warn" if savings >= 10 else "bad"
        verb = "Te sobró" if savings >= 0 else "Te faltaron"
        out.append(_ind("savings_rate", "Tasa de ahorro", status, 25, f"{savings:.0f}%",
                        f"{verb} {money(abs(income - expenses))} de {money(income)}. Lo recomendable es ahorrar al menos 20%.",
                        round(savings, 1)))  # fmt: skip
        essential = sum((c.total for c in categories if c.essential), Z)
        e = _pct(essential, income)
        status = "good" if e <= 50 else "warn" if e <= 70 else "bad"
        out.append(_ind("essential_share", "Gasto esencial", status, 15, f"{e:.0f}%",
                        f"Salud, educación, hogar, súper, transporte y servicios: {money(essential)} ({e:.0f}% de tu ingreso).", round(e, 1)))  # fmt: skip
        if ref is not None and ref.pay_no_interest is not None:
            d = _pct(ref.pay_no_interest, income)
            status = "good" if d < 30 else "warn" if d <= 60 else "bad"
            out.append(_ind("debt_to_income", "Saldo de tarjeta vs. ingreso", status, 15, f"{d:.0f}%",
                            f"Tu saldo Nu al {dmy(ref.period_end)} es {money(ref.pay_no_interest)}: {d:.0f}% de tu ingreso mensual. "
                            "Aunque lo pagues completo, arriba de 30% deja poco margen.", round(d, 1)))  # fmt: skip
        else:
            out.append(_ind("debt_to_income", "Saldo de tarjeta vs. ingreso", "na", 15,
                            detail="Carga un estado de cuenta para comparar tu saldo con tu ingreso."))  # fmt: skip
        c = _pct(finance_cost, income)
        status = "good" if finance_cost <= 0 else "warn" if c < 5 else "bad"
        out.append(_ind("cost_to_income", "Costo del crédito vs. ingreso", status, 5, f"{c:.1f}%",
                        f"Intereses, comisiones e IVA de la tarjeta este mes: {money(finance_cost)}.", round(c, 2)))  # fmt: skip
    if credit is not None and credit.score is not None:
        status = "good" if credit.score >= 80 else "warn" if credit.score >= 60 else "bad"
        out.append(_ind("credit", "Salud crediticia (Nu)", status, CREDIT_WEIGHT, f"{credit.score}/100",
                        f"Índice de tu tarjeta al {dmy(credit.period_end)}: {credit.level}.", credit.score))  # fmt: skip
    else:
        out.append(_ind("credit", "Salud crediticia (Nu)", "na", CREDIT_WEIGHT,
                        detail="Carga tus estados de cuenta para incluir tu tarjeta en la evaluación."))  # fmt: skip
    return out


def _recommendations(s: MonthlySummary) -> list[str]:
    recs: list[str] = []
    if s.income_total <= 0:
        return ["Registra tu ingreso de este mes para ver cuánto te sobra y tu tasa de ahorro."]
    if s.balance < 0:
        recs.append(f"Este mes gastaste {money(-s.balance)} más de lo que ganaste: revisa tus categorías discrecionales.")
    elif s.savings_rate is not None and s.savings_rate < 20:
        target = s.income_total * Decimal("0.80")
        recs.append(f"Para ahorrar 20% de tu ingreso necesitarías gastar {money(s.expense_total - target)} menos al mes.")
    discretionary = [c for c in s.categories if not c.essential and c.category != UNCLASSIFIED]
    if discretionary:
        top = discretionary[0]
        recs.append(f"Tu mayor gasto discrecional fue {top.label}: {money(top.total)} ({top.share:.0f}% de lo que gastaste).")
    if s.finance_cost > 0:
        recs.append(f"Pagaste {money(s.finance_cost)} en intereses y comisiones de la tarjeta: pagar el total evita ese costo.")
    deductible = sum((c.total for c in s.categories if c.deductible), Z)
    if deductible > 0:
        recs.append(f"Tienes {money(deductible)} en salud o educación que podrían ser deducibles: pide tus facturas (CFDI).")
    unclassified = next((c for c in s.categories if c.category == UNCLASSIFIED), None)
    if unclassified and unclassified.share >= 20:
        recs.append(f"Hay {money(unclassified.total)} sin clasificar: asígnales categoría para un análisis más preciso.")
    return recs


def summary_to_text(s: MonthlySummary) -> str:
    """Resumen del mes para el contexto del chat (números ya calculados)."""
    lines = [
        f"FINANZAS DEL MES {month_label(s.month).upper()} (calculado automáticamente; mes calendario)",
        f"- Ingresos: {money(s.income_total)} · gastos: {money(s.expense_total)} "
        f"(efectivo {money(s.cash_total)}, tarjeta Nu {money(s.nu_total)}, intereses y comisiones {money(s.finance_cost)})",
        f"- Balance: {money(s.balance)}" + (f" · tasa de ahorro: {s.savings_rate:.0f}%" if s.savings_rate is not None else ""),
        f"- Pagos a la tarjeta (no son gasto): {money(s.card_payments)}",
        f"- Índice de salud financiera: {s.score if s.score is not None else 'N/D'}/100 · {s.level}. {MONTH_DISCLAIMER}",
    ]
    status = {"good": "bien", "warn": "atención", "bad": "riesgo", "na": "sin datos"}
    lines += [f"    · {i.label}: {i.display} [{status[i.status]}] — {i.detail}" for i in s.indicators]
    if s.categories:
        lines.append("- Gasto por categoría (efectivo + Nu):")
        lines += [f"    · {c.label}: {money(c.total)} (efectivo {money(c.cash)}, Nu {money(c.nu)})" for c in s.categories]
    lines += [f"- Nota: {n}" for n in s.notes[1:]]
    if s.recommendations:
        lines.append("- Recomendaciones calculadas:")
        lines += [f"    · {r}" for r in s.recommendations]
    return "\n".join(lines)


# ---------------------------------------------------------------------- servicio (lo usan la API y el chat)
class FinanceService:
    def __init__(self, repo, statements, max_entries_per_day: int = 200,
                 today: Callable[[], date] = date.today):  # fmt: skip
        self.repo = repo
        self.statements = statements
        self.max_entries_per_day = max_entries_per_day
        self._today = today

    def current_month(self) -> str:
        return self._today().strftime("%Y-%m")

    def month(self, user_id: int, month: str | None = None) -> MonthlySummary:
        month = month or self.current_month()
        start, end = month_bounds(month)
        return build_month(
            month, self.repo.incomes_for_range(user_id, start, end), self.repo.cash_for_range(user_id, start, end),
            self.statements.list_statement_details(user_id), self._today(),
        )  # fmt: skip

    # ------------------------------------------------------------- validación común
    def _check_quota(self, user_id: int) -> None:
        if not self.max_entries_per_day:
            return
        since = datetime.now(timezone.utc) - timedelta(days=1)
        if self.repo.count_created_since(user_id, since) >= self.max_entries_per_day:
            raise QuotaExceededError(f"Llegaste al límite de {self.max_entries_per_day} registros por día.")

    def _check_date(self, when: date | None) -> None:
        if when is not None and not date(2000, 1, 1) <= when <= self._today() + timedelta(days=366):
            raise ValueError("La fecha no es válida.")

    @staticmethod
    def _cents(fields: dict) -> dict:
        if fields.get("amount") is not None:
            fields["amount"] = Decimal(fields["amount"]).quantize(Decimal("0.01"))
        return fields

    @staticmethod
    def _category(category: str | None, description: str) -> tuple[str, str]:
        if category:
            if category not in CATEGORIES or category == UNCLASSIFIED:
                raise ValueError(f"Categoría inválida: {category!r}")
            return category, "user"
        suggested = rule_category(normalize_merchant(description))
        return (suggested, "rule") if suggested else (UNCLASSIFIED, "none")

    # ------------------------------------------------------------- ingresos
    def add_income(self, user_id: int, data: IncomeIn) -> IncomeItem:
        self._check_quota(user_id)
        self._check_date(data.date)
        if data.end_date and data.end_date < data.date:
            raise ValueError("La fecha de fin no puede ser anterior al inicio.")
        return self.repo.add_income(user_id, **self._cents(data.model_dump()))

    def update_income(self, user_id: int, income_id: int, data: IncomeUpdate) -> IncomeItem | None:
        fields = self._cents(data.model_dump(exclude_unset=True))
        self._check_date(fields.get("date"))
        return self.repo.update_income(user_id, income_id, **fields)

    def delete_income(self, user_id: int, income_id: int) -> bool:
        return self.repo.delete_income(user_id, income_id)

    # ------------------------------------------------------------- gastos en efectivo
    def add_cash_expense(self, user_id: int, data: CashExpenseIn) -> CashExpenseItem:
        self._check_quota(user_id)
        self._check_date(data.date)
        category, source = self._category(data.category, data.description)
        return self.repo.add_cash_expense(user_id, date=data.date, amount=data.amount.quantize(Decimal("0.01")),
                                          description=data.description, category=category, category_source=source)  # fmt: skip

    def update_cash_expense(self, user_id: int, expense_id: int, data: CashExpenseUpdate) -> CashExpenseItem | None:
        fields = self._cents(data.model_dump(exclude_unset=True))
        self._check_date(fields.get("date"))
        if "category" in fields:
            fields["category"], fields["category_source"] = self._category(fields["category"], fields.get("description", ""))
        return self.repo.update_cash_expense(user_id, expense_id, **fields)

    def delete_cash_expense(self, user_id: int, expense_id: int) -> bool:
        return self.repo.delete_cash_expense(user_id, expense_id)
