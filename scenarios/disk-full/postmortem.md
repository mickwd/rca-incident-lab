# Postmortem: altas de órdenes rechazadas por disco lleno

- **Fecha del incidente:** 2026-05-06
- **Duración:** 1 hora 52 minutos (02:41 a 04:33 UTC-3)
- **Severidad:** SEV-2
- **Estado:** cerrado, action items en curso
- **Autores:** on-call de plataforma (L3), analista de soporte L2
- **Formato:** blameless. Buscamos qué permitió que pasara, no quién lo hizo.

## Resumen

Durante la madrugada del 6 de mayo el volumen `/data/uploads` de la API de órdenes se llenó por completo. Unas horas antes se había generado un export de diagnóstico grande dentro de ese mismo volumen para investigar otro ticket, y el archivo quedó ahí. Cuando el volumen llegó al 100%, la escritura del comprobante de cada orden empezó a fallar con `ENOSPC`, la transacción hacía rollback y `POST /orders` devolvía 500. Las consultas siguieron funcionando y los health checks se mantuvieron en verde, así que el problema se detectó por la alerta de error rate y no por los probes. Se mitigó borrando el archivo.

## Impacto

- 1 h 52 min sin poder crear órdenes. Unas 1.900 órdenes rechazadas, concentradas en clientes mayoristas que cargan pedidos de madrugada.
- Ninguna orden quedó inconsistente: el rollback evitó descontar stock sin orden.
- Lecturas (catálogo, historial) sin impacto.
- 12 tickets de clientes y una escalada comercial a primera hora.

## Línea de tiempo

| Hora (UTC-3) | Evento |
|---|---|
| 05-05 23:10 | Se genera un export de diagnóstico en `/data/uploads/tmp/export-debug.bin` para un ticket de soporte. |
| 05-06 02:41 | El volumen llega a 100%. Primeros `OSError: [Errno 28] No space left on device` en `write_receipt`. |
| 02:43 | `ApiHighErrorRate` no dispara: el 5xx global queda por debajo del 5% porque la mayoría del tráfico son lecturas. |
| 03:35 | Un cliente mayorista reporta por mail que no puede confirmar pedidos. Queda en cola de soporte L1. |
| 04:02 | Soporte L2 reproduce el error y escala a L3. |
| 04:08 | El on-call ve en Grafana 500 solo en `POST /orders` y `exc OSError`. `/readyz` en 200. |
| 04:12 | Primera hipótesis: problema en la base al hacer el UPDATE de stock. Se descarta: el error es inmediato y el traceback termina en `receipts.py`. |
| 04:17 | `docker compose logs api` muestra `Errno 28`. `df -h /data/uploads` dentro del contenedor: 100%. |
| 04:20 | El panel de disco del host está en 40%: se confirma que node_exporter no ve el tmpfs del contenedor. |
| 04:24 | `du` y `find -size +10M` encuentran `tmp/export-debug.bin` de 47 MB. |
| 04:29 | Se confirma con soporte L2 que el export ya no se necesita. Se borra el archivo. |
| 04:33 | `POST /orders` vuelve a 201 y `orders_created_total` crece. Se declara mitigado. |

## Causa raíz

El volumen de comprobantes (`tmpfs` de 48 MB) se usaba también como espacio de trabajo para archivos temporales. Un export de diagnóstico de 47 MB se generó ahí y no se borró. Al llegar al 100%, la escritura del comprobante de cada orden fallaba. Como el comprobante se escribe de forma síncrona dentro de la transacción del alta, cada falla de disco se convertía en una orden rechazada.

## 5 porqués

1. **¿Por qué fallaban las altas de órdenes?** Porque `write_receipt` levantaba `OSError: [Errno 28] No space left on device` y el handler hacía rollback.
2. **¿Por qué no había espacio?** Porque un export de diagnóstico de 47 MB ocupaba casi todo el volumen de 48 MB.
3. **¿Por qué el export estaba en ese volumen?** Porque era el único directorio con permisos de escritura en el contenedor, y no había una ubicación ni un procedimiento definido para exports temporales.
4. **¿Por qué el alta depende de que el disco tenga espacio?** Porque el comprobante se escribe de forma síncrona en la misma transacción que el alta, en lugar de usar un outbox desacoplado.
5. **¿Por qué tardamos casi una hora en enterarnos?** Porque no había métrica ni alerta del volumen del contenedor (node_exporter no ve tmpfs), el readiness no chequea disco y la alerta de error rate es global, así que un endpoint con 100% de errores quedaba diluido entre las lecturas.

## Qué salió bien

- El rollback funcionó: no hubo stock descontado sin orden ni datos a corregir.
- Una vez escalado, el diagnóstico llevó 25 minutos siguiendo el runbook.
- Los logs tenían el traceback completo con el errno, lo que apuntó directo al disco.

## Qué salió mal

- La detección dependió de un cliente. Cerca de una hora de impacto sin ninguna alerta.
- El dashboard de disco mostraba el host y daba una falsa sensación de normalidad.
- Los probes de readiness siguieron en verde con la funcionalidad principal rota.
- No había un procedimiento para limpiar archivos temporales después de un diagnóstico.

## Dónde tuvimos suerte

- Pasó de madrugada, con poco volumen de ventas.
- El archivo que llenó el disco era descartable. Si el volumen se hubiera llenado por comprobantes legítimos, liberar espacio habría requerido decidir qué datos mover con el incidente en curso.
- El `tmpfs` cuenta contra la memoria del contenedor. Con una fuga de memoria al mismo tiempo, el contenedor podría haber terminado en OOM kill.

## Action items

| # | Acción | Tipo | Dueño | Prioridad | Estado |
|---|---|---|---|---|---|
| 1 | Exponer `uploads_disk_free_bytes` y `uploads_disk_total_bytes` y agregarlas al dashboard | Detección | Equipo de órdenes | P0 | Hecho |
| 2 | Alertas `UploadsDiskAlmostFull`, `UploadsDiskFillingUp` y `OrderWritesFailingDiskErrors` | Detección | SRE / plataforma | P0 | Hecho |
| 3 | Separar volúmenes: comprobantes por un lado, temporales y exports por otro | Prevención | SRE / plataforma | P1 | En curso |
| 4 | Escribir comprobantes vía outbox en la base para que una falla de disco no rechace órdenes | Prevención | Equipo de órdenes | P1 | Pendiente |
| 5 | Retención de comprobantes locales (subir a object storage y borrar a las 24 h) | Prevención | Equipo de órdenes | P2 | Pendiente |
| 6 | Alerta de error rate por endpoint además de la global | Detección | SRE / plataforma | P1 | Pendiente |
| 7 | Procedimiento de diagnóstico: dónde generar exports y checklist de limpieza al cerrar el ticket | Proceso | Líder de soporte L2 | P2 | Pendiente |

## Lecciones aprendidas

- Medir el disco donde la aplicación escribe, no solo el del host. Un contenedor puede tener volúmenes que el monitoreo del nodo no ve.
- Un health check en verde no significa que el servicio funcione. Las alertas de impacto tienen que mirar los endpoints que generan valor, uno por uno.
- Una dependencia secundaria (el comprobante) no debería poder tirar abajo la operación principal (vender).
- Los archivos temporales necesitan un lugar propio y un dueño que los limpie.
