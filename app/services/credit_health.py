"""Salud financiera crediticia: indicadores, índice 0-100 y recomendaciones, calculados en código.

Todo sale de los estados de cuenta ya parseados de un usuario (ordenados del más antiguo al más reciente). El LLM
no calcula nada: solo recibe el resultado (ver `report_to_text`) para explicarlo en el chat.

El índice es una guía propia basada en los estados de cuenta, NO el score de Buró de Crédito.
"""
from __future__ import annotations

from decimal import Decimal

from app.schemas import (
    CreditHealthReport,
    HealthIndicator,
    MonthPoint,
    StatementDetail,
    TransactionKind,
)
from app.services.categorizer import UNCLASSIFIED, summarize_categories
from app.services.chunking import dmy, money

Z = Decimal(0)
POINTS = {"good": 1.0, "warn": 0.5, "bad": 0.0}
MIN_WEIGHT_FOR_SCORE = 40  # con menos información, no se calcula índice (solo semáforos)
DISCLAIMER = "Índice orientativo calculado con tus estados de cuenta; no es tu score de Buró de Crédito."


def _d(value: Decimal | None) -> Decimal:
    return value if value is not None else Z


def _pct(part: Decimal, whole: Decimal) -> float:
    return float(part / whole * 100) if whole else 0.0


def _payments(s: StatementDetail) -> Decimal:
    if s.payments is not None:
        return s.payments
    return -sum((t.amount for t in s.transactions if t.kind == TransactionKind.PAYMENT), Z)


def _purchases(s: StatementDetail) -> Decimal:
    if s.regular_charges is not None:
        return s.regular_charges
    return sum((t.amount for t in s.transactions if t.kind == TransactionKind.PURCHASE), Z)


def _finance_cost(s: StatementDetail) -> Decimal:
    return _d(s.interest) + _d(s.commissions) + _d(s.iva)


def _consecutive(prev: StatementDetail | None, cur: StatementDetail) -> bool:
    return prev is not None and 0 <= (cur.period_start - prev.period_end).days <= 3


# ---------------------------------------------------------------------- indicadores
def utilization(s: StatementDetail) -> HealthIndicator:
    label = "Uso del crédito"
    if not s.credit_limit or s.available_credit is None:
        return HealthIndicator(key="utilization", label=label, status="na", display="N/D", weight=30,
                               detail="El estado de cuenta no trae límite o crédito disponible.")  # fmt: skip
    used = s.credit_limit - s.available_credit
    u = _pct(used, s.credit_limit)
    status = "good" if u < 30 else "warn" if u <= 50 else "bad"
    return HealthIndicator(
        key="utilization", label=label, status=status, value=round(u, 1), display=f"{u:.0f}%", weight=30,
        detail=f"Usas {money(used)} de tu línea de {money(s.credit_limit)}. Lo recomendable es menos de 30%.",
    )


def payment_behavior(s: StatementDetail, prev: StatementDetail | None) -> HealthIndicator:
    label, paid = "Comportamiento de pago", _payments(s)
    if prev is not None:
        target, minimum, estimated = prev.pay_no_interest, prev.min_payment, False
    else:
        target, minimum, estimated = s.previous_balance, None, True
    if target is None:
        return HealthIndicator(key="payment_behavior", label=label, status="na", display="N/D", weight=30,
                               detail="No hay datos del saldo anterior para comparar tus pagos.")  # fmt: skip
    if target <= 0:
        return HealthIndicator(key="payment_behavior", label=label, status="good", value=100, display="Sin saldo",
                               weight=30, detail="No tenías saldo pendiente del periodo anterior.")  # fmt: skip
    ratio = float(paid / target * 100)
    if paid >= target - 1:
        status, display, detail = "good", "Pago total", f"Pagaste {money(paid)}, todo lo que debías: no generas intereses por compras."
    elif minimum is not None and paid > minimum + 1:
        status, display = "warn", "Más del mínimo"
        detail = f"Pagaste {money(paid)} de {money(target)}: más del mínimo, pero el resto genera intereses."
    elif minimum is not None and paid >= minimum - 1:
        status, display = "bad", "Solo el mínimo"
        detail = f"Pagaste solo el mínimo ({money(paid)}); el resto de {money(target)} genera intereses altos."
    elif paid > 0:
        status = "warn" if minimum is None else "bad"
        display = "Pago parcial" if minimum is None else "Menos del mínimo"
        detail = f"Pagaste {money(paid)} de {money(target)}."
    else:
        status, display, detail = "bad", "Sin pago", f"No se registraron pagos y debías {money(target)}."
    if estimated:
        detail += " (Estimado con el adeudo del periodo anterior; carga ese mes para más precisión.)"
    return HealthIndicator(key="payment_behavior", label=label, status=status, value=round(min(ratio, 100), 1),
                           display=display, detail=detail, weight=30)  # fmt: skip


def punctuality(s: StatementDetail, prev: StatementDetail | None) -> HealthIndicator:
    label = "Puntualidad"
    if prev is None or prev.due_date is None:
        return HealthIndicator(key="punctuality", label=label, status="na", display="N/D", weight=15,
                               detail="Carga el estado de cuenta anterior para revisar si pagaste a tiempo.")  # fmt: skip
    needed = _d(prev.min_payment)
    if needed <= 0:
        return HealthIndicator(key="punctuality", label=label, status="good", display="Sin pago requerido",
                               weight=15, detail="El periodo anterior no tenía pago mínimo.")  # fmt: skip
    pays = sorted((t for t in s.transactions if t.kind == TransactionKind.PAYMENT), key=lambda t: t.operation_date)
    on_time = -sum((t.amount for t in pays if t.operation_date <= prev.due_date), Z)
    if on_time >= needed - 1:
        return HealthIndicator(key="punctuality", label=label, status="good", display="A tiempo", weight=15,
                               detail=f"Cubriste al menos el mínimo antes de la fecha límite ({dmy(prev.due_date)}).")  # fmt: skip
    late = "Pagaste después de la fecha límite" if pays else "No hubo pago"
    return HealthIndicator(key="punctuality", label=label, status="bad", display="Tarde" if pays else "Sin pago",
                           weight=15, detail=f"{late} ({dmy(prev.due_date)}): puede generar comisión y afectar tu historial.")  # fmt: skip


def finance_cost(s: StatementDetail, prev: StatementDetail | None) -> HealthIndicator:
    label, cost = "Costo del crédito", _finance_cost(s)
    year = f" En los últimos 12 meses: {money(s.interest_last_12m)}." if s.interest_last_12m else ""
    if cost <= 0:
        status, detail = "good", "No pagaste intereses ni comisiones este periodo." + year
    elif prev is not None and cost > _finance_cost(prev) * Decimal("1.05"):
        status, detail = "bad", f"Intereses, comisiones e IVA subieron de {money(_finance_cost(prev))} a {money(cost)}." + year
    else:
        status, detail = "warn", f"Pagaste {money(cost)} en intereses, comisiones e IVA." + year
    return HealthIndicator(key="finance_cost", label=label, status=status, value=float(cost), display=money(cost),
                           detail=detail, weight=10)  # fmt: skip


def commitments(s: StatementDetail) -> HealthIndicator:
    label = "Compromisos fijos"
    if not s.installments:
        return HealthIndicator(key="commitments", label=label, status="good", value=0, display=money(Z), weight=5,
                               detail="No tienes compras a meses ni planes de pago.")  # fmt: skip
    monthly = sum((i.required_payment for i in s.installments), Z)
    left = max(i.total_installments - i.current_installment for i in s.installments)
    end = "terminan este mes" if left == 0 else f"la última termina en {left} mes(es)"
    pct = _pct(monthly, s.credit_limit) if s.credit_limit else None
    status = "warn" if pct is None else "good" if pct < 10 else "warn" if pct <= 25 else "bad"
    share = f" ({pct:.0f}% de tu línea)" if pct is not None else ""
    return HealthIndicator(
        key="commitments", label=label, status=status, value=round(pct, 1) if pct is not None else None,
        display=f"{money(monthly)}/mes", weight=5,
        detail=f"Pagas {money(monthly)} al mes por {len(s.installments)} compra(s) a meses{share}; {end}.",
    )


def debt_trend(s: StatementDetail, prev: StatementDetail | None) -> HealthIndicator:
    label = "Tendencia de la deuda"
    if prev is None or s.pay_no_interest is None or prev.pay_no_interest is None:
        return HealthIndicator(key="debt_trend", label=label, status="na", display="N/D", weight=10,
                               detail="Carga al menos dos meses consecutivos para ver si tu deuda sube o baja.")  # fmt: skip
    diff = s.pay_no_interest - prev.pay_no_interest
    change = _pct(diff, prev.pay_no_interest) if prev.pay_no_interest > 0 else (100.0 if diff > 0 else 0.0)
    if change <= -5:
        status, word = "good", "bajó"
    elif change < 5:
        status, word = "warn", "se mantuvo"
    else:
        status, word = "bad", "subió"
    detail = f"Tu saldo {word}: de {money(prev.pay_no_interest)} a {money(s.pay_no_interest)} ({change:+.0f}%)."
    return HealthIndicator(key="debt_trend", label=label, status=status, value=round(change, 1),
                           display=f"{change:+.0f}%", detail=detail, weight=10)  # fmt: skip


def spend_vs_pay(s: StatementDetail) -> HealthIndicator:
    spent, paid = _purchases(s), _payments(s)
    status = "good" if paid >= spent else "warn"
    return HealthIndicator(
        key="spend_vs_pay", label="Gasto contra pago", status=status, value=float(spent - paid),
        display=f"{money(spent)} / {money(paid)}", weight=0,
        detail=f"Compraste {money(spent)} y pagaste {money(paid)} en el periodo.",
    )


def indicators_for(s: StatementDetail, prev: StatementDetail | None) -> list[HealthIndicator]:
    return [utilization(s), payment_behavior(s, prev), punctuality(s, prev), finance_cost(s, prev),
            commitments(s), debt_trend(s, prev), spend_vs_pay(s)]  # fmt: skip


def score_of(indicators: list[HealthIndicator]) -> int | None:
    rated = [i for i in indicators if i.weight and i.status != "na"]
    total = sum(i.weight for i in rated)
    if total < MIN_WEIGHT_FOR_SCORE:
        return None
    return round(100 * sum(i.weight * POINTS[i.status] for i in rated) / total)


def level_of(score: int | None) -> str:
    if score is None:
        return "Sin datos suficientes"
    return "Saludable" if score >= 80 else "Aceptable" if score >= 60 else "En riesgo" if score >= 40 else "Crítica"


# ---------------------------------------------------------------------- recomendaciones
def recommendations(s: StatementDetail, ind: dict[str, HealthIndicator], categories, deductible_total: Decimal) -> list[str]:
    recs: list[str] = []
    if s.pay_no_interest and s.pay_no_interest > 0 and s.due_date:
        recs.append(f"Paga {money(s.pay_no_interest)} antes del {dmy(s.due_date)} para no generar intereses por tus compras.")
    u = ind["utilization"]
    if u.status in ("warn", "bad") and s.credit_limit and s.available_credit is not None:
        extra = (s.credit_limit - s.available_credit) - s.credit_limit * Decimal("0.30")
        recs.append(f"Si no puedes pagar el total, abona al menos {money(extra)} para dejar tu uso del crédito en 30%, un nivel más sano para tu historial.")
    if ind["payment_behavior"].status in ("warn", "bad"):
        minimum = next((sc for sc in s.payment_scenarios if sc.label.lower().strip() in ("el pago mínimo", "pago mínimo")), None)
        if minimum:
            recs.append(f"Si pagaras solo el mínimo ({money(minimum.monthly_payment)}), terminarías en {minimum.months_to_pay} "
                        f"y pagarías {money(minimum.total_interest)} de intereses.")
    if s.interest_last_12m and s.interest_last_12m > 0:
        recs.append(f"En los últimos 12 meses pagaste {money(s.interest_last_12m)} de intereses: dinero que no compró nada.")
    c = ind["commitments"]
    if c.status in ("warn", "bad"):
        recs.append(f"{c.detail} Evita nuevas compras a meses hasta liberar ese espacio.")
    if ind["debt_trend"].status == "bad":
        recs.append("Tu deuda creció respecto al mes anterior: revisa en qué categorías aumentó tu gasto.")
    if ind["spend_vs_pay"].status == "warn":
        recs.append(f"{ind['spend_vs_pay'].detail} Si se repite, tu deuda seguirá creciendo.")
    if deductible_total > 0:
        recs.append(f"Tienes {money(deductible_total)} en salud o educación que podrían ser deducibles: pide la factura (CFDI) a esos comercios.")
    unclassified = next((x for x in categories if x.category == UNCLASSIFIED), None)
    if unclassified and unclassified.share >= 20:
        recs.append(f"Hay {money(unclassified.amount)} en compras sin clasificar: corrígelas en Movimientos para un análisis más preciso.")
    return recs


# ---------------------------------------------------------------------- reporte
def build_report(statements: list[StatementDetail], statement_id: int | None = None) -> CreditHealthReport:
    """`statements` del usuario ordenados por period_end ascendente. Sin statement_id → el más reciente."""
    if not statements:
        return CreditHealthReport(statement_id=None, period_end=None, score=None, level="Sin datos suficientes",
                                  notes=["Carga tu primer estado de cuenta para ver tu salud crediticia."])  # fmt: skip
    idx = next((i for i, s in enumerate(statements) if s.id == statement_id), len(statements) - 1)
    series: list[MonthPoint] = []
    by_index: list[list[HealthIndicator]] = []
    for i, s in enumerate(statements):
        prev = statements[i - 1] if i and _consecutive(statements[i - 1], s) else None
        inds = indicators_for(s, prev)
        by_index.append(inds)
        u = inds[0].value
        series.append(MonthPoint(
            statement_id=s.id, period_end=s.period_end, utilization=u, balance=s.pay_no_interest,
            purchases=_purchases(s), payments=_payments(s), finance_cost=_finance_cost(s), score=score_of(inds),
        ))  # fmt: skip

    s, inds = statements[idx], by_index[idx]
    ind = {i.key: i for i in inds}
    categories = summarize_categories(s.transactions)
    total = sum((c.amount for c in categories), Z)
    essential = sum((c.amount for c in categories if c.essential), Z)
    deductible_total = sum((c.amount for c in categories if c.deductible), Z)
    score = score_of(inds)

    notes = [DISCLAIMER]
    has_prev = idx > 0 and _consecutive(statements[idx - 1], s)
    if idx == 0:
        notes.append("Carga el estado de cuenta del mes anterior para medir puntualidad y tendencia.")
    elif not has_prev:
        notes.append("El mes anterior cargado no es consecutivo: puntualidad y tendencia no se calculan.")

    return CreditHealthReport(
        statement_id=s.id, period_end=s.period_end, score=score, level=level_of(score), indicators=inds,
        recommendations=recommendations(s, ind, categories, deductible_total), categories=categories,
        essential_share=_pct(essential, total) if total else None, deductible_total=deductible_total,
        series=series, months_available=len(statements), notes=notes,
    )


def report_to_text(report: CreditHealthReport) -> str:
    """Resumen del reporte para el contexto del chat (los números ya vienen calculados)."""
    if report.statement_id is None:
        return "SALUD CREDITICIA: sin estados de cuenta cargados."
    lines = [
        f"SALUD CREDITICIA (calculada automáticamente, periodo que termina el {dmy(report.period_end)})",
        f"- Índice: {report.score if report.score is not None else 'N/D'}/100 · nivel: {report.level}. {DISCLAIMER}",
        "- Indicadores:",
    ]
    status = {"good": "bien", "warn": "atención", "bad": "riesgo", "na": "sin datos"}
    lines += [f"    · {i.label}: {i.display} [{status[i.status]}] — {i.detail}" for i in report.indicators]
    if report.categories:
        lines.append("- Gasto por categoría en este periodo:")
        lines += [f"    · {c.label}: {money(c.amount)} ({c.share:.0f}%)" for c in report.categories]
        if report.essential_share is not None:
            lines.append(f"- Gasto esencial: {report.essential_share:.0f}% · discrecional: {100 - report.essential_share:.0f}%")
    if len(report.series) > 1:
        lines.append("- Evolución mensual (fin de periodo: uso del crédito, saldo, índice):")
        lines += [f"    · {dmy(p.period_end)}: {p.utilization if p.utilization is not None else 'N/D'}%, "
                  f"{money(p.balance)}, {p.score if p.score is not None else 'N/D'}" for p in report.series[-12:]]
    if report.recommendations:
        lines.append("- Recomendaciones calculadas:")
        lines += [f"    · {r}" for r in report.recommendations]
    return "\n".join(lines)
