"""Formato de montos, fechas y porcentajes para los textos que ve la persona (y el LLM)."""
from __future__ import annotations

from decimal import Decimal


def money(value: Decimal | None) -> str:
    if value is None:
        return "N/D"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def dmy(d) -> str:
    return d.strftime("%d/%m/%Y") if d else "N/D"


def pct(part: Decimal, whole: Decimal) -> float:
    """part / whole en porcentaje; 0 si whole es 0."""
    return float(part / whole * 100) if whole else 0.0
