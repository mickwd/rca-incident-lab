"""API de órdenes (FastAPI).

Los defectos del laboratorio viven en el código como lo harían en un sistema
real: un cambio con un bug que se activa con un feature flag. Ningún endpoint
"sabe" que está roto, los síntomas aparecen solos en métricas y logs.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import Engine, select, text, update
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session, sessionmaker

from . import flags as flaglib
from .cache import ProductCache, RedisProductCache
from .config import Settings, get_settings
from .db import build_engine, build_session_factory
from .metrics import (
    APP_EXCEPTIONS,
    DEPENDENCY_UP,
    HTTP_IN_PROGRESS,
    HTTP_LATENCY,
    HTTP_REQUESTS,
    ORDERS_CREATED,
    bind_disk_metrics,
    bind_pool_metrics,
)
from .models import Order, Product, order_to_dict, product_to_dict
from .receipts import write_receipt
from .validation import validate_reference

log = logging.getLogger("orders.api")

# Tamaño por defecto (bytes) que retiene cada request con memory-leak activo
DEFAULT_LEAK_BYTES = 256 * 1024


@dataclass
class LabState:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    flags: flaglib.FlagStore
    cache: ProductCache
    # "Memo" local de respuestas. La clave incluye el request id, nunca se repite
    # y nunca se libera: es la fuga de memoria del escenario memory-leak.
    response_memo: dict[tuple[int, str], bytes] = field(default_factory=dict)
    # Sesiones retenidas por la auditoría diferida que nunca se ejecuta
    pending_audit_sessions: list[Session] = field(default_factory=list)


class OrderIn(BaseModel):
    customer_id: int = Field(gt=0)
    product_id: int = Field(gt=0)
    quantity: int = Field(default=1, gt=0, le=100)
    reference: str | None = Field(default=None, max_length=1024)


class ReferenceIn(BaseModel):
    reference: str = Field(max_length=1024)


class FlagIn(BaseModel):
    value: str = "1"


def _state(request: Request) -> LabState:
    return request.app.state.lab


def get_db(request: Request) -> Iterator[Session]:
    session = _state(request).session_factory()
    try:
        yield session
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", "unmatched")


def _error_response(exc: Exception) -> JSONResponse:
    kind = type(exc).__name__
    APP_EXCEPTIONS.labels(type=kind).inc()
    status = 503 if isinstance(exc, sa_exc.TimeoutError | sa_exc.OperationalError) else 500
    return JSONResponse(status_code=status, content={"detail": "internal error", "error": kind})


def create_app(
    settings: Settings | None = None,
    flags: flaglib.FlagStore | None = None,
    cache: ProductCache | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    engine = build_engine(settings)
    state = LabState(
        settings=settings,
        engine=engine,
        session_factory=build_session_factory(engine),
        flags=flags or flaglib.RedisFlagStore.from_url(settings.redis_url, settings.flag_cache_ttl),
        cache=cache or RedisProductCache.from_url(settings.redis_url),
    )

    app = FastAPI(title="orders-api", version="1.4.2")
    app.state.lab = state
    bind_pool_metrics(engine.pool.checkedout, settings.db_pool_size + settings.db_max_overflow)
    bind_disk_metrics(settings.uploads_dir)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        start = time.perf_counter()
        HTTP_IN_PROGRESS.inc()
        try:
            response = await call_next(request)
        except Exception as exc:  # se loguea y se convierte en 5xx
            log.exception("error no controlado en %s %s: %s", request.method, request.url.path, exc)
            response = _error_response(exc)
        finally:
            HTTP_IN_PROGRESS.dec()
        route = _route_label(request)
        if route != "/metrics":
            elapsed = time.perf_counter() - start
            HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
            HTTP_LATENCY.labels(request.method, route).observe(elapsed)
        return response

    @app.get("/")
    def root() -> dict:
        return {"service": "orders-api", "version": app.version}

    @app.get("/healthz")
    def healthz() -> dict:
        # Liveness: no toca dependencias para que un problema de DB no reinicie el proceso
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request) -> JSONResponse:
        st = _state(request)
        checks: dict[str, str] = {}
        try:
            with st.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception as exc:  # noqa: BLE001
            log.error("readyz: postgres no disponible: %s", exc)
            checks["postgres"] = "fail"
        checks["redis"] = "ok" if st.flags.ping() else "fail"
        for dependency, result in checks.items():
            DEPENDENCY_UP.labels(dependency=dependency).set(1 if result == "ok" else 0)
        ready = all(v == "ok" for v in checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "degraded", "checks": checks},
        )

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/products")
    def list_products(db: DbSession, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict]:
        rows = db.scalars(select(Product).order_by(Product.id).limit(limit)).all()
        return [product_to_dict(p) for p in rows]

    @app.get("/products/{product_id}")
    def get_product(product_id: int, request: Request, db: DbSession) -> dict:
        st = _state(request)
        data = st.cache.get(product_id)
        if data is None:
            product = db.get(Product, product_id)
            if product is None:
                raise HTTPException(status_code=404, detail="producto inexistente")
            data = product_to_dict(product)
            st.cache.set(product_id, data)
        if flaglib.is_enabled(st.flags, "memory-leak"):
            _memoize_response(st, request, product_id, data)
        return data

    @app.get("/orders/{order_id}")
    def get_order(order_id: int, request: Request, db: DbSession) -> dict:
        st = _state(request)
        if flaglib.is_enabled(st.flags, "pool-leak"):
            order = _load_order_with_audit(st, order_id)
        else:
            order = db.get(Order, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="orden inexistente")
        return order_to_dict(order)

    @app.get("/customers/{customer_id}/orders")
    def customer_orders(
        customer_id: int,
        db: DbSession,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> list[dict]:
        # Depende de ix_orders_customer_id. Sin el índice es un Seq Scan sobre toda la tabla
        stmt = (
            select(Order)
            .where(Order.customer_id == customer_id)
            .order_by(Order.created_at.desc())
            .limit(limit)
        )
        return [order_to_dict(o) for o in db.scalars(stmt).all()]

    @app.post("/orders", status_code=201)
    def create_order(payload: OrderIn, request: Request, db: DbSession) -> dict:
        st = _state(request)
        if payload.reference is not None and not validate_reference(
            payload.reference, use_v2=flaglib.is_enabled(st.flags, "cpu-spike")
        ):
            raise HTTPException(status_code=422, detail="reference inválida")
        # Descuenta stock de forma atómica. Si otra transacción tiene la fila
        # bloqueada (FOR UPDATE) este UPDATE espera
        price = db.execute(
            update(Product)
            .where(Product.id == payload.product_id, Product.stock >= payload.quantity)
            .values(stock=Product.stock - payload.quantity)
            .returning(Product.price)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        if price is None:
            db.rollback()
            if db.get(Product, payload.product_id) is None:
                raise HTTPException(status_code=404, detail="producto inexistente")
            raise HTTPException(status_code=409, detail="stock insuficiente")
        order = Order(
            customer_id=payload.customer_id,
            product_id=payload.product_id,
            quantity=payload.quantity,
            total=price * payload.quantity,
            status="pending",
            reference=payload.reference,
        )
        db.add(order)
        db.flush()
        try:
            write_receipt(st.settings.uploads_dir, order_to_dict(order))
        except OSError:
            db.rollback()
            raise
        db.commit()
        st.cache.invalidate(payload.product_id)
        ORDERS_CREATED.inc()
        return order_to_dict(order)

    @app.post("/orders/validate")
    def validate(payload: ReferenceIn, request: Request) -> dict:
        st = _state(request)
        use_v2 = flaglib.is_enabled(st.flags, "cpu-spike")
        return {
            "reference": payload.reference,
            "valid": validate_reference(payload.reference, use_v2),
        }

    def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
        if x_admin_token != settings.admin_token:
            raise HTTPException(status_code=401, detail="token inválido")

    @app.get("/admin/faults", dependencies=[Depends(require_admin)])
    def list_faults(request: Request) -> dict:
        return {"known": list(flaglib.KNOWN_FLAGS), "active": _state(request).flags.all()}

    @app.put("/admin/faults/{name}", dependencies=[Depends(require_admin)])
    def set_fault(name: str, payload: FlagIn, request: Request) -> dict:
        if name not in flaglib.KNOWN_FLAGS:
            raise HTTPException(status_code=404, detail="flag desconocido")
        _state(request).flags.set(name, payload.value)
        log.warning("flag %s activado con valor %s", name, payload.value)
        return {"name": name, "value": payload.value}

    @app.delete("/admin/faults/{name}", dependencies=[Depends(require_admin)])
    def delete_fault(name: str, request: Request) -> dict:
        _state(request).flags.delete(name)
        log.warning("flag %s desactivado", name)
        return {"name": name, "deleted": True}

    return app


def _memoize_response(st: LabState, request: Request, product_id: int, data: dict) -> None:
    """Guarda la respuesta "para reusarla". La clave lleva el request id, nunca hay hit."""
    size = flaglib.int_value(st.flags, "memory-leak", DEFAULT_LEAK_BYTES)
    if size <= 1:
        size = DEFAULT_LEAK_BYTES
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    # Se materializa el buffer completo para que las páginas cuenten en el RSS
    st.response_memo[(product_id, request_id)] = repr(data).encode() + b"\x01" * size


def _load_order_with_audit(st: LabState, order_id: int) -> Order | None:
    """Versión con "auditoría diferida": abre su propia sesión y la deja retenida.

    La sesión queda en ``pending_audit_sessions`` con la transacción abierta,
    la conexión nunca vuelve al pool y en Postgres se ve como
    ``idle in transaction`` con application_name=orders-api.
    """
    session = st.session_factory()
    order = session.get(Order, order_id)
    st.pending_audit_sessions.append(session)
    return order
