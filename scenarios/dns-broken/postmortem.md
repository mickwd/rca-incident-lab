# Postmortem: la API de órdenes no resolvía el host de la base tras un cambio de configuración

- **Fecha del incidente** 2026-06-09
- **Duración** 47 min (15:12 a 15:59 UTC-3)
- **Severidad** SEV-2
- **Estado** Cerrado, action items en curso
- **Autores** On-call de SRE, desarrollador del equipo de órdenes

## Resumen

Como parte de una migración de nombres internos se cambió la variable `DB_HOST` de la API de `postgres` a `orders-db.internal`. El nuevo nombre todavía no estaba dado de alta en la red. La API arrancó y pasó su liveness check, pero todas las operaciones contra la base fallaron con `Name or service not known`. Se mitigó volviendo a la configuración anterior.

## Impacto

- 47 minutos con la creación y consulta de órdenes caídas (503).
- La consulta de productos siguió funcionando parcialmente gracias al cache de Redis.
- Unas 1.900 órdenes no se crearon. Los clientes recibieron error y la mayoría reintentó más tarde.
- No hubo pérdida ni corrupción de datos.

## Línea de tiempo

| Hora (UTC-3) | Evento |
|---|---|
| 15:10 | Se despliega el cambio de configuración con `DB_HOST=orders-db.internal`. |
| 15:12 | La API nueva queda `healthy`. El deploy se marca exitoso. |
| 15:13 | El error rate 5xx sube al 70%. `ApiHighErrorRate` dispara a las 15:15. |
| 15:18 | El on-call ve `OperationalError` dominante y sospecha de Postgres. |
| 15:26 | Postgres responde a `pg_isready` y no tiene conexiones nuevas. Se descarta. |
| 15:34 | En los logs aparece `failed to resolve host 'orders-db.internal'`. |
| 15:39 | `getent hosts orders-db.internal` dentro del contenedor no resuelve. Se identifica el cambio. |
| 15:52 | Se revierte la variable y se recrea solo la API. |
| 15:59 | `/readyz` devuelve 200 y el error rate vuelve a 0. Fin de la mitigación. |

## Causa raíz

La migración de nombres se planificó en dos pasos (crear el alias y después cambiar los clientes), pero el cambio de la API se mergeó antes de que el alias existiera. El pipeline de deploy validaba solo `/healthz`, que no toca dependencias, por lo que la versión rota se consideró sana y recibió todo el tráfico.

## 5 porqués

1. ¿Por qué fallaban las órdenes? Porque la API no podía abrir conexiones a Postgres.
2. ¿Por qué no podía conectarse? Porque `orders-db.internal` no resolvía en el DNS interno de Docker.
3. ¿Por qué apuntaba a un nombre inexistente? Porque el cambio de configuración se desplegó antes que el alias del que dependía.
4. ¿Por qué el deploy no lo frenó? Porque el gate de deploy usaba `/healthz` (liveness) y no `/readyz` (readiness).
5. ¿Por qué el gate usaba liveness? Porque se copió del health check de Docker sin distinguir entre "el proceso vive" y "el servicio puede atender".

## Qué salió bien

- `ApiHighErrorRate` disparó en 3 minutos.
- El mensaje de error de psycopg era explícito y llevó directo a DNS.
- El rollback fue de un solo servicio y no requirió reiniciar Postgres ni Redis.

## Qué salió mal

- Se perdieron 16 minutos investigando Postgres por el nombre genérico `OperationalError`.
- El deploy se marcó exitoso con la API sin poder atender.
- No había un diff visible de variables de entorno en la revisión del cambio.
- El orden de los pasos de la migración no estaba explícito en el ticket.

## Dónde tuvimos suerte

- El cache de Redis mantuvo vivas las lecturas de productos y redujo el impacto percibido.
- El cambio afectaba solo a la API: el worker seguía con la configuración vieja.

## Action items

| # | Acción | Tipo | Dueño | Prioridad | Estado |
|---|---|---|---|---|---|
| 1 | Gate de deploy contra `/readyz` con rollback automático si falla | Prevención | Equipo de plataforma | P0 | En curso |
| 2 | Validar en el arranque que los hosts de dependencias resuelvan y loguear error claro | Prevención | Equipo de órdenes | P1 | Pendiente |
| 3 | Alertas `ApiNotReady` y `ApiDependencyDown` sobre probe de readyz | Detección | On-call de SRE | P1 | Hecho |
| 4 | Mostrar diff de variables de entorno en cada PR de configuración | Proceso | Equipo de plataforma | P2 | Pendiente |
| 5 | Documentar nombres DNS de cada dependencia por entorno | Proceso | Líder técnico de órdenes | P2 | Pendiente |
| 6 | Runbook: buscar el mensaje exacto antes de asumir la dependencia caída | Mitigación | On-call de SRE | P2 | Hecho |

## Lecciones aprendidas

- Liveness y readiness responden preguntas distintas y no son intercambiables en un gate de deploy.
- Un error rápido con el proceso vivo suele ser configuración, no capacidad.
- Los cambios en varios pasos necesitan el orden explícito y una verificación entre pasos.
