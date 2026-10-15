from datetime import date
from decimal import Decimal

import pytest

from app.schemas import Transaction, TransactionKind
from app.services.categorizer import (
    TransactionCategorizer,
    normalize_merchant,
    rule_category,
    summarize_categories,
)


def tx(description, amount="100", kind=TransactionKind.PURCHASE, category=None):
    d = date(2026, 9, 1)
    return Transaction(operation_date=d, charge_date=d, description=description, amount=Decimal(amount),
                       kind=kind, category=category)  # fmt: skip


class Overrides:
    def __init__(self, data=None):
        self.data = data or {}

    def get_merchant_categories(self, user_id, keys):
        return {k: v for (u, k), v in self.data.items() if u == user_id and k in keys}


class FakeClassifier:
    def __init__(self, answers=None, fail=False):
        self.answers, self.fail, self.calls = answers or {}, fail, []

    def classify(self, merchants):
        self.calls.append(list(merchants))
        if self.fail:
            raise RuntimeError("sin red")
        return {m: self.answers.get(m, ("sin_clasificar", 0.2)) for m in merchants}


@pytest.mark.parametrize("description,expected", [
    ("FARMACIAS GUADALAJARA", "salud"),
    ("Liverpool Guadalajara", "hogar"),          # "guadalajara" sola no es salud
    ("UBER *TRIP", "transporte"),
    ("UBER EATS", "alimentos"),
    ("Gasolinera Aguascalientes", "transporte"),  # "agua" dentro de una palabra no es servicio
    ("NETFLIX.COM", "entretenimiento"),
    ("COLEGIATURA OCT", "educacion"),
    ("Apple.Com/Bill", "servicios"),
    ("Dor Gaynor Metepec", None),
    # palabras de uso diario en gastos en efectivo
    ("Mercado", "supermercado"),
    ("Mercado Libre", "hogar"),                   # no confundir con el mercado
    ("Supermercado", "supermercado"),
    ("Pan", "supermercado"),
    ("Panda Express Galerias", "alimentos"),      # "pan" no debe atrapar a Panda
    ("Taxi", "transporte"),
    ("Comida corrida", "alimentos"),
    ("Pago de luz", "servicios"),
    ("Renta", "hogar"),
    ("Papelería", "hogar"),                       # no es Educación: no debe marcarse como posible deducible
])
def test_rules(description, expected):
    assert rule_category(normalize_merchant(description)) == expected


def test_normalize_is_stable():
    assert normalize_merchant("  Ópticas  Devlyn 0423 ") == normalize_merchant("OPTICAS DEVLYN")


def test_cascade_user_then_rules_then_llm():
    overrides = Overrides({(1, "sears metepec"): "ropa_cuidado"})
    llm = FakeClassifier({"dor gaynor metepec": ("ropa_cuidado", 0.9), "vsfa galerias metepec": ("hogar", 0.4)})
    out = TransactionCategorizer(overrides, llm).categorize(1, [
        tx("Sears Metepec"), tx("Panda Express Galerias"), tx("Dor Gaynor Metepec"), tx("Vsfa Galerias Metepec"),
        tx("¡Gracias por tu pago!", "-500", TransactionKind.PAYMENT),
    ])
    got = {t.description: (t.category, t.category_source) for t in out}
    assert got["Sears Metepec"] == ("ropa_cuidado", "user")        # la corrección gana a la regla (hogar)
    assert got["Panda Express Galerias"] == ("alimentos", "rule")
    assert got["Dor Gaynor Metepec"] == ("ropa_cuidado", "llm")
    assert got["Vsfa Galerias Metepec"] == ("sin_clasificar", "none")  # confianza baja
    assert got["¡Gracias por tu pago!"] == (None, None)               # solo se clasifican compras
    assert llm.calls == [["dor gaynor metepec", "vsfa galerias metepec"]]  # una llamada, solo lo pendiente


def test_overrides_are_per_user():
    overrides = Overrides({(1, "dor gaynor metepec"): "hogar"})
    out = TransactionCategorizer(overrides).categorize(2, [tx("Dor Gaynor Metepec")])
    assert out[0].category == "sin_clasificar"


def test_llm_failure_never_breaks_ingestion():
    out = TransactionCategorizer(Overrides(), FakeClassifier(fail=True)).categorize(1, [tx("Dor Gaynor"), tx("Netflix")])
    assert [t.category for t in out] == ["sin_clasificar", "entretenimiento"]


def test_invalid_llm_category_is_ignored():
    llm = FakeClassifier({"dor gaynor": ("casino", 0.99)})
    out = TransactionCategorizer(Overrides(), llm).categorize(1, [tx("Dor Gaynor")])
    assert out[0].category == "sin_clasificar"


def test_summary_shares_and_deductibles():
    totals = summarize_categories([
        tx("a", "300", category="salud"), tx("b", "100", category="entretenimiento"),
        tx("c", "100"), tx("pago", "-400", TransactionKind.PAYMENT),
    ])
    by = {c.category: c for c in totals}
    assert [c.category for c in totals][0] == "salud"
    assert by["salud"].share == pytest.approx(60) and by["salud"].deductible and by["salud"].essential
    assert by["sin_clasificar"].amount == Decimal("100")
    assert sum(c.amount for c in totals) == Decimal("500")
    assert tx("x", category="educacion").deductible and not tx("x", category="hogar").deductible
