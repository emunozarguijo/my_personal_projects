"""Extracción de texto del PDF y parseo del estado de cuenta (formato Nu México).

Para soportar otro banco: crear una subclase de `BaseStatementParser` y registrarla en `PARSERS`.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.core.exceptions import PdfExtractionError, StatementParseError
from app.schemas import (
    Installment,
    ParsedStatement,
    PaymentScenario,
    Transaction,
    TransactionKind,
)

logger = logging.getLogger(__name__)

MONTHS = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}  # fmt: skip

_DATE = r"(\d{2} [A-Z]{3} \d{4})"
_MONEY = r"\$\s?([\d,]+\.\d{2})"


def parse_date(raw: str) -> date:
    day, mon, year = raw.split()
    return date(int(year), MONTHS[mon], int(day))


def parse_money(raw: str) -> Decimal:
    return Decimal(raw.replace(",", "").replace("$", "").strip())


class PdfTextExtractor:
    """Lee el texto del PDF con pdfplumber."""

    def __init__(self, max_mb: int = 20):
        self.max_bytes = max_mb * 1024 * 1024

    def extract(self, path: str | Path) -> str:
        import pdfplumber  # import perezoso

        path = Path(path)
        if path.stat().st_size > self.max_bytes:
            raise PdfExtractionError(f"El PDF excede {self.max_bytes // (1024 * 1024)} MB")
        try:
            with pdfplumber.open(path) as pdf:
                pages = [(page.extract_text() or "") for page in pdf.pages]
        except Exception as exc:  # pdf corrupto, protegido, etc.
            raise PdfExtractionError(f"No se pudo abrir el PDF: {exc}") from exc
        text = "\n".join(pages).strip()
        if len(text) < 200:
            raise PdfExtractionError(
                "El PDF no contiene texto extraíble (¿está escaneado o protegido con contraseña?)"
            )
        return text


class BaseStatementParser(ABC):
    @abstractmethod
    def can_parse(self, text: str) -> bool: ...

    @abstractmethod
    def parse(self, text: str) -> ParsedStatement: ...


class NuStatementParser(BaseStatementParser):
    _TX = re.compile(
        rf"^{_DATE} {_DATE} (.+?) \| RFC: (\S+) ([+-])\$([\d,]+\.\d{{2}})\s*$"
    )
    _INST = re.compile(
        rf"^{_DATE}\s+((?:\$[\d,]+\.\d{{2}}\s+){{5}})(\d+)/(\d+)\s+([\d.]+)%\s*$"
    )
    _SCENARIO = re.compile(
        rf"^(El pago mínimo|\d+ veces el pago mínimo|El pago para no generar intereses)\s+{_MONEY}\s+"
        rf"(\d+ meses|este periodo)\s+{_MONEY}\s*$"
    )
    _STOP_DETAIL = ("Total de", "Notas:", "Página", "Número de tarjeta", "CARGOS,", "COMPRAS Y")

    def can_parse(self, text: str) -> bool:
        return "Nubank" in text or "Tarjeta de Crédito Nu" in text

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _money(pattern: str, text: str, flags: int = 0) -> Decimal | None:
        m = re.search(pattern, text, flags)
        return parse_money(m.group(1)) if m else None

    @staticmethod
    def _classify(description: str, amount: Decimal) -> TransactionKind:
        d = description.lower()
        if amount < 0 or "gracias" in d or "grácias" in d:
            return TransactionKind.PAYMENT
        if d.startswith("iva"):
            return TransactionKind.TAX
        if "plan de pagos fijos" in d:
            return TransactionKind.INTEREST if "intereses" in d else TransactionKind.INSTALLMENT
        if "interes" in d:
            return TransactionKind.INTEREST
        if "comisi" in d:
            return TransactionKind.FEE
        return TransactionKind.PURCHASE

    # ------------------------------------------------------------------ parse
    def parse(self, text: str) -> ParsedStatement:
        warnings: list[str] = []

        cut = re.search(rf"Fecha de corte:\s*{_DATE}", text)
        start = re.search(rf"{_DATE} al \d{{2}} [A-Z]{{3}}", text)
        if not cut or not start:
            raise StatementParseError("No se encontraron el periodo / fecha de corte del estado de cuenta")
        cut_date = parse_date(cut.group(1))

        def opt(name: str, value):
            if value is None:
                warnings.append(f"No se pudo leer: {name}")
            return value

        due = re.search(rf"Fecha límite de pago\d*:\s*\w+,\s*{_DATE}", text)
        days = re.search(r"Número de días en el periodo:\s*(\d+)", text)
        last4 = re.search(r"XXXX-XXXX-XXXX-(\d{4})", text)
        holder = re.search(r"Tarjeta titular:\s*(.+)", text)
        rates = re.search(r"([\d.]+)%\s*Sin IVA\s*([\d.]+)%", text)

        # page 1 viene con el layout mezclado: el importe puede estar antes o después de la etiqueta
        min_plus = self._money(r"Pago mínimo \+ compras y\s*\$\s?([\d,]+\.\d{2})", text) or self._money(
            r"\$\s?([\d,]+\.\d{2})\s*\n\s*cargos diferidos a meses\d*:", text
        )

        stmt = ParsedStatement(
            card_last4=last4.group(1) if last4 else None,
            holder_name=holder.group(1).strip() if holder else None,
            period_start=parse_date(start.group(1)),
            period_end=cut_date,
            cut_date=cut_date,
            due_date=parse_date(due.group(1)) if due else opt("fecha límite de pago", None),
            days_in_period=int(days.group(1)) if days else None,
            previous_balance=opt("adeudo anterior", self._money(rf"Adeudo del periodo anterior\s*=\s*{_MONEY}", text)),
            regular_charges=self._money(rf"Cargos regulares \(no a meses\)\s*\+\s*{_MONEY}", text),
            installment_capital=self._money(rf"Cargos y compras a meses \(capital\)\d*\s*\+\s*{_MONEY}", text),
            interest=self._money(rf"Monto de intereses\d*\s*\+\s*{_MONEY}", text),
            commissions=self._money(rf"Monto de comisiones\s*\+\s*{_MONEY}", text),
            iva=self._money(rf"IVA de intereses y comisiones\d*\s*\+\s*{_MONEY}", text),
            payments=self._money(rf"Pagos y abonos\s*-\s*{_MONEY}", text),
            pay_no_interest=opt(
                "pago para no generar intereses",
                self._money(rf"Pago para no generar intereses\d*:\s*{_MONEY}", text),
            ),
            min_payment=opt("pago mínimo", self._money(rf"Pago mínimo\d+:\s*{_MONEY}", text)),
            min_plus_installments=min_plus,
            credit_limit=self._money(rf"Límite de crédito\s*{_MONEY}", text),
            available_credit=self._money(rf"Crédito disponible\s*{_MONEY}", text),
            cat=Decimal(rates.group(1)) if rates else None,
            annual_rate=Decimal(rates.group(2)) if rates else None,
            interest_last_12m=self._money(rf"Monto de intereses pagados en los\s*{_MONEY}", text),
            total_charges=self._money(rf"Total de cargos\s*\+{_MONEY}", text),
            total_payments=self._money(rf"Total de abonos\s*-{_MONEY}", text),
        )

        self._parse_lines(text, stmt)
        stmt.warnings = warnings + self._validate(stmt)
        logger.info(
            "Estado de cuenta parseado: %s movimientos, %s planes a meses, %s avisos",
            len(stmt.transactions), len(stmt.installments), len(stmt.warnings),
        )
        return stmt

    def _parse_lines(self, text: str, stmt: ParsedStatement) -> None:
        lines = [ln.strip() for ln in text.splitlines()]
        current: Transaction | None = None

        for i, line in enumerate(lines):
            if not line:
                continue

            if m := self._TX.match(line):
                amount = parse_money(m.group(6)) * (-1 if m.group(5) == "-" else 1)
                desc = m.group(3).strip()
                current = Transaction(
                    operation_date=parse_date(m.group(1)),
                    charge_date=parse_date(m.group(2)),
                    description=desc,
                    amount=amount,
                    kind=self._classify(desc, amount),
                )
                stmt.transactions.append(current)
                continue

            if m := self._INST.match(line):
                amounts = [parse_money(x) for x in re.findall(_MONEY, m.group(2))]
                prev_line = lines[i - 1] if i > 0 else ""
                next_line = lines[i + 1] if i + 1 < len(lines) else ""
                parts = [
                    p for p in (prev_line, next_line)
                    if p and not p.startswith(("Notas:", "Página", "Descripción", "Fecha de")) and "$" not in p
                ]
                stmt.installments.append(
                    Installment(
                        operation_date=parse_date(m.group(1)),
                        description=" ".join(parts) or "Compra a meses",
                        original_amount=amounts[0],
                        pending_balance=amounts[1],
                        period_interest=amounts[2],
                        period_iva=amounts[3],
                        required_payment=amounts[4],
                        current_installment=int(m.group(3)),
                        total_installments=int(m.group(4)),
                        annual_rate=Decimal(m.group(5)),
                    )
                )
                continue

            if m := self._SCENARIO.match(line):
                stmt.payment_scenarios.append(
                    PaymentScenario(
                        label=m.group(1),
                        monthly_payment=parse_money(m.group(2)),
                        months_to_pay=m.group(3),
                        total_interest=parse_money(m.group(4)),
                    )
                )
                continue

            # líneas de detalle ("Tarjeta virtual ****", "Abono (SPEI)", "Cambio (MXN...)")
            if current is not None:
                if line.startswith(self._STOP_DETAIL):
                    current = None
                elif len(line) < 90:
                    current.detail = f"{current.detail}; {line}" if current.detail else line

    @staticmethod
    def _validate(stmt: ParsedStatement) -> list[str]:
        """Chequeos de consistencia: avisan si el parseo pudo omitir algo."""
        tol = Decimal("0.05")
        out: list[str] = []
        z = Decimal(0)

        parts = [stmt.previous_balance, stmt.regular_charges, stmt.installment_capital,
                 stmt.interest, stmt.commissions, stmt.iva, stmt.payments, stmt.pay_no_interest]  # fmt: skip
        if all(p is not None for p in parts):
            calc = sum(parts[:6], z) - parts[6]
            if abs(calc - parts[7]) > tol:
                out.append(f"El resumen no cuadra: calculado {calc} vs pago para no generar intereses {parts[7]}")

        purchases = sum((t.amount for t in stmt.transactions if t.kind == TransactionKind.PURCHASE), z)
        if stmt.regular_charges is not None and abs(purchases - stmt.regular_charges) > tol:
            out.append(f"Compras parseadas ({purchases}) ≠ cargos regulares del resumen ({stmt.regular_charges})")

        paid = -sum((t.amount for t in stmt.transactions if t.kind == TransactionKind.PAYMENT), z)
        if stmt.payments is not None and abs(paid - stmt.payments) > tol:
            out.append(f"Abonos parseados ({paid}) ≠ pagos del resumen ({stmt.payments})")

        if not stmt.transactions:
            out.append("No se encontraron movimientos")
        return out


PARSERS: list[BaseStatementParser] = [NuStatementParser()]


def parse_statement_text(text: str) -> ParsedStatement:
    for parser in PARSERS:
        if parser.can_parse(text):
            return parser.parse(text)
    raise StatementParseError("Formato de estado de cuenta no soportado (por ahora solo Nu México)")
