"""Worker en segundo plano.

Tareas:
- ``process_pending``: pasa órdenes pendientes a ``processed`` en lotes.
- ``run_reconciliation``: conciliación de stock contra el ERP. Bloquea las
  filas de ``products`` con ``SELECT ... FOR UPDATE`` y consulta el ERP
  producto por producto *dentro de la transacción*. Con el flag ``db-lock``
  la llamada al ERP "se cuelga" y el lock queda tomado (sesión
  ``idle in transaction``), así que todo ``POST /orders`` queda esperando.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable

from prometheus_client import Counter, Gauge, start_http_server
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from . import flags as flaglib
from .config import Settings, get_settings
from .db import build_engine
from .models import Order, Product

log = logging.getLogger("orders.worker")

JOBS = Counter("worker_jobs_total", "Ejecuciones de jobs del worker", ["task", "status"])
LAST_SUCCESS = Gauge(
    "worker_last_success_timestamp_seconds", "Último job exitoso (epoch)", ["task"]
)

# Límite de seguridad para no dejar el lock tomado para siempre en el laboratorio
MAX_LOCK_HOLD_SECONDS = 1800


def process_pending(engine: Engine, batch_size: int = 100) -> int:
    with Session(engine) as session, session.begin():
        ids = session.scalars(
            select(Order.id)
            .where(Order.status == "pending")
            .order_by(Order.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        if ids:
            session.execute(
                update(Order)
                .where(Order.id.in_(ids))
                .values(status="processed")
                .execution_options(synchronize_session=False)
            )
    return len(ids)


def run_reconciliation(
    engine: Engine,
    store: flaglib.FlagStore,
    sleep: Callable[[float], None] = time.sleep,
    max_hold: float = MAX_LOCK_HOLD_SECONDS,
) -> float:
    """Ejecuta la conciliación. Devuelve los segundos que estuvo tomado el lock."""
    started = time.monotonic()
    with Session(engine) as session:
        products = session.scalars(select(Product).order_by(Product.id).with_for_update()).all()
        log.info("conciliación iniciada, %d productos bloqueados", len(products))
        held = 0.0
        # Simula la llamada lenta al ERP mientras se mantiene el lock
        while flaglib.is_enabled(store, "db-lock") and held < max_hold:
            sleep(1.0)
            held += 1.0
        session.rollback()
    elapsed = time.monotonic() - started
    log.info("conciliación terminada en %.1fs", elapsed)
    return held


class Worker:
    def __init__(self, settings: Settings, store: flaglib.FlagStore, interval: float = 2.0):
        self.engine = build_engine(settings)
        self.store = store
        self.interval = interval
        self.running = True

    def stop(self, *_args: object) -> None:
        self.running = False

    def tick(self) -> None:
        try:
            processed = process_pending(self.engine)
            JOBS.labels(task="process_pending", status="ok").inc()
            LAST_SUCCESS.labels(task="process_pending").set_to_current_time()
            if processed:
                log.info("órdenes procesadas: %d", processed)
        except Exception as exc:  # noqa: BLE001
            JOBS.labels(task="process_pending", status="error").inc()
            log.error("process_pending falló: %s", exc)
        if flaglib.is_enabled(self.store, "db-lock"):
            try:
                run_reconciliation(self.engine, self.store)
                JOBS.labels(task="reconciliation", status="ok").inc()
                LAST_SUCCESS.labels(task="reconciliation").set_to_current_time()
            except Exception as exc:  # noqa: BLE001
                JOBS.labels(task="reconciliation", status="error").inc()
                log.error("conciliación falló: %s", exc)

    def run(self) -> None:
        while self.running:
            self.tick()
            time.sleep(self.interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    store = flaglib.RedisFlagStore.from_url(settings.redis_url, settings.flag_cache_ttl)
    worker = Worker(settings, store)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    start_http_server(9101)
    log.info("worker iniciado")
    worker.run()


if __name__ == "__main__":
    main()
