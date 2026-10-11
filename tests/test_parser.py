from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.exceptions import StatementParseError
from app.schemas import TransactionKind
from app.services.pdf_parser import parse_statement_text

FIXTURE = Path(__file__).parent / "fixtures" / "nu_statement_sample.txt"


@pytest.fixture(scope="module")
def parsed():
    return parse_statement_text(FIXTURE.read_text(encoding="utf-8"))


def test_header_fields(parsed):
    assert parsed.card_last4 == "1234"
    assert parsed.period_start == date(2026, 8, 10)
    assert parsed.cut_date == date(2026, 9, 9)
    assert parsed.due_date == date(2026, 9, 21)
    assert parsed.days_in_period == 31


def test_amounts(parsed):
    assert parsed.pay_no_interest == Decimal("17510.97")
    assert parsed.min_payment == Decimal("396.06")
    assert parsed.min_plus_installments == Decimal("2787.41")
    assert parsed.credit_limit == Decimal("25000.00")
    assert parsed.available_credit == Decimal("7489.02")
    assert parsed.cat == Decimal("18.44")
    assert parsed.annual_rate == Decimal("57.00")
    assert parsed.interest_last_12m == Decimal("5918.32")
    assert parsed.total_charges == Decimal("17493.00")


def test_transactions(parsed):
    assert len(parsed.transactions) == 17
    payment = next(t for t in parsed.transactions if t.kind == TransactionKind.PAYMENT)
    assert payment.amount == Decimal("-11553.87")
    assert "SPEI" in (payment.detail or "")
    purchases = sum(t.amount for t in parsed.transactions if t.kind == TransactionKind.PURCHASE)
    assert purchases == Decimal("14984.20")


def test_installments_and_scenarios(parsed):
    (plan,) = parsed.installments
    assert plan.original_amount == Decimal("12444.01")
    assert (plan.current_installment, plan.total_installments) == (6, 6)
    assert "febrero" in plan.description
    assert len(parsed.payment_scenarios) == 4
    assert parsed.payment_scenarios[0].months_to_pay == "120 meses"


def test_no_consistency_warnings(parsed):
    assert parsed.warnings == []


def test_rejects_unknown_document():
    with pytest.raises(StatementParseError):
        parse_statement_text("Esto no es un estado de cuenta " * 20)
