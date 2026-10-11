"""Instrumentación: latencia, tokens, costo y errores de cada llamada (se guarda en PostgreSQL)."""
from __future__ import annotations

import logging
import time
from typing import Protocol

from app.core.config import Settings
from app.core.logging import current_request_id, current_user_id, log_event

logger = logging.getLogger(__name__)


class MetricsSink(Protocol):
    def add(self, **fields) -> None: ...


class Stopwatch:
    """with Stopwatch() as sw: ...; sw.ms"""

    def __enter__(self) -> "Stopwatch":
        self._t0 = time.perf_counter()
        self.ms = 0.0
        return self

    def __exit__(self, *exc) -> None:
        self.ms = (time.perf_counter() - self._t0) * 1000

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000


class MetricsService:
    def __init__(self, sink: MetricsSink, settings: Settings):
        self._sink = sink
        self._price_in = settings.price_input_per_mtok
        self._price_out = settings.price_output_per_mtok

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self._price_in + output_tokens * self._price_out) / 1_000_000

    def record(
        self,
        *,
        operation: str,
        provider: str,
        model: str,
        latency_ms: float,
        status: str = "ok",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
        error: str | None = None,
        meta: dict | None = None,
    ) -> None:
        cost = self.cost_usd(input_tokens, output_tokens) if cost_usd is None else cost_usd
        log_event(
            logger, f"metric.{operation}",
            provider=provider, model=model, status=status, latency_ms=round(latency_ms, 1),
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=round(cost, 6),
        )  # fmt: skip
        try:
            self._sink.add(
                request_id=current_request_id(), user_id=current_user_id(), operation=operation,
                provider=provider, model=model,
                status=status, latency_ms=latency_ms, input_tokens=input_tokens,
                output_tokens=output_tokens, cost_usd=cost, error=error, meta=meta or {},
            )  # fmt: skip
        except Exception:  # las métricas nunca deben romper el flujo principal
            logger.exception("No se pudo guardar la métrica")
