"""Convierte un ParsedStatement en fragmentos de texto autocontenidos para RAG.

Los totales y agregados se calculan aquí (en código) y se escriben en el texto, así el LLM
no tiene que sumar de memoria. Con `redact_pii=True` no se incluye dirección, CLABE, RFC ni nombre completo.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from app.schemas import Chunk, ParsedStatement, Transaction, TransactionKind
from app.services.categorizer import summarize_categories

KIND_LABEL = {
    TransactionKind.PURCHASE: "compra",
    TransactionKind.PAYMENT: "pago/abono",
    TransactionKind.INTEREST: "interés",
    TransactionKind.TAX: "IVA",
    TransactionKind.FEE: "comisión",
    TransactionKind.INSTALLMENT: "mensualidad a meses",
    TransactionKind.OTHER: "otro",
}


def money(value: Decimal | None) -> str:
    if value is None:
        return "N/D"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def dmy(d) -> str:
    return d.strftime("%d/%m/%Y") if d else "N/D"


class StatementChunker:
    def __init__(self, tx_per_chunk: int = 10, redact_pii: bool = True):
        self.tx_per_chunk = max(1, tx_per_chunk)
        self.redact_pii = redact_pii

    # ------------------------------------------------------------------ API
    def chunk(self, s: ParsedStatement) -> list[Chunk]:
        header = (
            f"[Estado de cuenta {s.bank.upper()} · tarjeta terminación {s.card_last4 or '----'} · "
            f"periodo {dmy(s.period_start)} al {dmy(s.period_end)}]"
        )
        chunks = [self._summary(s, header)]
        if s.payment_scenarios:
            chunks.append(self._scenarios(s, header))
        if s.installments:
            chunks.append(self._installments(s, header))
        chunks.extend(self._transactions(s, header))
        chunks.append(self._analytics(s, header))
        for i, c in enumerate(chunks):
            c.position = i
        return chunks

    # ------------------------------------------------------------------ builders
    def _summary(self, s: ParsedStatement, header: str) -> Chunk:
        holder = "(oculto)" if self.redact_pii else (s.holder_name or "N/D")
        used = None
        if s.credit_limit and s.available_credit is not None and s.credit_limit > 0:
            used = (s.credit_limit - s.available_credit) / s.credit_limit * 100
        lines = [
            header,
            "RESUMEN GENERAL DEL ESTADO DE CUENTA",
            f"Titular: {holder}",
            f"Periodo: {dmy(s.period_start)} al {dmy(s.period_end)} ({s.days_in_period or 'N/D'} días). "
            f"Fecha de corte: {dmy(s.cut_date)}.",
            f"Fecha límite de pago: {dmy(s.due_date)}.",
            "PAGOS REQUERIDOS",
            f"- Pago para no generar intereses: {money(s.pay_no_interest)}",
            f"- Pago mínimo: {money(s.min_payment)}",
            f"- Pago mínimo + mensualidades de compras a meses: {money(s.min_plus_installments)}",
            "MOVIMIENTO DEL PERIODO",
            f"- Adeudo del periodo anterior: {money(s.previous_balance)}",
            f"- Cargos regulares (no a meses): {money(s.regular_charges)}",
            f"- Cargos y compras a meses (capital): {money(s.installment_capital)}",
            f"- Intereses: {money(s.interest)}",
            f"- Comisiones: {money(s.commissions)}",
            f"- IVA de intereses y comisiones: {money(s.iva)}",
            f"- Pagos y abonos: {money(s.payments)}",
            "COSTO Y CRÉDITO",
            f"- CAT: {s.cat}% sin IVA" if s.cat is not None else "- CAT: N/D",
            f"- Tasa de interés anual ordinaria (fija): {s.annual_rate}%" if s.annual_rate is not None else "- Tasa anual: N/D",
            f"- Intereses pagados en los últimos 12 meses: {money(s.interest_last_12m)}",
            f"- Límite de crédito: {money(s.credit_limit)}",
            f"- Crédito disponible: {money(s.available_credit)}",
        ]
        if used is not None:
            lines.append(f"- Uso del límite de crédito: {used:.1f}%")
        return Chunk(kind="summary", content="\n".join(lines), meta={"period_end": str(s.period_end)})

    def _scenarios(self, s: ParsedStatement, header: str) -> Chunk:
        lines = [header, "CUÁNTO PAGARÍAS POR TUS COMPRAS REGULARES SEGÚN LO QUE PAGUES CADA MES"]
        for sc in s.payment_scenarios:
            lines.append(
                f"- Si pagas {sc.label.lower()} ({money(sc.monthly_payment)} al mes): terminas de pagar en "
                f"{sc.months_to_pay} y pagas {money(sc.total_interest)} de intereses."
            )
        lines.append("Pagar solo el mínimo aumenta el tiempo de pago y el costo total de la deuda.")
        return Chunk(kind="scenarios", content="\n".join(lines))

    def _installments(self, s: ParsedStatement, header: str) -> Chunk:
        lines = [header, "COMPRAS Y CARGOS DIFERIDOS A MESES CON INTERESES"]
        for i in s.installments:
            left = i.total_installments - i.current_installment
            lines.append(
                f"- {i.description} (operación {dmy(i.operation_date)}): monto original {money(i.original_amount)}, "
                f"saldo pendiente {money(i.pending_balance)}, mensualidad {i.current_installment}/{i.total_installments} "
                f"(quedan {left} mensualidades después de esta), pago requerido {money(i.required_payment)}, "
                f"intereses del periodo {money(i.period_interest)} + IVA {money(i.period_iva)}, tasa {i.annual_rate}%."
            )
        return Chunk(kind="installments", content="\n".join(lines))

    def _transactions(self, s: ParsedStatement, header: str) -> list[Chunk]:
        out: list[Chunk] = []
        txs = s.transactions
        for start in range(0, len(txs), self.tx_per_chunk):
            part = txs[start : start + self.tx_per_chunk]
            lines = [header, f"MOVIMIENTOS {start + 1} a {start + len(part)} de {len(txs)} (cargos positivos, abonos negativos)"]
            for t in part:
                lines.append(self._tx_line(t))
            out.append(
                Chunk(
                    kind="transactions",
                    content="\n".join(lines),
                    meta={"from": start + 1, "to": start + len(part)},
                )
            )
        return out

    @staticmethod
    def _tx_line(t: Transaction) -> str:
        extra = f" ({t.detail})" if t.detail else ""
        return (
            f"- {dmy(t.operation_date)} | {t.description} | {money(t.amount)} | "
            f"{KIND_LABEL[t.kind]} | cargado el {dmy(t.charge_date)}{extra}"
        )

    def _analytics(self, s: ParsedStatement, header: str) -> Chunk:
        z = Decimal(0)
        purchases = [t for t in s.transactions if t.kind == TransactionKind.PURCHASE]
        payments = [t for t in s.transactions if t.kind == TransactionKind.PAYMENT]
        fin = [t for t in s.transactions if t.kind in (TransactionKind.INTEREST, TransactionKind.TAX, TransactionKind.FEE)]
        by_merchant: dict[str, Decimal] = defaultdict(lambda: z)
        by_day: dict = defaultdict(lambda: z)
        for t in purchases:
            by_merchant[t.description.strip()] += t.amount
            by_day[t.operation_date] += t.amount

        lines = [header, "ANÁLISIS DE GASTOS DEL PERIODO (calculado automáticamente)"]
        lines.append(f"- Compras regulares: {len(purchases)} movimientos por un total de {money(sum((t.amount for t in purchases), z))}")
        lines.append("- Compras por comercio (mayor a menor):")
        for name, total in sorted(by_merchant.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"    · {name}: {money(total)}")
        categories = summarize_categories(s.transactions)
        if any(c.category != "sin_clasificar" for c in categories):
            lines.append("- Compras por categoría de gasto (mayor a menor):")
            for c in categories:
                flag = " (posible deducible: requiere CFDI)" if c.deductible else ""
                lines.append(f"    · {c.label}: {money(c.amount)} ({c.share:.0f}%, {c.count} compras){flag}")
        lines.append("- Compras por día de operación:")
        for day, total in sorted(by_day.items()):
            lines.append(f"    · {dmy(day)}: {money(total)}")
        if purchases:
            lines.append("- 5 compras individuales más grandes:")
            for t in sorted(purchases, key=lambda t: t.amount, reverse=True)[:5]:
                lines.append(f"    · {t.description} el {dmy(t.operation_date)}: {money(t.amount)}")
        lines.append(f"- Pagos/abonos realizados: {money(-sum((t.amount for t in payments), z))} en {len(payments)} movimiento(s)")
        lines.append(f"- Intereses, IVA y comisiones cargados como movimientos: {money(sum((t.amount for t in fin), z))}")
        return Chunk(kind="analytics", content="\n".join(lines))
