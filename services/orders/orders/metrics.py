"""Métricas Prometheus del servicio (método RED + saturación de recursos)."""

from __future__ import annotations

import shutil
from collections.abc import Callable

from prometheus_client import Counter, Gauge, Histogram

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "Requests HTTP atendidos",
    ["method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Latencia de requests HTTP",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)
HTTP_IN_PROGRESS = Gauge("http_requests_in_progress", "Requests en curso")
APP_EXCEPTIONS = Counter(
    "app_exceptions_total",
    "Excepciones no controladas por tipo",
    ["type"],
)
ORDERS_CREATED = Counter("orders_created_total", "Órdenes creadas")
CACHE_REQUESTS = Counter("cache_requests_total", "Lecturas de cache", ["result"])
DEPENDENCY_UP = Gauge(
    "app_dependency_up",
    "Resultado del último chequeo de dependencias (1 ok, 0 falla)",
    ["dependency"],
)
DB_POOL_CHECKED_OUT = Gauge("db_pool_checked_out", "Conexiones del pool en uso")
DB_POOL_CAPACITY = Gauge("db_pool_capacity", "Conexiones máximas del pool (size + overflow)")
UPLOADS_FREE = Gauge("uploads_disk_free_bytes", "Espacio libre en el volumen de uploads")
UPLOADS_TOTAL = Gauge("uploads_disk_total_bytes", "Tamaño del volumen de uploads")


def bind_pool_metrics(checked_out: Callable[[], int], capacity: int) -> None:
    DB_POOL_CHECKED_OUT.set_function(checked_out)
    DB_POOL_CAPACITY.set(capacity)


def bind_disk_metrics(path: str) -> None:
    def _free() -> float:
        try:
            return float(shutil.disk_usage(path).free)
        except OSError:
            return float("nan")

    def _total() -> float:
        try:
            return float(shutil.disk_usage(path).total)
        except OSError:
            return float("nan")

    UPLOADS_FREE.set_function(_free)
    UPLOADS_TOTAL.set_function(_total)
