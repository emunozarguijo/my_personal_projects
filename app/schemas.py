"""Contratos de datos compartidos entre capas (parser, servicios, pipeline y UI)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TransactionKind(str, Enum):
    PURCHASE = "purchase"
    PAYMENT = "payment"
    INTEREST = "interest"
    TAX = "tax"
    FEE = "fee"
    INSTALLMENT = "installment"
    OTHER = "other"


class Transaction(BaseModel):
    operation_date: date
    charge_date: date
    description: str
    amount: Decimal  # positivo = cargo, negativo = abono
    kind: TransactionKind = TransactionKind.PURCHASE
    detail: str | None = None
    currency: str = "MXN"


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
