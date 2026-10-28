# Postmortem: latencia alta en el historial de órdenes por índice borrado

- **Fecha del incidente:** 2026-08-12
- **Duración:** 1 h 47 min (14:05 a 15:52 UTC-3)
- **Severidad:** SEV-2
- **Estado:** cerrado, action items en curso
- **Autores:** on-call de plataforma, equipo de datos, equipo de órdenes

## Resumen

Una migración de "limpieza de índices sin uso" borró `ix_orders_customer_id` de la tabla `orders`. La consulta de historial por cliente pasó a hacer un scan secuencial de 1M de filas en cada request. El p99 de `GET /customers/{customer_id}/orders` subió de 15 ms a 1,8 s y la contención en Postgres degradó al resto de la API. Se mitigó recreando el índice con `CREATE INDEX CONCURRENTLY`.

## Impacto

- 107 minutos con el historial de órdenes lento. p99 de la ruta entre 0,9 y 2,4 s.
- Entre 14:40 y 15:20 el pool de conexiones de la API llegó al 100% y el 3,1% de todos los requests devolvió 503 (`TimeoutError` del pool).
- Ninguna pérdida de datos. Las órdenes nuevas se crearon bien, con latencia mayor.
- Tickets de soporte: 23 por "la app tarda en mostrar mis pedidos".

## Línea de tiempo

| Hora (UTC-3) | Evento |
|---|---|
| 13:58 | Se deploya la migración `0042_drop_unused_indexes` como parte del release semanal |
| 14:05 | La migración ejecuta `DROP INDEX ix_orders_customer_id` |
| 14:09 | Grafana muestra el p99 de la ruta en 400 ms. No hay alerta de latencia por ruta |
| 14:38 | Dispara `ApiHighLatencyP99` (p99 global > 1 s por 5m). Se pagina al on-call |
| 14:45 | On-call confirma en "p99 por endpoint" que la ruta afectada es una sola |
| 14:52 | `docker stats` muestra Postgres con CPU alta. Hipótesis: consulta lenta |
| 15:05 | `pg_stat_statements` muestra la consulta por `customer_id` con mean de 240 ms |
| 15:12 | `EXPLAIN (ANALYZE, BUFFERS)` devuelve `Parallel Seq Scan on orders` |
| 15:18 | `\d orders` confirma que el índice no existe. Se correlaciona con la migración 0042 |
| 15:25 | Se decide recrear el índice con `CONCURRENTLY` en lugar de revertir el release |
| 15:41 | Termina el `CREATE INDEX CONCURRENTLY` y se ejecuta `ANALYZE orders` |
| 15:52 | p99 de la ruta vuelve a 15 ms, pool normal. Se cierra el incidente |

## Causa raíz

La migración `0042` borró índices cuyo `idx_scan` era bajo según una consulta a `pg_stat_user_tables`. Esa consulta se corrió en una réplica de lectura que no atiende el tráfico del historial y cuyas estadísticas se habían reseteado días antes. `ix_orders_customer_id` era el único índice que permitía resolver `WHERE customer_id = $1 ORDER BY created_at DESC LIMIT 20` sin recorrer la tabla. Sin él, cada request lee 1M de filas y ordena, con lo que la ruta más usada (40% del tráfico) multiplicó su costo por más de diez.

## 5 porqués

1. ¿Por qué subió la latencia? Porque la consulta de historial hacía un Seq Scan sobre 1M de filas.
2. ¿Por qué hacía Seq Scan? Porque no existía un índice sobre `customer_id`.
3. ¿Por qué no existía? Porque la migración 0042 lo borró considerándolo sin uso.
4. ¿Por qué se lo consideró sin uso? Porque la evidencia (`idx_scan`) se tomó de una réplica con estadísticas recién reseteadas, no del primario.
5. ¿Por qué eso llegó a producción? Porque la revisión de migraciones no exige `EXPLAIN` de las consultas críticas ni cruzar contra `pg_stat_statements`, y no hay un entorno con volumen productivo donde se note la regresión.

## Qué salió bien

- El dashboard por endpoint permitió aislar la ruta en minutos.
- `pg_stat_statements` y `log_min_duration_statement` ya estaban activos, la evidencia estaba disponible.
- `CREATE INDEX CONCURRENTLY` permitió mitigar sin cortar escrituras ni revertir el release completo.

## Qué salió mal

- 33 minutos entre el inicio y la primera alerta: solo había alerta de p99 global.
- La migración destructiva salió junto a otros cambios, lo que demoró la correlación.
- No había alerta sobre lecturas secuenciales en tablas grandes.
- El pool de la API se saturó y amplificó el impacto a otras rutas.

## Dónde tuvimos suerte

- Fue en horario laboral con el equipo de datos disponible.
- La tabla tenía 1M de filas. Con el volumen proyectado para fin de año el `CREATE INDEX` hubiera tardado mucho más.

## Action items

| # | Acción | Tipo | Dueño | Prioridad | Estado |
|---|---|---|---|---|---|
| 1 | Reemplazar el índice por `(customer_id, created_at DESC)` | Prevención | Equipo de órdenes | P1 | En curso |
| 2 | Check en CI que corre `EXPLAIN` de las consultas críticas y falla con `Seq Scan` en `orders` | Prevención | Equipo de plataforma | P1 | Pendiente |
| 3 | Política: borrar índices solo con `idx_scan` del primario en 30 días y cruce con `pg_stat_statements` | Proceso | Equipo de datos | P1 | Hecho |
| 4 | Alertas de p99 por ruta y de `seq_tup_read` en `orders` | Detección | On-call de plataforma | P0 | Hecho |
| 5 | Migraciones destructivas en un deploy separado con rollback documentado | Proceso | Equipo de plataforma | P2 | Pendiente |
| 6 | Runbook de recreación de índices con `CONCURRENTLY` y verificación de `INVALID` | Mitigación | Equipo de datos | P2 | Hecho |

## Lecciones aprendidas

- Las estadísticas de uso de índices dependen de dónde y desde cuándo se miden. Sin ese contexto engañan.
- Alertar por ruta detecta regresiones localizadas que el promedio global esconde.
- La base suele ser el cuello compartido: una consulta lenta termina en pool agotado y errores en endpoints que no tienen nada que ver.
