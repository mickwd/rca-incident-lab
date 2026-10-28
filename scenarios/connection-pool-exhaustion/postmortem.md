# Postmortem: la API de órdenes agotó su pool de conexiones a la base

- **Fecha del incidente:** 2026-03-12
- **Duración:** 47 minutos (14:18 a 15:05 UTC-3)
- **Severidad:** SEV-2
- **Estado:** cerrado, action items en curso
- **Autores:** on-call de plataforma (L3), líder técnico del equipo de órdenes
- **Formato:** blameless. El objetivo es entender cómo el sistema permitió el error, no buscar culpables.

## Resumen

A las 14:15 se desplegó la versión 1.4.2 de `orders-api`, que agregaba una "auditoría diferida" en `GET /orders/{id}`. Esa función abría su propia sesión de base de datos y la guardaba en una lista para procesarla más tarde, pero nunca la cerraba. Cada consulta de una orden dejaba una conexión del pool tomada. Con 10 conexiones de capacidad por instancia, el pool se agotó en segundos y a partir de ahí todos los endpoints que usan la base respondieron 503 después de esperar 5 segundos. La mitigación fue desactivar la funcionalidad y reiniciar la API.

## Impacto

- 47 minutos con error rate entre 85% y 98% en los endpoints que tocan la base (consulta de catálogo, historial de clientes, alta de órdenes).
- Aproximadamente 3.100 órdenes no pudieron crearse. Según soporte, alrededor del 60% se reintentó con éxito después de la mitigación.
- Latencia p99 de 5 s durante todo el incidente.
- Postgres no tuvo impacto: el resto de los consumidores (worker, reportes) siguieron funcionando.
- 41 tickets de clientes en soporte nivel 1.

## Línea de tiempo

| Hora (UTC-3) | Evento |
|---|---|
| 14:15 | Deploy de `orders-api` 1.4.2 con la auditoría diferida en `GET /orders/{id}`. |
| 14:18 | `db_pool_checked_out` llega a 10 de 10 y deja de bajar. Empiezan los 503. |
| 14:19 | Dispara `DbPoolTimeouts` (critical). Paginan al on-call. |
| 14:23 | El on-call confirma con curl: `/healthz` 200 instantáneo, `/readyz` 503 en 5 s. |
| 14:26 | Primera hipótesis: Postgres saturado. Se descarta: `psql` conecta al instante y hay 17 conexiones de 100. |
| 14:31 | Segunda hipótesis: locks. Se descarta: `pg_locks` sin esperas. |
| 14:34 | `pg_stat_activity` muestra 10 conexiones `orders-api` en `idle in transaction` con la última query sobre `orders`. Se sospecha una fuga en la aplicación. |
| 14:38 | Reinicio de la API como prueba. El servicio se recupera y a los 20 s vuelve a fallar. Confirma fuga, no capacidad. |
| 14:44 | `py-spy dump` muestra todos los hilos esperando en `QueuePool._do_get`. Se correlaciona el inicio con el deploy de las 14:15. |
| 14:52 | Revisión del diff: `_load_order_with_audit` crea una sesión fuera de `get_db` y la guarda en `pending_audit_sessions`. |
| 14:58 | Se desactiva el flag de la auditoría diferida y se reinicia la API. |
| 15:05 | Pool estable en 1-3 conexiones, error rate en 0. Se declara mitigado. |

## Causa raíz

La función `_load_order_with_audit` creaba una sesión con `session_factory()` fuera de la dependency `get_db` y la agregaba a `pending_audit_sessions` para una auditoría que se iba a ejecutar "más tarde" pero que nunca se implementó del todo. La sesión hacía una consulta (autobegin abre transacción), así que la conexión quedaba en `idle in transaction` y nunca volvía al pool. Como la sesión seguía referenciada, ni el garbage collector la liberaba.

Con un pool de 5 + 5 conexiones y ~2 requests por segundo a ese endpoint, el pool se agotaba en unos 5 segundos. Desde ahí todo request que necesitaba la base esperaba `pool_timeout` (5 s) y fallaba con `sqlalchemy.exc.TimeoutError`.

## 5 porqués

1. **¿Por qué la API respondía 503?** Porque los requests no conseguían una conexión del pool en 5 s y SQLAlchemy levantaba `TimeoutError: QueuePool limit of size 5 overflow 5 reached`.
2. **¿Por qué el pool no tenía conexiones libres?** Porque las 10 estaban tomadas por sesiones en `idle in transaction` que nadie cerraba.
3. **¿Por qué nadie las cerraba?** Porque `_load_order_with_audit` creaba sesiones fuera del ciclo de vida de `get_db` y las guardaba en una lista global para usarlas después.
4. **¿Por qué ese patrón llegó a producción?** Porque los tests verificaban el contenido de la respuesta pero no el estado del pool, y la revisión de código no tenía una regla explícita sobre crear sesiones fuera de la dependency.
5. **¿Por qué no lo detectamos antes de impactar a los usuarios?** Porque no había alerta de saturación del pool (solo de errores), el entorno de staging tiene tan poco tráfico que el pool tardaba horas en llenarse y no se corrió prueba de carga para este cambio.

## Qué salió bien

- La alerta de timeouts disparó en 1 minuto y el on-call respondió rápido.
- El runbook de "la API no llega a la base" llevó a descartar Postgres y locks con evidencia en lugar de reiniciar la base.
- `application_name` distinto para API y worker permitió ver en segundos quién tenía las conexiones.
- La métrica `db_pool_checked_out` ya existía y dejó clara la diferencia entre fuga (no baja) y falta de capacidad (baja con el tráfico).

## Qué salió mal

- Se perdieron 8 minutos con las hipótesis de base saturada y locks, aunque el mensaje del log ya decía `QueuePool limit`.
- El deploy no tenía un indicador visible en Grafana, así que la correlación con el cambio llegó tarde.
- El reinicio de prueba recuperó el servicio solo por 20 segundos y generó la falsa sensación de que estaba resuelto.
- No había forma de desactivar la funcionalidad sin reiniciar, porque las sesiones filtradas quedaban en memoria.

## Dónde tuvimos suerte

- Postgres tenía `max_connections` holgado. Con más réplicas de la API la fuga podría haber agotado las conexiones de la base y tirado también al worker y a los reportes.
- El incidente fue en horario laboral con el autor del cambio disponible para revisar el diff.

## Action items

| # | Acción | Tipo | Dueño | Prioridad | Estado |
|---|---|---|---|---|---|
| 1 | Reescribir la auditoría diferida para usar `with session_factory()` y encolar datos planos | Prevención | Equipo de órdenes | P0 | Hecho |
| 2 | Test de regresión que verifique `engine.pool.checkedout() == 0` después de llamar a cada endpoint | Prevención | Equipo de órdenes | P0 | Hecho |
| 3 | Alerta `DbPoolSaturated` sobre `db_pool_checked_out / db_pool_capacity` | Detección | SRE / plataforma | P1 | Hecho |
| 4 | Configurar `idle_in_transaction_session_timeout = 60s` para el rol de la API | Mitigación | DBA | P1 | En curso |
| 5 | Anotaciones de deploy en el dashboard de Grafana | Detección | SRE / plataforma | P1 | Pendiente |
| 6 | Regla de lint o checklist de revisión: no crear sesiones fuera de `get_db` o un context manager | Proceso | Líder técnico de órdenes | P2 | Pendiente |
| 7 | Prueba de carga corta en staging como paso obligatorio para cambios en la capa de datos | Proceso | QA / plataforma | P2 | Pendiente |

## Lecciones aprendidas

- Un pool clavado en el máximo que no baja con el tráfico es una fuga, no un problema de capacidad. Agrandar el pool solo hubiera retrasado el síntoma.
- El primer mensaje de error suele decir dónde mirar. `QueuePool limit` apunta a la aplicación, no a la base.
- Ponerle `application_name` a cada cliente de la base es barato y ahorra mucho tiempo en un incidente.
- Los recursos con ciclo de vida (sesiones, conexiones, archivos) tienen que abrirse y cerrarse en el mismo bloque. Todo lo que los guarde "para después" merece una revisión extra.
