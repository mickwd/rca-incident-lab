"""Cada falla se reproduce de verdad en la API o el worker (sin Docker)."""

from __future__ import annotations

import errno
import time

import pytest
from conftest import make_settings
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from orders import validation
from orders.api import create_app
from orders.models import Order
from orders.worker import process_pending, run_reconciliation


def test_memory_leak_retains_one_buffer_per_request(client, app, flags):
    memo = app.state.lab.response_memo
    for _ in range(3):
        client.get("/products/1")
    assert memo == {}, "sin el flag no se retiene nada"

    flags.set("memory-leak", "4096")
    for i in range(5):
        client.get("/products/1", headers={"X-Request-ID": f"req-{i}"})
    assert len(memo) == 5, "cada request agrega una entrada que nunca se reutiliza"
    assert all(len(v) >= 4096 for v in memo.values())


def test_pool_leak_exhausts_queue_pool(client, app, flags):
    flags.set("pool-leak", "1")
    # pool_size=2, max_overflow=0: las dos primeras lecturas se quedan con las conexiones
    assert client.get("/orders/1").status_code == 200
    assert client.get("/orders/2").status_code == 200
    assert app.state.lab.engine.pool.checkedout() == 2

    resp = client.get("/customers/7/orders")
    assert resp.status_code == 503
    assert resp.json()["error"] == "TimeoutError"
    metrics = client.get("/metrics").text
    assert 'app_exceptions_total{type="TimeoutError"}' in metrics


def test_pool_leak_off_does_not_leak(client, app):
    for _ in range(5):
        assert client.get("/orders/1").status_code == 200
    assert app.state.lab.pending_audit_sessions == []


def test_disk_full_fails_order_and_rolls_back(client, monkeypatch):
    def no_space(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("orders.api.write_receipt", no_space)
    before = client.get("/products/1").json()["stock"]
    resp = client.post("/orders", json={"customer_id": 3, "product_id": 1, "quantity": 1})
    assert resp.status_code == 500
    assert resp.json()["error"] == "OSError"
    # El descuento de stock y la orden se revierten
    assert client.get("/products/1").json()["stock"] == before
    assert client.get("/customers/3/orders").json() == []


def test_dns_broken_makes_readyz_fail(tmp_path, flags, cache):
    # Un directorio inexistente provoca el mismo tipo de error que un host que no resuelve
    bad = make_settings(tmp_path, database_url=f"sqlite:///{tmp_path / 'nope' / 'x.db'}")
    with TestClient(create_app(bad, flags, cache), raise_server_exceptions=False) as c:
        resp = c.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["checks"]["postgres"] == "fail"
        assert c.get("/healthz").status_code == 200, "liveness no depende de la base"
        assert c.get("/products").status_code == 503


def test_cpu_spike_flag_switches_to_vulnerable_validator(client, flags, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "orders.api.validate_reference",
        lambda value, use_v2: calls.append("v2" if use_v2 else "v1") or True,
    )
    client.post("/orders/validate", json={"reference": "abc"})
    flags.set("cpu-spike", "1")
    client.post("/orders/validate", json={"reference": "abc"})
    assert calls == ["v1", "v2"]


def _time_match(fn, n: int) -> float:
    start = time.perf_counter()
    fn("A" * n + "#")
    return time.perf_counter() - start


def test_vulnerable_regex_grows_exponentially():
    small = min(_time_match(validation.validate_reference_v2, 12) for _ in range(3))
    big = min(_time_match(validation.validate_reference_v2, 18) for _ in range(3))
    # 6 caracteres más multiplican el tiempo ~64 veces. Se exige x8 para evitar flakiness
    assert big > small * 8


def test_safe_regex_is_linear_and_bounded():
    assert _time_match(validation.validate_reference_v1, 60) < 0.01
    assert validation.validate_reference_v1("A" * 65) is False
    assert validation.validate_reference_v1("PO-2024_A B") is True
    assert validation.validate_reference_v1("PO--1") is False


@pytest.mark.parametrize("value", ["PO-1", "ABC_123", "a b c"])
def test_both_validators_agree_on_normal_input(value):
    assert validation.validate_reference_v1(value) == validation.validate_reference_v2(value)


def test_worker_process_pending(client, app):
    client.post("/orders", json={"customer_id": 4, "product_id": 2, "quantity": 1})
    assert process_pending(app.state.lab.engine) == 1
    assert process_pending(app.state.lab.engine) == 0
    with Session(app.state.lab.engine) as s:
        statuses = set(s.scalars(select(Order.status)).all())
    assert statuses == {"processed"}


def test_reconciliation_holds_lock_while_flag_is_on(app, flags):
    flags.set("db-lock", "1")
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            flags.delete("db-lock")

    held = run_reconciliation(app.state.lab.engine, flags, sleep=fake_sleep)
    assert held == 3.0
    assert app.state.lab.engine.pool.checkedout() == 0


def test_reconciliation_has_safety_limit(app, flags):
    flags.set("db-lock", "1")
    held = run_reconciliation(app.state.lab.engine, flags, sleep=lambda _s: None, max_hold=5)
    assert held == 5.0
