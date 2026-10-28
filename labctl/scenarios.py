"""Catálogo de escenarios de falla.

Cada escenario sabe romper y arreglar el stack usando el mecanismo más
parecido a como pasaría en la realidad: un flag que activa código con bugs,
un cambio de configuración, un comando SQL o un archivo en disco.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .lab import Lab

LEAK_BYTES_PER_REQUEST = 256 * 1024
# 27 caracteres alfanuméricos + uno inválido: decenas de segundos de CPU con la regex v2
EVIL_REFERENCE = "ORDER2024ACME" + "0" * 14 + "#"
FILLER_PATH = "/data/uploads/tmp/export-debug.bin"


@dataclass(frozen=True)
class Scenario:
    name: str
    category: str
    title: str
    root_cause: str

    @property
    def folder(self) -> Path:
        return Path("scenarios") / self.name

    def break_(self, lab: Lab) -> None:
        raise NotImplementedError

    def fix(self, lab: Lab) -> None:
        raise NotImplementedError


class MemoryLeak(Scenario):
    def break_(self, lab: Lab) -> None:
        lab.set_flag("memory-leak", str(LEAK_BYTES_PER_REQUEST))

    def fix(self, lab: Lab) -> None:
        lab.clear_flag("memory-leak")
        # La memoria retenida solo se libera reiniciando el proceso
        lab.restart("api")


class DbLock(Scenario):
    def break_(self, lab: Lab) -> None:
        lab.set_flag("db-lock")

    def fix(self, lab: Lab) -> None:
        # Primero el flag, si no el worker vuelve a tomar el lock en el próximo ciclo
        lab.clear_flag("db-lock")
        lab.psql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE application_name = 'orders-worker' AND state = 'idle in transaction';"
        )


class PoolExhaustion(Scenario):
    def break_(self, lab: Lab) -> None:
        lab.set_flag("pool-leak")

    def fix(self, lab: Lab) -> None:
        lab.clear_flag("pool-leak")
        # Las sesiones filtradas siguen referenciadas en memoria: hay que reiniciar
        lab.restart("api")


class DiskFull(Scenario):
    def break_(self, lab: Lab) -> None:
        # dd termina con "No space left on device", es lo esperado
        lab.exec(
            "api",
            [
                "sh",
                "-c",
                f"mkdir -p $(dirname {FILLER_PATH}) && "
                f"dd if=/dev/zero of={FILLER_PATH} bs=1M 2>/dev/null; true",
            ],
        )

    def fix(self, lab: Lab) -> None:
        lab.exec("api", ["rm", "-f", FILLER_PATH])


class ExpiredCert(Scenario):
    def break_(self, lab: Lab) -> None:
        lab.certgen("expired")
        lab.reload_nginx()

    def fix(self, lab: Lab) -> None:
        lab.certgen("valid")
        lab.reload_nginx()


class DnsBroken(Scenario):
    @property
    def override(self) -> Path:
        return self.folder / "compose.fault.yml"

    def break_(self, lab: Lab) -> None:
        lab.recreate("api", files=(lab.config.root / self.override,))

    def fix(self, lab: Lab) -> None:
        lab.recreate("api")


class SlowQuery(Scenario):
    def break_(self, lab: Lab) -> None:
        lab.psql(
            "DROP INDEX IF EXISTS ix_orders_customer_id;\nSELECT pg_stat_statements_reset();\n"
        )

    def fix(self, lab: Lab) -> None:
        # CONCURRENTLY no bloquea escrituras. No puede ir dentro de una transacción
        lab.psql(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_orders_customer_id "
            "ON orders (customer_id);\nANALYZE orders;\n"
        )


class CpuSpike(Scenario):
    def break_(self, lab: Lab) -> None:
        lab.set_flag("cpu-spike")
        # El tráfico real del loadgen ya trae entradas de este tipo, estas lo aceleran
        for _ in range(3):
            lab.post_quiet("/orders/validate", {"reference": EVIL_REFERENCE})

    def fix(self, lab: Lab) -> None:
        lab.clear_flag("cpu-spike")
        # Los hilos que están matcheando no se pueden cancelar desde Python
        lab.restart("api")


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (
        MemoryLeak(
            "memory-leak",
            "app",
            "Memoria de la API creciendo hasta OOM kill",
            "Cache local de respuestas en GET /products/{id} con el request id en la clave: "
            "nunca hay hit y nunca se libera. El contenedor supera mem_limit y el kernel lo mata.",
        ),
        DbLock(
            "db-lock",
            "db",
            "POST /orders colgados por un lock",
            "La conciliación del worker toma SELECT ... FOR UPDATE sobre products y espera al ERP "
            "dentro de la transacción. La sesión queda idle in transaction con el lock tomado.",
        ),
        PoolExhaustion(
            "connection-pool-exhaustion",
            "app",
            "Errores QueuePool limit reached",
            "GET /orders/{id} con auditoría diferida abre su propia sesión y nunca la cierra. "
            "Cada request se queda con una conexión del pool hasta agotarlo.",
        ),
        DiskFull(
            "disk-full",
            "infra",
            "Las órdenes fallan al escribir comprobantes",
            "Un archivo de export de debug llenó el volumen /data/uploads. "
            "write() devuelve ENOSPC y POST /orders hace rollback.",
        ),
        ExpiredCert(
            "expired-cert",
            "network",
            "Clientes con errores TLS",
            "nginx sirve un certificado con notAfter en el pasado. "
            "Los clientes que validan la cadena cortan el handshake.",
        ),
        DnsBroken(
            "dns-broken",
            "network",
            "La API no resuelve el host de la base",
            "Cambio de configuración: DB_HOST=orders-db.internal, nombre que no existe "
            "en la red de Docker. psycopg falla con 'could not translate host name'.",
        ),
        SlowQuery(
            "slow-query",
            "db",
            "p99 alto en /customers/{id}/orders",
            "Se borró ix_orders_customer_id. La consulta por cliente pasa de Index Scan a "
            "Seq Scan sobre 1M de filas.",
        ),
        CpuSpike(
            "cpu-spike",
            "app",
            "CPU al 100% y API congelada",
            "Validador v2 de reference con regex vulnerable a backtracking catastrófico (ReDoS). "
            "Ciertas entradas tardan segundos y el GIL congela todo el proceso.",
        ),
    )
}
