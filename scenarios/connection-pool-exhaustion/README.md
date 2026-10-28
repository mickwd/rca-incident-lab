# Agotamiento del pool de conexiones

La API empieza a responder 503 en casi todos los endpoints que tocan la base, pero Postgres está sano y acepta conexiones de otros clientes. El cuello de botella está del lado de la aplicación: el pool de SQLAlchemy se quedó sin conexiones libres porque un camino de código las toma y nunca las devuelve.

**Categoría:** app · **Dificultad:** media · **Herramientas:** Grafana, PromQL, `docker compose logs`, `pg_stat_activity`, `py-spy dump`

## Síntomas

- Los usuarios ven errores intermitentes que en pocos minutos pasan a ser casi totales en `GET /products`, `GET /orders/{id}`, `GET /customers/{id}/orders` y `POST /orders`. Cada request fallido tarda unos 5 segundos antes de devolver error.
- Respuesta típica a través de nginx:

  ```json
  {"detail": "internal error", "error": "TimeoutError"}
  ```

  con status `503`.
- `/healthz` sigue respondiendo 200 al instante (no toca dependencias). `/readyz` tarda unos 5 s y devuelve 503 con `"postgres": "fail"`, porque su chequeo también espera una conexión del pool.
- En Grafana (dashboard **RCA Lab - Overview**):
  - **Error rate 5xx** sube de golpe y el panel de excepciones muestra `exc TimeoutError`.
  - **Pool de conexiones de la API**: `en uso` queda clavado en 10, igual a `capacidad`, y no baja aunque baje el tráfico.
  - **Latencia p50/p95/p99** se aplana cerca de 5 s (el `pool_timeout`).
  - **Conexiones Postgres por estado** muestra conexiones `idle in transaction` cuya transacción más larga crece sin parar.
- El loadgen registra `http_5xx` en casi todos los endpoints y latencias de ~5 s.
- Logs de la API:

  ```text
  2026-03-12 14:21:07,512 ERROR orders.api error no controlado en GET /orders/48211: QueuePool limit of size 5 overflow 5 reached, connection timed out, timeout 5.00 (Background on this error at: https://sqlalche.me/e/20/3o7r)
  Traceback (most recent call last):
    ...
  sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 5 reached, connection timed out, timeout 5.00 (Background on this error at: https://sqlalche.me/e/20/3o7r)
  2026-03-12 14:21:08,004 ERROR orders.api readyz: postgres no disponible: QueuePool limit of size 5 overflow 5 reached, connection timed out, timeout 5.00 (Background on this error at: https://sqlalche.me/e/20/3o7r)
  ```

## Runbook de diagnóstico

La idea es ir de lo que ve el cliente hacia adentro, descartando hipótesis con evidencia. Las hipótesis típicas frente a "la API no llega a la base" son: (a) Postgres caído o saturado, (b) problema de red o DNS, (c) locks en la base, (d) el pool de la aplicación agotado.

1. **Observar desde el cliente (RED).** Confirmar el alcance: qué endpoints fallan, con qué código y cuánto tardan.

   ```bash
   curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' --cacert certs/ca.crt https://localhost:8443/healthz
   curl -sS -w '\n%{http_code} %{time_total}s\n' --cacert certs/ca.crt https://localhost:8443/readyz
   curl -sS -w '\n%{http_code} %{time_total}s\n' --cacert certs/ca.crt https://localhost:8443/products/1
   python -m labctl status
   ```

   `/healthz` rápido y `/readyz` lento con 503 indica que el proceso vive pero no consigue hablar con una dependencia. El tiempo de ~5 s es una pista fuerte: coincide con `DB_POOL_TIMEOUT`, no con un timeout de red ni con el `proxy_read_timeout` de nginx (30 s).

2. **Leer los logs de la API.** Buscar el tipo de excepción dominante.

   ```bash
   docker compose logs --since 10m api | grep -E 'ERROR|Timeout|QueuePool' | tail -n 20
   docker compose logs --since 10m api | grep -c 'QueuePool limit'
   ```

   `QueuePool limit of size 5 overflow 5 reached` dice explícitamente que el pool *de la aplicación* está lleno. No es un error de Postgres (sería `OperationalError` con `connection refused`, `too many clients` o `could not translate host name`).

3. **Descartar Postgres caído o saturado (USE sobre la base).**

   ```bash
   docker compose ps postgres
   docker compose exec postgres pg_isready -h 127.0.0.1
   docker compose exec postgres psql -U orders -d orders -c "SELECT count(*) AS total, current_setting('max_connections') AS max FROM pg_stat_activity;"
   ```

   Si `psql` conecta al instante y el total de conexiones está lejos de `max_connections` (100), la base no es el cuello de botella. La hipótesis (a) queda descartada.

4. **Ver quién tiene las conexiones.** Es el paso que aísla el problema.

   ```bash
   docker compose exec postgres psql -U orders -d orders -c "
   SELECT pid, application_name, state,
          now() - xact_start   AS tx_age,
          now() - state_change AS idle_for,
          wait_event_type, left(query, 60) AS last_query
   FROM pg_stat_activity
   WHERE datname = 'orders' AND pid <> pg_backend_pid()
   ORDER BY xact_start NULLS LAST;"
   ```

   Lo que vas a ver: ~10 filas con `application_name = orders-api`, `state = idle in transaction`, `last_query` = `SELECT orders.id, orders.customer_id, ...` y `idle_for` creciendo. Nadie espera locks (`wait_event_type` vacío, no hay `Lock`).

   Contraste con `db-lock`: ahí la sesión ociosa en transacción es de `orders-worker` y aparecen sesiones de `orders-api` *esperando* (`wait_event_type = Lock`). Acá las conexiones de la API no esperan nada, simplemente nadie las devuelve.

5. **Descartar locks explícitamente.**

   ```bash
   docker compose exec postgres psql -U orders -d orders -c "
   SELECT count(*) FILTER (WHERE NOT granted) AS waiting, count(*) AS total FROM pg_locks;"
   ```

   `waiting = 0` descarta la hipótesis (c).

6. **Confirmar la saturación del pool con métricas.**

   ```bash
   curl -sS http://localhost:9090/api/v1/query --data-urlencode 'query=db_pool_checked_out{job="orders-api"}'
   docker compose exec api curl -sS http://127.0.0.1:8000/metrics | grep -E '^db_pool_(checked_out|capacity)'
   ```

   `db_pool_checked_out` igual a `db_pool_capacity` (10) de forma sostenida, incluso con poco tráfico, es una fuga: si fuera falta de capacidad bajaría cuando baja la carga.

7. **Ver qué hacen los hilos del proceso.**

   ```bash
   docker compose exec api py-spy dump --pid 1
   ```

   La mayoría de los hilos del threadpool aparecen en `_do_get` de `sqlalchemy/pool/impl.py` y `wait` de `threading.py`: esperan una conexión que no llega. No hay hilos ejecutando queries.

8. **Encontrar el camino de código que filtra.** Correlacionar el inicio de la saturación con el tráfico por endpoint y con cambios recientes.

   ```bash
   docker compose exec redis redis-cli --scan --pattern 'fault:*'
   ```

   En el laboratorio el cambio "desplegado" es el flag `fault:pool-leak`, que hace que `GET /orders/{id}` use `_load_order_with_audit`: abre su propia sesión con `session_factory()`, ejecuta `session.get(Order, ...)` (autobegin abre transacción) y guarda la sesión en `pending_audit_sessions` sin cerrarla nunca. En un incidente real este paso es revisar el diff del último deploy buscando sesiones o conexiones creadas fuera del `Depends(get_db)`.

## Consultas PromQL

```promql
# Saturación del pool (1 = lleno)
db_pool_checked_out{job="orders-api"} / db_pool_capacity{job="orders-api"}

# Excepciones por tipo: TimeoutError domina
sum by (type) (rate(app_exceptions_total{job="orders-api"}[5m]))

# Error rate 5xx por endpoint
sum by (route) (rate(http_requests_total{job="orders-api",status=~"5.."}[5m]))
  / sum by (route) (rate(http_requests_total{job="orders-api"}[5m]))

# Latencia p99 aplanada en ~5s (pool_timeout)
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job="orders-api"}[5m])))

# Conexiones idle in transaction y antigüedad de la transacción más vieja
pg_stat_activity_count{datname="orders",state="idle in transaction"}
pg_stat_activity_max_tx_duration{datname="orders",state="idle in transaction"}

# Tráfico de GET /orders/{id}: cuántos requests hacen falta para llenar el pool
sum(rate(http_requests_total{job="orders-api",route="/orders/{order_id}"}[5m]))
```

## Confirmar la causa raíz

La causa está confirmada cuando se cumplen las tres cosas a la vez:

1. `db_pool_checked_out == db_pool_capacity` sostenido y sin relación con el volumen de tráfico.
2. En `pg_stat_activity` las conexiones retenidas son de `application_name = 'orders-api'`, en `idle in transaction`, sin esperas de locks, con la última query sobre `orders`.
3. Reproducción controlada: con el stack sano y sin carga, unos pocos requests a `GET /orders/{id}` hacen subir `db_pool_checked_out` de a uno y **no vuelve a bajar**. Con la versión corregida vuelve a 0.

   ```bash
   for i in 1 2 3; do curl -s -o /dev/null --cacert certs/ca.crt https://localhost:8443/orders/$i; done
   docker compose exec api curl -sS http://127.0.0.1:8000/metrics | grep '^db_pool_checked_out'
   ```

## Mitigación inmediata

1. Desactivar el camino que filtra (en el lab, el flag) para que el problema no vuelva a crecer:

   ```bash
   docker compose exec redis redis-cli DEL fault:pool-leak
   ```

2. Reiniciar la API. Las sesiones filtradas siguen referenciadas en memoria del proceso, así que no se liberan solas:

   ```bash
   docker compose restart api
   ```

   `labctl fix connection-pool-exhaustion` hace los dos pasos.

3. Alternativa si no se puede reiniciar ya: terminar las conexiones colgadas desde Postgres. El pool las va a descartar en el próximo `pre_ping`, pero las sesiones siguen referenciadas, así que es solo un parche:

   ```sql
   SELECT pg_terminate_backend(pid) FROM pg_stat_activity
   WHERE application_name = 'orders-api' AND state = 'idle in transaction'
     AND now() - state_change > interval '1 minute';
   ```

Verificar: `db_pool_checked_out` vuelve cerca de 0, `/readyz` 200 y el error rate a cero.

## Solución definitiva

- **Toda sesión se crea en un context manager o en la dependency con `finally`.** Nada de `session_factory()` suelto:

  ```python
  def _load_order_with_audit(st: LabState, order_id: int) -> Order | None:
      with st.session_factory() as session:
          order = session.get(Order, order_id)
          audit_queue.put({"order_id": order_id})  # la auditoría no retiene la sesión
          return order
  ```

  O directamente reusar la sesión de `Depends(get_db)`, que ya cierra en `finally`.
- **No retener objetos ligados a una sesión** (sesiones, conexiones, ORM objects lazy) en estructuras globales. Si hace falta procesar después, encolar datos planos.
- **Test de regresión**: después de N requests a cada endpoint, `engine.pool.checkedout()` debe volver a 0. El repo ya tiene un test así en `tests/`.
- **Red de seguridad en la base**: `idle_in_transaction_session_timeout = '60s'` para el rol de la API, así una fuga no puede retener conexiones para siempre.
- **Dimensionamiento**: `pool_size + max_overflow` por réplica, multiplicado por la cantidad de réplicas y workers, tiene que quedar por debajo de `max_connections` con margen. `pool_timeout` corto para fallar rápido en lugar de apilar requests.
- **Observabilidad**: alerta sobre saturación del pool (ver abajo) y panel permanente en el dashboard.

## Alertas

Definidas en `alerts.yml` de esta carpeta:

- **DbPoolSaturated** (warning): `db_pool_checked_out / db_pool_capacity >= 0.9` durante 2 minutos. Detecta el problema antes de que haya errores, cuando el pool se está llenando. Un pool sano bajo esta carga ronda el 10-30%.
- **DbPoolTimeouts** (critical): hay `TimeoutError` en `app_exceptions_total`. A esta altura los usuarios ya ven 503.
- **ApiIdleInTransactionConnections** (warning): conexiones `idle in transaction` cuya transacción supera los 2 minutos. Sirve también para `db-lock`; las labels y el `application_name` en `pg_stat_activity` separan un caso del otro.

## Cómo reproducir

```bash
make up-load                                      # stack + generador de carga
python -m labctl break connection-pool-exhaustion
python -m labctl status
# ... diagnosticar con el runbook ...
python -m labctl fix connection-pool-exhaustion
```

Tiempos esperados con el loadgen por defecto (20 rps, ~8% a `GET /orders/{id}`): el pool se llena en menos de 10 segundos. La alerta `DbPoolTimeouts` dispara en ~1 minuto y `DbPoolSaturated` en ~2 minutos. Después del fix, la API vuelve a estar sana en lo que tarda el reinicio (~10 s).
