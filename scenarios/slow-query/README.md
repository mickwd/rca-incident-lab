# Consulta lenta por índice faltante

La consulta de órdenes por cliente pasa de leer ~20 filas por índice a recorrer la tabla `orders` completa (1.000.000 de filas). El p99 del endpoint se dispara y, bajo carga, Postgres se satura y arrastra al resto de la API.

**Categoría:** db · **Dificultad:** media · **Herramientas:** Grafana, Prometheus, `docker compose logs`, `psql`, `pg_stat_statements`, `EXPLAIN (ANALYZE, BUFFERS)`, `pg_stat_user_tables`

## Síntomas

- Lo que ve el usuario: el historial de órdenes (`GET /customers/{customer_id}/orders`) tarda varios cientos de ms y, con carga, más de un segundo. Algunos requests de otros endpoints también se ponen lentos.
- Dashboard *RCA Lab - Overview*:
  - "p99 por endpoint" muestra solo la ruta `/customers/{customer_id}/orders` subiendo de ~10 ms a 300 ms-2 s.
  - "Pool de conexiones de la API" con más conexiones en uso que lo habitual (cada request retiene la conexión más tiempo).
  - "Postgres: lecturas secuenciales vs índice": `seq_tup_read` de `orders` salta a millones de filas por segundo e `idx_tup_fetch` cae.
  - CPU del contenedor `rca-lab-postgres-1` alta en "CPU".
- Error rate 5xx en general bajo. Si la saturación sigue, pueden aparecer `TimeoutError` del pool (503) o 504 de nginx.
- Logs de Postgres con `duration: 2xx.xxx ms  execute <unnamed>: SELECT orders.id ... WHERE orders.customer_id = $1 ...` (umbral `log_min_duration_statement=200`).

## Runbook de diagnóstico

1. **Observar (RED).** Confirmar que la latencia es real y acotarla. En Grafana (http://localhost:3000) panel "p99 por endpoint" o en Prometheus (http://localhost:9090):

   ```bash
   curl -s 'http://localhost:9090/api/v1/query' --data-urlencode \
     'query=histogram_quantile(0.99, sum by (le, route) (rate(http_request_duration_seconds_bucket{job="orders-api"}[5m])))'
   ```

   Si una sola ruta sube y el resto acompaña poco, el problema es específico de esa ruta o de lo que ella consulta.

2. **Medir desde el cliente.** Comparar la ruta sospechosa con otra que no toca la misma tabla:

   ```bash
   for i in 1 2 3 4 5; do
     curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' --cacert certs/ca.crt \
       "https://localhost:8443/customers/$((RANDOM % 50000 + 1))/orders?limit=20"
   done
   curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' --cacert certs/ca.crt https://localhost:8443/products/1
   ```

3. **Recursos (USE).** ¿Quién consume CPU? Si es Postgres y no la API, la hipótesis apunta a la base:

   ```bash
   docker stats --no-stream
   ```

4. **Hipótesis: consulta lenta en la base.** Buscar en los logs de Postgres las sentencias que superan 200 ms:

   ```bash
   docker compose logs --since 10m postgres | grep 'duration:' | tail -20
   ```

5. **Top de consultas con `pg_stat_statements`.**

   ```bash
   docker compose exec postgres psql -U orders -d orders -c "
   SELECT calls, round(total_exec_time) AS total_ms, round(mean_exec_time::numeric, 1) AS mean_ms,
          rows, left(query, 120) AS query
   FROM pg_stat_statements
   ORDER BY total_exec_time DESC
   LIMIT 10;"
   ```

   La consulta `... FROM orders WHERE orders.customer_id = $1 ORDER BY orders.created_at DESC LIMIT $2` aparece primera, con `mean_ms` en cientos.

6. **Probar la hipótesis con el plan real.**

   ```bash
   docker compose exec postgres psql -U orders -d orders -c "
   EXPLAIN (ANALYZE, BUFFERS)
   SELECT * FROM orders WHERE customer_id = 4242 ORDER BY created_at DESC LIMIT 20;"
   ```

   Esperado sano: `Bitmap Heap Scan` / `Index Scan using ix_orders_customer_id`, decenas de buffers, pocos ms.
   Con la falla: `Parallel Seq Scan on orders`, `Rows Removed by Filter: ~333000` por worker, `Sort Method: top-N heapsort`, miles de buffers leídos y 100-300 ms.

7. **Aislar: ¿por qué el planner no usa el índice?** Ver índices de la tabla y estadísticas de acceso:

   ```bash
   docker compose exec postgres psql -U orders -d orders -c '\d orders'
   docker compose exec postgres psql -U orders -d orders -c "
   SELECT relname, seq_scan, seq_tup_read, idx_scan, idx_tup_fetch, n_live_tup
   FROM pg_stat_user_tables WHERE relname = 'orders';"
   ```

   `ix_orders_customer_id` no figura en `\d orders` y `seq_scan` crece con cada request.

8. **Correlacionar con cambios.** Buscar el DDL que lo borró: migraciones deployadas, logs del servidor y consultas activas.

   ```bash
   docker compose logs --since 1h postgres | grep -iE 'drop index|create index' || true
   docker compose exec postgres psql -U orders -d orders -c "
   SELECT pid, application_name, state, now() - query_start AS running, left(query, 80)
   FROM pg_stat_activity WHERE datname = 'orders' AND state <> 'idle' ORDER BY running DESC;"
   ```

## Consultas PromQL

```promql
# p99 por ruta
histogram_quantile(0.99, sum by (le, route) (rate(http_request_duration_seconds_bucket{job="orders-api"}[5m])))

# p99 solo del endpoint afectado
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job="orders-api",route="/customers/{customer_id}/orders"}[5m])))

# Filas leídas por seq scan vs por índice en orders
rate(pg_stat_user_tables_seq_tup_read{relname="orders"}[5m])
rate(pg_stat_user_tables_idx_tup_fetch{relname="orders"}[5m])

# Proporción de scans secuenciales
rate(pg_stat_user_tables_seq_scan{relname="orders"}[5m])
  / clamp_min(rate(pg_stat_user_tables_seq_scan{relname="orders"}[5m]) + rate(pg_stat_user_tables_idx_scan{relname="orders"}[5m]), 1e-9)

# Saturación del pool como efecto secundario
db_pool_checked_out{job="orders-api"} / db_pool_capacity{job="orders-api"}

# Consultas activas en Postgres
sum by (state) (pg_stat_activity_count{datname="orders"})
```

## Confirmar la causa raíz

La causa está confirmada cuando se cumplen las tres:

1. `EXPLAIN (ANALYZE, BUFFERS)` de la consulta muestra `Seq Scan` sobre `orders` en lugar de usar un índice por `customer_id`.
2. `\d orders` no lista `ix_orders_customer_id` (o el índice existe pero está `INVALID`).
3. Al recrear el índice en una prueba controlada, el plan vuelve a `Index Scan`/`Bitmap Heap Scan` y el p99 de la ruta baja a los valores previos.

Descartar antes: tabla sin `ANALYZE` (estadísticas viejas), bloat extremo, locks (`pg_locks` sin esperas) y saturación de CPU del host por otro contenedor.

## Mitigación inmediata

Recrear el índice sin bloquear escrituras y actualizar estadísticas:

```bash
labctl fix slow-query
```

Equivalente manual (`CONCURRENTLY` no puede ir dentro de una transacción):

```bash
docker compose exec postgres psql -U orders -d orders -c \
  'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_orders_customer_id ON orders (customer_id);'
docker compose exec postgres psql -U orders -d orders -c 'ANALYZE orders;'
```

Verificar con `\d orders` que el índice no quedó `INVALID` y repetir el `EXPLAIN`.

## Solución definitiva

- Índice compuesto que cubre filtro y orden, así Postgres lee las 20 filas ya ordenadas sin sort:

  ```sql
  CREATE INDEX CONCURRENTLY ix_orders_customer_created
      ON orders (customer_id, created_at DESC);
  -- después de validar el plan, el índice simple sobra
  DROP INDEX CONCURRENTLY IF EXISTS ix_orders_customer_id;
  ```

- Las migraciones que borran índices requieren evidencia: `idx_scan` medido en el primario sobre una ventana larga (no en una réplica ni después de un `pg_stat_reset`) y cruce con las top queries de `pg_stat_statements`.
- Revisión de migraciones con `EXPLAIN` sobre datos de tamaño productivo. Un check en CI que corre el `EXPLAIN` de las consultas críticas y falla si aparece `Seq Scan` en `orders`.
- Alerta temprana sobre `seq_tup_read` de tablas grandes (ver `alerts.yml`), además de la de latencia.

## Alertas

Definidas en [`alerts.yml`](alerts.yml):

| Alerta | Señal | Severidad |
|---|---|---|
| `CustomerOrdersLatencyHigh` | p99 de `/customers/{customer_id}/orders` > 500 ms por 5m | warning |
| `OrdersTableSeqScanStorm` | más de 1M filas/s leídas por seq scan en `orders` por 5m | warning |
| `OrdersQueryPathDegradedCritical` | p99 de la ruta > 2 s por 5m | critical |

## Cómo reproducir

```bash
make up-load                 # stack + generador de carga
labctl break slow-query      # DROP INDEX + reset de pg_stat_statements
# esperar 2-5 minutos y seguir el runbook
labctl fix slow-query
```
