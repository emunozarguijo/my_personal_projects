"""Contratos de datos compartidos entre capas (parser, servicios, pipeline y UI)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field


class TransactionKind(str, Enum):
    PURCHASE = "purchase"
    PAYMENT = "payment"
    INTEREST = "interest"
    TAX = "tax"
    FEE = "fee"
    INSTALLMENT = "installment"
    OTHER = "other"


class Transaction(BaseModel):
    id: int | None = None  # asignado al guardar
    operation_date: date
    charge_date: date
    description: str
    amount: Decimal  # positivo = cargo, negativo = abono
    kind: TransactionKind = TransactionKind.PURCHASE
    detail: str | None = None
    currency: str = "MXN"
    category: str | None = None         # solo compras; ver services/categorizer.CATEGORIES
    category_source: str | None = None  # user | rule | llm | none

    @computed_field
    @property
    def deductible(self) -> bool:
        """Posible deducción personal (salud, educación): solo es deducible con su CFDI."""
        return self.category in ("salud", "educacion")


class Installment(BaseModel):
    """Compra o cargo diferido a meses (con intereses)."""

    operation_date: date
    description: str
    original_amount: Decimal
    pending_balance: Decimal
    period_interest: Decimal
    period_iva: Decimal
    required_payment: Decimal
    current_installment: int
    total_installments: int
    annual_rate: Decimal


class PaymentScenario(BaseModel):
    label: str
    monthly_payment: Decimal
    months_to_pay: str  # "120 meses" | "este periodo"
    total_interest: Decimal


class ParsedStatement(BaseModel):
    bank: str = "nu"
    card_last4: str | None = None
    holder_name: str | None = None
    period_start: date
    period_end: date
    cut_date: date
    due_date: date | None = None
    days_in_period: int | None = None

    previous_balance: Decimal | None = None
    regular_charges: Decimal | None = None
    installment_capital: Decimal | None = None
    interest: Decimal | None = None
    commissions: Decimal | None = None
    iva: Decimal | None = None
    payments: Decimal | None = None
    pay_no_interest: Decimal | None = None
    min_payment: Decimal | None = None
    min_plus_installments: Decimal | None = None
    credit_limit: Decimal | None = None
    available_credit: Decimal | None = None
    cat: Decimal | None = None
    annual_rate: Decimal | None = None
    interest_last_12m: Decimal | None = None
    total_charges: Decimal | None = None
    total_payments: Decimal | None = None

    payment_scenarios: list[PaymentScenario] = Field(default_factory=list)
    transactions: list[Transaction] = Field(default_factory=list)
    installments: list[Installment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class StatementDetail(ParsedStatement):
    """Un estado de cuenta ya guardado, con todo su detalle (lo usa la interfaz web)."""

    id: int
    filename: str


class Chunk(BaseModel):
    kind: str  # summary | scenarios | installments | transactions | analytics
    position: int = 0
    content: str
    meta: dict[str, Any] = Field(default_factory=dict)


class RetrievedChunk(Chunk):
    statement_id: int
    score: float = 0.0


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMResponse(BaseModel):
    text: str
    provider: str
    model: str
    usage: TokenUsage
    latency_ms: float
    cost_usd: float = 0.0


class StatementSummary(BaseModel):
    id: int
    filename: str
    card_last4: str | None
    period_start: date
    period_end: date
    due_date: date | None
    pay_no_interest: Decimal | None

    @property
    def label(self) -> str:
        return f"{self.period_start:%d/%m/%Y} – {self.period_end:%d/%m/%Y}  (••{self.card_last4 or '----'})"


class IngestResult(BaseModel):
    ok: bool
    statement_id: int | None = None
    duplicate: bool = False
    n_transactions: int = 0
    n_installments: int = 0
    n_chunks: int = 0
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    timings_ms: dict[str, float] = Field(default_factory=dict)
    total_ms: float = 0.0


class AskResult(BaseModel):
    ok: bool
    answer: str = ""
    sources: list[RetrievedChunk] = Field(default_factory=list)
    llm: LLMResponse | None = None
    error: str | None = None
    timings_ms: dict[str, float] = Field(default_factory=dict)
    total_ms: float = 0.0


class OperationStats(BaseModel):
    operation: str
    calls: int
    errors: int
    avg_latency_ms: float
    p95_latency_ms: float
    input_tokens: int
    output_tokens: int
    cost_usd: float


class ApiCallRecord(BaseModel):
    created_at: str
    operation: str
    provider: str
    model: str
    status: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    request_id: str


class UserAccount(BaseModel):
    """Cuenta tal como sale del repositorio (uso interno: incluye el hash de la contraseña)."""

    id: int
    username: str
    role: str = "user"
    is_active: bool = True
    password_hash: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def public(self) -> "UserPublic":
        return UserPublic(id=self.id, username=self.username, role=self.role)


class UserPublic(BaseModel):
    """Lo único de una cuenta que sale por la API."""

    id: int
    username: str
    role: str


# ---------------------------------------------------------------------- salud crediticia
Status = Literal["good", "warn", "bad", "na"]


class HealthIndicator(BaseModel):
    key: str
    label: str
    status: Status
    value: float | None = None      # número base (porcentaje, pesos…) para gráficas
    display: str                    # valor ya formateado para mostrar
    detail: str                     # explicación en una frase
    weight: int = 0                 # peso en el índice (0 = informativo)


class CategoryTotal(BaseModel):
    category: str
    label: str
    amount: Decimal
    share: float                    # % del total de compras
    essential: bool
    deductible: bool
    count: int


class MonthPoint(BaseModel):
    statement_id: int
    period_end: date
    utilization: float | None
    balance: Decimal | None         # pago para no generar intereses
    purchases: Decimal
    payments: Decimal
    finance_cost: Decimal           # intereses + comisiones + IVA
    score: int | None


class CreditHealthReport(BaseModel):
    statement_id: int | None
    period_end: date | None
    score: int | None
    level: str                      # Saludable | Aceptable | En riesgo | Crítica | Sin datos
    indicators: list[HealthIndicator] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    categories: list[CategoryTotal] = Field(default_factory=list)
    essential_share: float | None = None
    deductible_total: Decimal = Decimal(0)
    series: list[MonthPoint] = Field(default_factory=list)
    months_available: int = 0
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------- finanzas del mes (registro manual)
IncomeKind = Literal["salario", "honorarios", "otros"]


class IncomeItem(BaseModel):
    id: int
    date: date
    amount: Decimal
    kind: str
    description: str = ""
    recurring: bool = False
    end_date: date | None = None


class CashExpenseItem(BaseModel):
    id: int
    date: date
    amount: Decimal
    description: str = ""
    category: str
    category_source: str = "user"


class MonthCategory(BaseModel):
    category: str
    label: str
    total: Decimal
    cash: Decimal
    nu: Decimal
    share: float          # % del gasto del mes
    essential: bool
    deductible: bool


class MonthMovement(BaseModel):
    source: Literal["efectivo", "nu", "ingreso"]
    id: int | None        # id del gasto en efectivo / ingreso (editable) o del cargo Nu (solo lectura)
    date: date
    description: str
    amount: Decimal       # positivo; ingresos y gastos se distinguen por `source`
    category: str | None = None
    recurring: bool = False


class MonthlySummary(BaseModel):
    month: str                                   # YYYY-MM
    income_total: Decimal
    cash_total: Decimal
    nu_total: Decimal                            # compras con Nu (por fecha de operación)
    finance_cost: Decimal                        # intereses + comisiones + IVA de Nu cargados en el mes
    expense_total: Decimal                       # efectivo + Nu + costo financiero
    balance: Decimal                             # ingresos − gastos
    savings_rate: float | None
    card_payments: Decimal                       # pagos a la tarjeta (informativo: no son gasto)
    incomes: list[IncomeItem] = Field(default_factory=list)
    categories: list[MonthCategory] = Field(default_factory=list)
    movements: list[MonthMovement] = Field(default_factory=list)
    score: int | None = None
    level: str = "Sin datos suficientes"
    indicators: list[HealthIndicator] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    nu_covered_until: date | None = None         # último día del mes con cargos Nu ya cargados
    notes: list[str] = Field(default_factory=list)
