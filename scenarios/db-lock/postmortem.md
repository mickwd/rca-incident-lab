# Postmortem: creación de órdenes bloqueada por un lock del job de conciliación

- **Fecha del incidente:** 2026-05-20
- **Duración:** 47 min (10:13 a 11:00 UTC-3)
- **Severidad:** SEV-1
- **Estado:** cerrado, action items en curso
- **Autores:** guardia de SRE, equipo de Órdenes, DBA de guardia
- **Formato:** blameless. Analizamos el sistema y el proceso, no a las personas.

## Resumen

El job de conciliación de stock del worker bloquea con `SELECT ... FOR UPDATE` todas las filas de `products` y, dentro de la misma transacción, consulta el ERP producto por producto. El 20 de mayo el ERP dejó de responder por una ventana de mantenimiento no comunicada y la transacción quedó abierta en estado `idle in transaction`. Todo `POST /orders` necesita actualizar el stock de un producto, así que quedó esperando el lock. Los requests retenían conexiones del pool de la API, el pool se agotó y el error se propagó a endpoints de lectura. Se mitigó terminando la sesión del worker y deshabilitando la conciliación.

## Impacto

- 47 minutos sin creación de órdenes: 0 órdenes confirmadas entre 10:14 y 10:58.
- ~11.200 `POST /orders` fallidos (100% del endpoint), 504 tras 30 s de espera.
- 38% de los requests de lectura (`GET /orders/{id}`, `GET /customers/{id}/orders`) devolvieron 503 por pool agotado.
- `GET /products/{id}` se mantuvo mayormente disponible gracias al cache de Redis.
- Sin pérdida de datos: las órdenes no confirmadas hicieron rollback. Soporte reprocesó 230 pedidos telefónicos a mano.

## Línea de tiempo

| Hora (UTC-3) | Evento |
|---|---|
| 10:13 | Arranca la conciliación programada. Toma locks sobre los 200 productos; la primera llamada al ERP no responde |
| 10:14 | Primeros `POST /orders` en espera. Postgres loguea `still waiting for ShareLock on transaction` |
| 10:14 | El pool de la API (10 conexiones) se llena; aparecen `QueuePool limit ... reached` y 503 en lecturas |
| 10:19 | Dispara `ApiHighErrorRate`. Guardia de SRE toma el incidente |
| 10:24 | `docker stats` muestra CPU bajo. Se descarta carga; la hipótesis inicial es "Postgres lento" |
| 10:31 | Se reinicia la API. El pool se libera y se vuelve a llenar en 6 segundos: se descarta fuga de conexiones |
| 10:38 | `py-spy dump` muestra los hilos esperando dentro de `psycopg`. Se convoca al DBA de guardia |
| 10:44 | `pg_stat_activity` + `pg_blocking_pids()` señalan una sesión `orders-worker` `idle in transaction` de 31 minutos |
| 10:49 | Se deshabilita la conciliación y se ejecuta `pg_terminate_backend` sobre esa sesión. Los `UPDATE` se destraban al instante |
| 10:52 | Pool estable, error rate en 0%, `orders_created_total` vuelve a crecer |
| 11:00 | Se cierra el incidente tras verificar stock consistente con el ERP en una muestra de productos |

## Causa raíz

`run_reconciliation` mantenía una transacción abierta con locks de fila sobre toda la tabla `products` mientras hacía llamadas de red al ERP sin timeout. Cuando el ERP dejó de responder la transacción no terminó nunca. Postgres no tenía `idle_in_transaction_session_timeout` ni la API un `lock_timeout`, así que nada cortó la espera: los `UPDATE products` de la API se encolaron indefinidamente y, al no tener timeout propio, retuvieron las conexiones del pool hasta agotarlo.

## 5 porqués

1. ¿Por qué no se podían crear órdenes? Porque el `UPDATE products` que descuenta stock quedaba esperando un lock de fila.
2. ¿Por qué había un lock tomado? Porque la conciliación del worker hizo `SELECT ... FOR UPDATE` sobre todos los productos y su transacción no terminaba.
3. ¿Por qué no terminaba? Porque dentro de la transacción esperaba una respuesta del ERP, que estaba caído, y la llamada no tenía timeout.
4. ¿Por qué una llamada externa estaba dentro de una transacción? Porque el job se diseñó para "leer, consultar y escribir" en un solo bloque y no había guía de diseño que lo prohibiera.
5. ¿Por qué afectó también a las lecturas? Porque los requests bloqueados retuvieron todas las conexiones del pool y no había `lock_timeout` ni `statement_timeout` que los cortara antes.
6. ¿Por qué tardamos 30 minutos en encontrarlo? Porque no había alerta de transacciones largas y el primer reflejo fue mirar CPU y reiniciar la API, que trataba el síntoma.

## Qué salió bien

- `log_lock_waits` estaba activo: los logs de Postgres mostraban el pid bloqueante desde el primer minuto.
- `application_name` distinto para API y worker permitió identificar al bloqueante sin ambigüedad.
- El rollback transaccional evitó órdenes a medio crear o stock descontado sin orden.
- El cache de Redis mantuvo el catálogo disponible.

## Qué salió mal

- No había alerta sobre transacciones `idle in transaction` ni sobre jobs del worker que no terminan.
- Reiniciar la API costó tiempo y generó más errores sin acercarnos a la causa.
- Los logs de Postgres no estaban en el primer paso del runbook general; se llegaron a mirar a los 30 minutos.
- La ventana de mantenimiento del ERP no se comunicó a los equipos consumidores.

## Dónde tuvimos suerte

- El incidente ocurrió a media mañana con el equipo completo disponible. A la madrugada el job corre igual y nadie habría estado mirando.
- El lock era de fila (`FOR UPDATE`) y no de tabla: las lecturas que no pasaban por el pool agotado siguieron funcionando.

## Action items

| # | Acción | Tipo | Dueño | Prioridad | Estado |
|---|---|---|---|---|---|
| 1 | Reescribir la conciliación: consultar el ERP fuera de la transacción y escribir en lotes cortos con `SKIP LOCKED` | Prevención | Equipo de Órdenes | P0 | En curso |
| 2 | Configurar `idle_in_transaction_session_timeout = 60s` para el rol del worker y `lock_timeout = 3s` para el rol de la API | Mitigación | DBA | P0 | Hecho |
| 3 | Alertas `PostgresLongIdleInTransaction` y `WorkerPendingProcessingStalled` con runbook enlazado | Detección | Equipo de SRE | P0 | Hecho |
| 4 | Timeout explícito (5 s) en todas las llamadas al ERP desde el worker | Prevención | Equipo de Órdenes | P1 | Hecho |
| 5 | Agregar consulta de `pg_stat_activity` con `pg_blocking_pids()` como paso 1 del runbook de latencia con CPU bajo | Proceso | Equipo de SRE | P1 | Hecho |
| 6 | Acordar con el equipo del ERP aviso previo de ventanas de mantenimiento | Proceso | Líder técnico de Órdenes | P2 | Pendiente |

## Lecciones aprendidas

- Latencia alta con CPU bajo significa espera: buscar locks, colas o dependencias antes de pensar en capacidad.
- Una transacción abierta es un recurso compartido; mantenerla abierta durante una llamada de red es apostar la disponibilidad a un sistema ajeno.
- Los timeouts tienen que existir en todas las capas (llamada externa, lock, statement, pool, proxy) y ser coherentes entre sí.
- El pool agotado casi nunca es la causa raíz: es el punto donde un problema de otra capa se vuelve visible.
