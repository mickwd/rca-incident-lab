"""Fixtures compartidas.

La API se prueba contra SQLite en archivo (no en memoria) para que el engine
use un QueuePool real y se pueda reproducir el agotamiento de conexiones.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from orders.api import create_app
from orders.cache import InMemoryProductCache
from orders.config import Settings
from orders.flags import InMemoryFlagStore
from orders.models import Base, Order, Product

ROOT = Path(__file__).resolve().parent.parent


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "database_url": f"sqlite:///{tmp_path / 'orders.db'}",
        "redis_url": "redis://unused:6379/0",
        "db_pool_size": 2,
        "db_max_overflow": 0,
        "db_pool_timeout": 0.3,
        "uploads_dir": str(tmp_path / "uploads"),
        "admin_token": "test-token",
    }
    values.update(overrides)
    return Settings(**values)


def seed(app) -> None:
    engine = app.state.lab.engine
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all(
            [
                Product(
                    id=i,
                    sku=f"SKU-{i:04d}",
                    name=f"Producto {i}",
                    price=Decimal("10.50"),
                    stock=100,
                )
                for i in range(1, 4)
            ]
        )
        s.add_all(
            [
                Order(
                    customer_id=7,
                    product_id=1,
                    quantity=1,
                    total=Decimal("10.50"),
                    status="processed",
                    reference=f"PO-{i}",
                )
                for i in range(3)
            ]
        )
        s.commit()


@pytest.fixture
def flags() -> InMemoryFlagStore:
    return InMemoryFlagStore()


@pytest.fixture
def cache() -> InMemoryProductCache:
    return InMemoryProductCache()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def app(settings, flags, cache):
    application = create_app(settings=settings, flags=flags, cache=cache)
    seed(application)
    yield application
    lab = application.state.lab
    for leaked in lab.pending_audit_sessions:
        leaked.close()
    lab.engine.dispose()


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
