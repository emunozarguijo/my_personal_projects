import threading
from types import SimpleNamespace

from app.core.logging import request_context
from app.services.metrics import BackgroundMetricsSink, MetricsService


class Repo:
    def __init__(self, fail_first: int = 0):
        self.batches: list[list[dict]] = []
        self.fail_first = fail_first
        self.writer_threads: set[str] = set()

    def add_many(self, rows):
        self.writer_threads.add(threading.current_thread().name)
        if self.fail_first:
            self.fail_first -= 1
            raise RuntimeError("BD caída")
        self.batches.append(list(rows))


def test_writes_in_batches_off_the_request_thread():
    repo = Repo()
    sink = BackgroundMetricsSink(repo, batch_size=50, flush_interval=0.05)
    for i in range(120):
        sink.add(n=i)
    assert sink.flush(timeout=5)
    rows = [r["n"] for b in repo.batches for r in b]
    assert sorted(rows) == list(range(120))
    assert all(len(b) <= 50 for b in repo.batches) and len(repo.batches) < 120   # agrupadas, no una por una
    assert repo.writer_threads == {"metrics-writer"}                              # no en el hilo de la petición
    sink.close()


def test_database_errors_do_not_stop_the_writer():
    repo = Repo(fail_first=1)
    sink = BackgroundMetricsSink(repo, batch_size=1, flush_interval=0.05)
    sink.add(n=1)
    sink.add(n=2)
    assert sink.flush(timeout=5)
    assert [r["n"] for b in repo.batches for r in b] == [2]   # la primera se perdió (y se registró en el log); la app sigue
    sink.close()


def test_close_writes_pending_and_is_idempotent():
    repo = Repo()
    sink = BackgroundMetricsSink(repo, flush_interval=5)    # intervalo largo: lo pendiente se escribe al cerrar
    sink.add(n=1)
    sink.close()
    sink.close()
    assert [r["n"] for b in repo.batches for r in b] == [1]


def test_user_and_request_id_are_captured_when_recorded():
    repo = Repo()
    sink = BackgroundMetricsSink(repo, flush_interval=0.05)
    metrics = MetricsService(sink, SimpleNamespace(price_input_per_mtok=2, price_output_per_mtok=10))
    with request_context("req-123", user_id=7):
        metrics.record(operation="chat", provider="p", model="m", latency_ms=1, input_tokens=1000, output_tokens=100)
    assert sink.flush(timeout=5)
    row = repo.batches[0][0]
    assert (row["user_id"], row["request_id"], row["cost_usd"]) == (7, "req-123", (1000 * 2 + 100 * 10) / 1e6)
    sink.close()
