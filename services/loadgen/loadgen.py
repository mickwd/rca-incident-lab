"""Generador de carga asíncrono (asyncio + httpx).

Simula clientes reales contra nginx y expone métricas del lado cliente en
:9102. Sirven para ver lo que el servidor no ve (errores TLS, timeouts,
conexiones rechazadas).

Una fracción chica de los requests manda referencias "raras" (código largo
con un caracter inválido al final) como haría un usuario que pega texto con
basura. Con el validador correcto se rechazan en microsegundos.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import ssl
import time
from dataclasses import dataclass

import httpx
from prometheus_client import Counter, Histogram, start_http_server

REQUESTS = Counter(
    "loadgen_requests_total", "Requests enviados por el generador", ["endpoint", "outcome"]
)
LATENCY = Histogram(
    "loadgen_request_duration_seconds",
    "Latencia vista por el cliente",
    ["endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

CUSTOMERS = 50_000
PRODUCTS = 200
SEEDED_ORDERS = 1_000_000


@dataclass(frozen=True)
class Call:
    endpoint: str
    method: str
    path: str
    json: dict | None = None


def odd_reference(rng: random.Random) -> str:
    # 26 caracteres alfanuméricos + uno inválido
    body = "".join(rng.choices("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789", k=26))
    return body + rng.choice("#!?")


def pick_call(rng: random.Random) -> Call:
    roll = rng.random()
    if roll < 0.40:
        cid = rng.randint(1, CUSTOMERS)
        return Call("customer_orders", "GET", f"/customers/{cid}/orders?limit=20")
    if roll < 0.65:
        return Call("get_product", "GET", f"/products/{rng.randint(1, PRODUCTS)}")
    if roll < 0.72:
        return Call("list_products", "GET", "/products?limit=20")
    if roll < 0.80:
        return Call("get_order", "GET", f"/orders/{rng.randint(1, SEEDED_ORDERS)}")
    if roll < 0.99:
        payload = {
            "customer_id": rng.randint(1, CUSTOMERS),
            "product_id": rng.randint(1, PRODUCTS),
            "quantity": rng.randint(1, 3),
            "reference": f"PO-{rng.randint(1000, 9999)}-{rng.choice(['A', 'B', 'C'])}",
        }
        return Call("create_order", "POST", "/orders", payload)
    return Call("validate", "POST", "/orders/validate", {"reference": odd_reference(rng)})


def classify(exc: Exception) -> str:
    text = repr(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if "CERTIFICATE_VERIFY_FAILED" in text or isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, httpx.ConnectError):
        return "connect_error"
    return "other_error"


def outcome_for_status(status: int) -> str:
    if status < 400:
        return "ok"
    if status < 500:
        return "http_4xx"
    return "http_5xx"


async def worker(client: httpx.AsyncClient, rng: random.Random, interval: float) -> None:
    while True:
        call = pick_call(rng)
        start = time.perf_counter()
        try:
            resp = await client.request(call.method, call.path, json=call.json)
            outcome = outcome_for_status(resp.status_code)
        except Exception as exc:  # noqa: BLE001
            outcome = classify(exc)
        elapsed = time.perf_counter() - start
        REQUESTS.labels(call.endpoint, outcome).inc()
        LATENCY.labels(call.endpoint).observe(elapsed)
        await asyncio.sleep(max(0.0, interval - elapsed) * rng.uniform(0.8, 1.2))


async def run(url: str, rps: float, concurrency: int, cacert: str | None, timeout: float) -> None:
    verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=cacert) if cacert else True
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(
        base_url=url, verify=verify, timeout=timeout, limits=limits
    ) as client:
        interval = concurrency / rps
        tasks = [
            asyncio.create_task(worker(client, random.Random(i), interval))
            for i in range(concurrency)
        ]
        await asyncio.gather(*tasks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generador de carga del laboratorio")
    parser.add_argument("--url", default=os.getenv("LOADGEN_URL", "https://localhost:8443"))
    parser.add_argument("--rps", type=float, default=float(os.getenv("LOADGEN_RPS", "20")))
    parser.add_argument(
        "--concurrency", type=int, default=int(os.getenv("LOADGEN_CONCURRENCY", "10"))
    )
    parser.add_argument("--cacert", default=os.getenv("LOADGEN_CACERT", "certs/ca.crt"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("LOADGEN_TIMEOUT", "10")))
    parser.add_argument(
        "--metrics-port", type=int, default=int(os.getenv("LOADGEN_METRICS_PORT", "9102"))
    )
    args = parser.parse_args()
    if args.metrics_port:
        start_http_server(args.metrics_port)
    print(f"loadgen: {args.rps} rps contra {args.url} con {args.concurrency} workers", flush=True)
    asyncio.run(run(args.url, args.rps, args.concurrency, args.cacert, args.timeout))


if __name__ == "__main__":
    main()
