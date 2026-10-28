"""Comportamiento normal de la API (sin fallas activas)."""

from __future__ import annotations

from pathlib import Path


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_ok(client):
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["checks"] == {"postgres": "ok", "redis": "ok"}


def test_list_products(client):
    resp = client.get("/products?limit=2")
    assert resp.status_code == 200
    assert [p["id"] for p in resp.json()] == [1, 2]


def test_get_product_uses_cache(client, cache):
    assert client.get("/products/2").status_code == 200
    assert 2 in cache.data
    cache.data[2] = {**cache.data[2], "name": "desde cache"}
    assert client.get("/products/2").json()["name"] == "desde cache"


def test_get_product_not_found(client):
    assert client.get("/products/999").status_code == 404


def test_customer_orders(client):
    resp = client.get("/customers/7/orders?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert all(o["customer_id"] == 7 for o in body)


def test_create_order_decrements_stock_and_writes_receipt(client, settings, cache):
    cache.data[1] = {"id": 1, "stale": True}
    resp = client.post(
        "/orders",
        json={"customer_id": 9, "product_id": 1, "quantity": 2, "reference": "PO-1234-A"},
    )
    assert resp.status_code == 201
    order = resp.json()
    assert order["total"] == 21.0
    assert order["status"] == "pending"
    assert 1 not in cache.data, "la cache del producto tiene que invalidarse"
    assert client.get("/products/1").json()["stock"] == 98
    receipts = list((Path(settings.uploads_dir) / "receipts").glob("*.jsonl"))
    assert len(receipts) == 1
    assert f'"id": {order["id"]}' in receipts[0].read_text(encoding="utf-8")


def test_create_order_insufficient_stock(client):
    resp = client.post("/orders", json={"customer_id": 1, "product_id": 1, "quantity": 100})
    assert resp.status_code == 201
    resp = client.post("/orders", json={"customer_id": 1, "product_id": 1, "quantity": 1})
    assert resp.status_code == 409


def test_create_order_unknown_product(client):
    resp = client.post("/orders", json={"customer_id": 1, "product_id": 77, "quantity": 1})
    assert resp.status_code == 404


def test_create_order_rejects_invalid_reference(client):
    resp = client.post("/orders", json={"customer_id": 1, "product_id": 1, "reference": "PO 12##"})
    assert resp.status_code == 422


def test_get_order(client):
    assert client.get("/orders/1").json()["reference"] == "PO-0"
    assert client.get("/orders/12345").status_code == 404


def test_validate_endpoint(client):
    assert client.post("/orders/validate", json={"reference": "PO-1"}).json()["valid"] is True
    assert client.post("/orders/validate", json={"reference": "PO-1#"}).json()["valid"] is False


def test_metrics_exposes_red_and_pool(client):
    client.get("/products/1")
    text = client.get("/metrics").text
    assert 'http_requests_total{method="GET",route="/products/{product_id}",status="200"}' in text
    assert "http_request_duration_seconds_bucket" in text
    assert "db_pool_checked_out" in text
    assert "db_pool_capacity" in text
    assert "uploads_disk_free_bytes" in text


def test_pool_returns_connections_after_requests(client, app):
    for _ in range(5):
        client.get("/orders/1")
        client.get("/customers/7/orders")
    assert app.state.lab.engine.pool.checkedout() == 0


def test_admin_requires_token(client):
    assert client.get("/admin/faults").status_code == 401
    headers = {"X-Admin-Token": "test-token"}
    assert client.put("/admin/faults/cpu-spike", json={"value": "1"}, headers=headers).is_success
    assert client.get("/admin/faults", headers=headers).json()["active"] == {"cpu-spike": "1"}
    assert client.put("/admin/faults/nope", json={}, headers=headers).status_code == 404
    assert client.delete("/admin/faults/cpu-spike", headers=headers).is_success
    assert client.get("/admin/faults", headers=headers).json()["active"] == {}
