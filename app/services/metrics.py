"""Instrumentación: latencia, tokens, costo y errores de cada llamada (se guarda en PostgreSQL)."""
from __future__ import annotations

import atexit
import logging
import queue
import threading
import time
from typing import Protocol

from app.core.config import Settings
from app.core.logging import current_request_id, current_user_id, log_event

logger = logging.getLogger(__name__)


class MetricsSink(Protocol):
    def add(self, **fields) -> None: ...


class BackgroundMetricsSink:
    """Guarda las métricas en un hilo aparte y en lotes, para no retrasar la respuesta a la persona.

    `add` solo encola (instantáneo); el hilo escribe hasta `batch_size` filas por transacción. Al cerrar la app
    (`close`, también registrado en atexit) se escribe lo pendiente. El usuario y el request_id ya vienen en cada fila:
    se capturan cuando se registra la métrica, no cuando se escribe.
    """

    def __init__(self, repo, batch_size: int = 50, flush_interval: float = 0.5):
        self.repo = repo
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._queue: queue.Queue[dict] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="metrics-writer", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def add(self, **fields) -> None:
        self._queue.put(fields)

    def _run(self) -> None:
        while not (self._stop.is_set() and self._queue.empty()):
            try:
                batch = [self._queue.get(timeout=self.flush_interval)]
            except queue.Empty:
                continue
            while len(batch) < self.batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self.repo.add_many(batch)
            except Exception:  # las métricas nunca deben tumbar la app
                logger.exception("No se pudieron guardar %d métricas", len(batch))
            finally:
                for _ in batch:
                    self._queue.task_done()

    def flush(self, timeout: float = 5.0) -> bool:
        """Espera a que se escriba lo encolado. True si terminó a tiempo."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)
        return not self._queue.unfinished_tasks

    def close(self) -> None:
        if self._stop.is_set():
            return
        self.flush()
        self._stop.set()
        self._thread.join(timeout=2)


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
