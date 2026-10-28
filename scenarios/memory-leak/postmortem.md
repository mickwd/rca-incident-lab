# Postmortem: reinicios en loop de orders-api por fuga de memoria

- **Fecha del incidente:** 2026-03-12
- **Duración:** 1 h 22 min (14:03 a 15:25 UTC-3)
- **Severidad:** SEV-2
- **Estado:** cerrado, action items en curso
- **Autores:** guardia de SRE, líder técnico del equipo de Órdenes
- **Formato:** blameless. Analizamos el sistema y el proceso, no a las personas.

## Resumen

El 12 de marzo a las 13:40 se desplegó la versión 1.4.2 de `orders-api`, que agregaba un "memo" en memoria de las respuestas de `GET /products/{id}` para bajar la carga sobre Redis. La clave del memo incluía el `X-Request-ID`, que nginx genera distinto en cada request, así que la estructura crecía con cada request y nunca se liberaba. Cada 2 a 3 minutos el contenedor superaba su límite de 256 MiB, el kernel lo mataba (OOM kill) y Docker lo reiniciaba. Durante cada reinicio nginx devolvía 502. Se mitigó desactivando el flag del memo y reiniciando la API, y se resolvió con un rollback del cambio.

## Impacto

- 1 h 22 min de errores intermitentes en toda la API (no solo en el endpoint con la fuga).
- 31 reinicios del contenedor `rca-lab-api-1`.
- 6,8% de los requests terminaron en 502 durante la ventana (~6.700 de ~98.000).
- ~410 órdenes no se pudieron crear en el primer intento; el 85% se reintentó con éxito desde el frontend.
- Sin pérdida de datos: las órdenes que fallaron no llegaron a confirmarse en la base.

## Línea de tiempo

| Hora (UTC-3) | Evento |
|---|---|
| 13:40 | Deploy de orders-api 1.4.2 con el memo de respuestas detrás del flag `memory-leak`, activado al 100% |
| 14:03 | Primer OOM kill del contenedor. Nadie lo nota: el healthcheck se recupera en segundos |
| 14:19 | Soporte recibe dos tickets de "error 502 al ver productos" |
| 14:31 | Dispara `ApiRestartLoop` (agregada como regla manual días antes, sin `for`). Guardia toma el incidente |
| 14:36 | `docker inspect` muestra `OOMKilled=true`, `RestartCount=14`. Se descarta excepción en el arranque |
| 14:44 | Grafana muestra diente de sierra en el RSS. Se sube `mem_limit` a 512 MiB como paliativo: los reinicios se espacian pero siguen |
| 14:58 | Se correlaciona la pendiente con el RPS de `GET /products/{product_id}`; `py-spy dump` muestra `_memoize_response` |
| 15:06 | Se desactiva el flag (`DEL fault:memory-leak`) y se reinicia la API. RSS estable en ~85 MiB |
| 15:25 | Rollback a 1.4.1, `mem_limit` vuelve a 256 MiB. Se cierra el incidente tras 15 minutos sin reinicios |

## Causa raíz

El memo agregado en `GET /products/{id}` era un `dict` global sin límite de tamaño ni TTL, con clave `(product_id, X-Request-ID)`. Como el request id es único por request, cada request agregaba una entrada nueva (~256 KiB) y ninguna se leía ni se borraba. El consumo de memoria quedó proporcional al tráfico acumulado desde el último arranque, y con 256 MiB de límite el proceso duraba entre 2 y 3 minutos bajo carga normal.

## 5 porqués

1. ¿Por qué hubo 502? Porque nginx no tenía upstream mientras el contenedor de la API se reiniciaba.
2. ¿Por qué se reiniciaba? Porque el kernel lo mataba al superar el límite de memoria del cgroup (OOM kill, exit 137).
3. ¿Por qué superaba el límite? Porque un `dict` global crecía ~256 KiB con cada `GET /products/{id}` y nunca se liberaba.
4. ¿Por qué nunca se liberaba? Porque la clave incluía el request id, que nunca se repite, y la estructura no tenía tamaño máximo ni TTL.
5. ¿Por qué llegó a producción? Porque la revisión de código no tiene un criterio para caches en proceso y las pruebas de carga previas al deploy duran 2 minutos y no miran la tendencia de memoria.
6. ¿Por qué tardamos en detectarlo? Porque no había alertas de memoria ni de reinicios con umbrales razonables; el healthcheck se recuperaba solo y ocultaba el problema.

## Qué salió bien

- `docker inspect` dio la confirmación del OOM en un solo comando y permitió descartar rápido la hipótesis de excepción en el arranque.
- El cambio estaba detrás de un feature flag: la mitigación fue un comando de Redis más un reinicio, sin esperar un build.
- Las órdenes fallidas hicieron rollback completo; no quedaron datos inconsistentes.

## Qué salió mal

- 28 minutos entre el primer OOM y la detección: no había alerta de memoria y el crash loop "parecía" sano en `docker compose ps`.
- Subir `mem_limit` consumió tiempo y escondió parcialmente el síntoma sin acercarnos a la causa.
- Los logs de la API no muestran nada antes de un SIGKILL; el equipo perdió tiempo buscando un traceback que no existía.

## Dónde tuvimos suerte

- El tráfico era el de un jueves a la tarde. En un pico de promociones el proceso habría durado menos de un minuto y el impacto habría sido mucho mayor.
- El tmpfs de uploads estaba casi vacío; si hubiese tenido datos, el OOM habría llegado antes porque también cuenta contra el límite del contenedor.

## Action items

| # | Acción | Tipo | Dueño | Prioridad | Estado |
|---|---|---|---|---|---|
| 1 | Reemplazar el memo por el cache de Redis existente o un `TTLCache` acotado con clave por producto | Prevención | Equipo de Órdenes | P0 | Hecho |
| 2 | Alertas `ApiMemoryNearLimit`, `ApiMemoryGrowing` y `ApiRestartLoop` con runbook enlazado | Detección | Equipo de SRE | P0 | Hecho |
| 3 | Prueba de carga de 15 minutos en staging con chequeo de pendiente de memoria como gate del pipeline | Prevención | Equipo de Plataforma | P1 | En curso |
| 4 | Agregar "caches en proceso: tamaño máximo, TTL y clave" al checklist de revisión de código | Proceso | Líder técnico de Órdenes | P1 | Hecho |
| 5 | Endpoint de debug protegido con snapshot de `tracemalloc` para diagnosticar fugas en vivo | Mitigación | Equipo de Órdenes | P2 | Pendiente |
| 6 | Documentar en el runbook general que un exit 137 sin traceback se investiga con `docker inspect` y `docker events` | Proceso | Equipo de SRE | P2 | Hecho |

## Lecciones aprendidas

- Un proceso que "se recupera solo" no está sano: los reinicios son una señal que hay que alertar.
- Todo cache en memoria necesita límite y TTL; la clave tiene que identificar el recurso, nunca el request.
- Ante un exit 137 los logs de la aplicación no van a tener la respuesta; la evidencia está en el runtime (`docker inspect`, eventos, `dmesg`) y en las métricas de tendencia.
- Subir límites de recursos es un paliativo para ganar tiempo, no un paso de diagnóstico.
