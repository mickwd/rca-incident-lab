# Lock de base de datos que bloquea la creación de órdenes

Un job del worker toma locks de fila sobre todos los productos y los deja tomados mientras espera una respuesta externa. Cada `POST /orders` necesita actualizar el stock de un producto, así que queda esperando ese lock. Los requests se acumulan, cada uno retiene una conexión del pool y en pocos segundos el pool de la API se agota: endpoints que no tienen nada que ver con el lock empiezan a fallar también.

**Categoría:** db · **Dificultad:** media/alta · **Herramientas:** `docker compose logs`, `py-spy`, `psql` (`pg_stat_activity`, `pg_locks`, `pg_blocking_pids`), Prometheus, Grafana

## Síntomas

- `POST /orders` tarda 30 s y devuelve `504 Gateway Time-out` (corte de `proxy_read_timeout` en nginx). Ninguna orden nueva se confirma.
- Poco después otros endpoints (`GET /orders/{id}`, `GET /customers/{id}/orders`, `/readyz`) devuelven `503` con `{"detail": "internal error", "error": "TimeoutError"}` tras ~5 s.
- `GET /products/{id}` sigue respondiendo en parte: los productos que están en el cache de Redis no necesitan conexión a la base.
- En Grafana: el panel **Pool de conexiones de la API** muestra `en uso` pegado a la `capacidad` (10) y **requests en curso** subiendo; el p99 de `POST /orders` se va a 30 s; **Conexiones Postgres por estado** muestra una transacción `idle in transaction` cada vez más larga.
- El CPU de la API y de Postgres está bajo. No es un problema de carga: todo está esperando.
- Logs de la API:

```text
2026-05-20 10:14:52,118 ERROR orders.api error no controlado en GET /customers/18231/orders: QueuePool limit of size 5 overflow 5 reached, connection timed out, timeout 5.00 (Background on this error at: https://sqlalche.me/e/20/3o7r)
```

- Logs de Postgres (por `log_lock_waits=on` y `deadlock_timeout=1s`):

```text
LOG:  process 812 still waiting for ShareLock on transaction 1043561 after 1000.142 ms
DETAIL:  Process holding the lock: 640. Wait queue: 812, 815, 817.
CONTEXT:  while updating tuple (1,23) in relation "products"
STATEMENT:  UPDATE products SET stock=(products.stock - $1::INTEGER) WHERE products.id = $2::INTEGER AND products.stock >= $3::INTEGER RETURNING products.price
```

- Logs del worker: el último mensaje es el inicio de la conciliación y después nada.

```text
2026-05-20 10:13:40,502 INFO orders.worker conciliación iniciada, 200 productos bloqueados
```

- nginx:

```text
upstream timed out (110: Connection timed out) while reading response header from upstream, client: 172.20.0.12, server: localhost, request: "POST /orders HTTP/1.1"
```

## Runbook de diagnóstico

1. **Observar el impacto con RED.** ¿Qué rutas fallan, con qué código y desde cuándo?

   ```bash
   docker compose ps
   docker compose logs --since 10m nginx | awk '{print $6, $7, $9}' | sort | uniq -c | sort -rn | head
   ```

   Si todos los contenedores están `Up` pero `POST /orders` da 504 y el resto 503, el problema no es un proceso caído sino algo que hace esperar a la API.

2. **USE sobre la API: ¿saturación de CPU, memoria o de algo más?**

   ```bash
   docker stats --no-stream rca-lab-api-1 rca-lab-postgres-1
   ```

   CPU bajo y memoria normal con latencias de 30 s descartan carga y fugas. Hipótesis que quedan: la API espera a una dependencia (DB lenta, lock, red) o se quedó sin conexiones.

3. **Leer el error concreto en los logs de la API.**

   ```bash
   docker compose logs --since 10m api | grep -E "ERROR|QueuePool" | tail -20
   ```

   `QueuePool limit of size 5 overflow 5 reached` indica que las 10 conexiones están prestadas y nadie las devuelve. Esto es un síntoma, no la causa: hay que averiguar *por qué* las conexiones no vuelven (fuga de sesiones o requests que nunca terminan).

4. **Ver dónde están parados los hilos de la API.**

   ```bash
   docker compose exec api py-spy dump --pid 1
   ```

   Si muchos hilos de `create_order` están dentro de `psycopg` esperando la respuesta del `execute` y otros en `pool/impl.py` (`_do_get`) esperando una conexión, los requests están bloqueados *en la base*. Eso apunta a un lock, no a una fuga de sesiones (en una fuga no habría hilos esperando a Postgres).

5. **Ir a Postgres: quién espera y quién bloquea.**

   ```bash
   docker compose exec postgres psql -U orders -d orders -c "
   SELECT pid, application_name, state, wait_event_type, wait_event,
          now() - xact_start AS tx_age, pg_blocking_pids(pid) AS blocked_by,
          left(query, 60) AS query
   FROM pg_stat_activity
   WHERE datname = 'orders' AND pid <> pg_backend_pid()
   ORDER BY xact_start NULLS LAST;"
   ```

   Esperado: varias sesiones `orders-api` en estado `active` con `wait_event_type = Lock` y `blocked_by = {640}`, y una sesión `orders-worker` en `idle in transaction` con `tx_age` de varios minutos. Una sesión *idle* que bloquea es clave: no está ejecutando nada, solo tiene una transacción abierta.

6. **Confirmar qué tiene tomado el bloqueante.**

   ```bash
   docker compose exec postgres psql -U orders -d orders -c "
   SELECT l.pid, a.application_name, l.locktype, l.relation::regclass, l.mode, l.granted
   FROM pg_locks l JOIN pg_stat_activity a USING (pid)
   WHERE a.datname = 'orders' AND l.locktype IN ('relation', 'transactionid', 'tuple')
   ORDER BY l.granted DESC, l.pid;"
   ```

   El worker tiene `RowShareLock` sobre `products` (lo que toma un `SELECT ... FOR UPDATE`) y su `transactionid` en `ExclusiveLock`; los backends de la API esperan `ShareLock` sobre ese `transactionid` con `granted = f`.

7. **Aislar el origen en el worker.**

   ```bash
   docker compose logs --since 30m worker | tail -20
   docker compose exec redis redis-cli --scan --pattern 'fault:*'
   ```

   El log muestra que la conciliación arrancó y nunca terminó. En el código (`orders/worker.py`, `run_reconciliation`) se ve el patrón: `SELECT ... FOR UPDATE` sobre `products` y, dentro de la misma transacción, la espera de la llamada al ERP. El flag `fault:db-lock` simula que el ERP no responde.

## Consultas PromQL

```promql
# Transacción más larga por estado (postgres-exporter)
pg_stat_activity_max_tx_duration{datname="orders",state="idle in transaction"}

# Conexiones por estado
sum by (state) (pg_stat_activity_count{datname="orders"})

# Saturación del pool de la API y requests acumulados
db_pool_checked_out{job="orders-api"} / db_pool_capacity{job="orders-api"}
http_requests_in_progress{job="orders-api"}

# Latencia p99 de POST /orders
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job="orders-api",method="POST",route="/orders"}[5m])))

# Errores por tipo (TimeoutError = pool agotado)
sum by (type) (rate(app_exceptions_total{job="orders-api"}[5m]))

# Órdenes creadas: cae a cero
rate(orders_created_total{job="orders-api"}[5m])

# El job de conciliación no termina nunca
time() - worker_last_success_timestamp_seconds{job="orders-worker",task="reconciliation"}
```

## Confirmar la causa raíz

La evidencia decisiva es una fila de `pg_stat_activity` con `application_name = 'orders-worker'`, `state = 'idle in transaction'` y `now() - xact_start` en minutos, cuyo `pid` aparece en `pg_blocking_pids()` de todas las sesiones de la API que esperan un `Lock`. Junto con el log del worker (conciliación iniciada sin fin) cierra la cadena: lock del worker → `UPDATE products` bloqueados → conexiones retenidas → pool agotado → 503/504.

Prueba de causalidad: al terminar el backend del worker los `UPDATE` se destraban en el acto, el pool se vacía y `orders_created_total` vuelve a crecer.

## Mitigación inmediata

1. Evitar que el worker vuelva a tomar el lock en el próximo ciclo (si no, el lock vuelve en 2 segundos):

   ```bash
   docker compose exec redis redis-cli DEL fault:db-lock
   ```

2. Terminar la sesión que tiene el lock:

   ```bash
   docker compose exec postgres psql -U orders -d orders -c "
   SELECT pg_terminate_backend(pid) FROM pg_stat_activity
   WHERE application_name = 'orders-worker' AND state = 'idle in transaction';"
   ```

`labctl fix db-lock` hace los dos pasos en ese orden. La API se recupera sola: los requests en espera terminan, las conexiones vuelven al pool. Si el pool quedara en mal estado, `docker compose restart api`.

## Solución definitiva

- **Nunca llamar a sistemas externos con una transacción abierta.** Traer los datos del ERP primero y abrir la transacción solo para escribir, en lotes cortos:

  ```python
  erp_stock = erp_client.fetch_stock()  # fuera de cualquier transacción

  for batch in chunked(erp_stock.items(), 50):
      with Session(engine) as session, session.begin():
          ids = [pid for pid, _ in batch]
          session.scalars(
              select(Product).where(Product.id.in_(ids)).with_for_update(skip_locked=True)
          ).all()
          ...  # aplicar ajustes, la transacción dura milisegundos
  ```

- **Límites del lado de Postgres:** `idle_in_transaction_session_timeout = '60s'` para el rol del worker y `lock_timeout = '3s'` para el rol de la API (`ALTER ROLE ... SET ...`), así un lock olvidado se corta solo y la API falla rápido en lugar de agotar el pool.
- **Timeouts del lado de la API:** `statement_timeout` por conexión (por ejemplo `options=-c statement_timeout=5000` en `connect_args`) alineado con el timeout de nginx.
- **Detección:** alertar por transacciones largas `idle in transaction` y por jobs del worker que no reportan éxito.

## Alertas

Definidas en [`alerts.yml`](alerts.yml):

- **PostgresLongIdleInTransaction** (critical): hay una transacción `idle in transaction` abierta hace más de 60 s. Es la causa, y se detecta antes de que el pool se agote.
- **ApiCreateOrderLatencyHigh** (warning): el p99 de `POST /orders` supera 5 s durante 2 minutos. Es el síntoma que ven los usuarios.
- **WorkerPendingProcessingStalled** (warning): el worker está arriba pero hace más de 2 minutos que no completa `process_pending`. El loop es secuencial, así que una conciliación colgada con el lock tomado frena también el procesamiento de órdenes pendientes. Es una señal independiente de Postgres que apunta al worker.

## Cómo reproducir

```bash
make up-load                   # stack + generador de carga
python -m labctl break db-lock # activa fault:db-lock
```

El worker toma el lock en el siguiente ciclo (≤ 2 s). En ~5 s el pool de la API está lleno, en ~30 s aparecen los primeros 504 y en 1 minuto dispara `PostgresLongIdleInTransaction`. El lock se mantiene hasta 30 minutos si nadie interviene. Para revertir:

```bash
python -m labctl fix db-lock
```
