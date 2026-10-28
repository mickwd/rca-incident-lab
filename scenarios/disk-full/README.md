# Disco lleno en el volumen de uploads

Las altas de órdenes empiezan a fallar mientras las consultas siguen funcionando. Es una caída parcial: el catálogo y el historial responden bien, pero ninguna orden nueva se confirma. La API escribe un comprobante por cada orden en un volumen chico, y ese volumen se quedó sin espacio.

**Categoría:** infra · **Dificultad:** fácil a media · **Herramientas:** Grafana, PromQL, `docker compose logs`, `df`, `du`, `find`

## Síntomas

- Los usuarios pueden navegar el catálogo y ver sus órdenes, pero al confirmar una compra reciben un error. `POST /orders` devuelve `500`:

  ```json
  {"detail": "internal error", "error": "OSError"}
  ```

- `GET /products`, `GET /orders/{id}` y `GET /customers/{id}/orders` siguen en 200 con latencia normal.
- `/healthz` y `/readyz` responden 200: el readiness solo chequea Postgres y Redis, así que **el load balancer y los probes no ven el problema**.
- En Grafana:
  - **RPS por status** muestra una franja de 500 que coincide exactamente con el tráfico de `POST /orders`.
  - **Error rate y excepciones por tipo**: `exc OSError`.
  - **Disco**: la serie `uploads usado` llega a 100%. La serie del host (node_exporter) **no cambia**, porque node_exporter no ve los tmpfs de los contenedores.
  - `orders_created_total` deja de crecer.
- El loadgen reporta `http_5xx` solo en el endpoint `create_order`.
- Logs de la API:

  ```text
  2026-05-06 03:12:44,901 ERROR orders.api error no controlado en POST /orders: [Errno 28] No space left on device
  Traceback (most recent call last):
    ...
    File "/app/orders/receipts.py", line 22, in write_receipt
      fh.flush()
  OSError: [Errno 28] No space left on device
  ```

## Runbook de diagnóstico

Hipótesis iniciales razonables ante "solo fallan las escrituras": (a) locks o problema en la base que solo afecta UPDATE/INSERT, (b) validación rechazando payloads, (c) una dependencia de escritura caída, (d) falta de espacio o permisos en disco.

1. **Observar con RED, separando por endpoint y método.**

   ```bash
   curl -sS --cacert certs/ca.crt https://localhost:8443/readyz
   curl -sS -w '\n%{http_code}\n' --cacert certs/ca.crt -X POST https://localhost:8443/orders \
     -H 'Content-Type: application/json' \
     -d '{"customer_id": 1, "product_id": 1, "quantity": 1, "reference": "PO-1234-A"}'
   curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' --cacert certs/ca.crt https://localhost:8443/products/1
   ```

   500 rápido (no 503, no 504, no timeout) en la escritura y 200 en lecturas. Que falle rápido descarta locks (la hipótesis a): un lock haría esperar el request.

2. **Leer los logs y buscar el errno.**

   ```bash
   docker compose logs --since 15m api | grep -E 'ERROR|Errno' | tail -n 20
   docker compose logs --since 15m api | grep -c 'No space left on device'
   ```

   `[Errno 28] No space left on device` en `receipts.py` apunta al disco de la API, no a la base. Con eso la hipótesis (d) pasa al frente y (b) queda descartada porque la validación devuelve 422, no 500.

3. **Verificar espacio e inodos en el contenedor (USE: utilización del recurso disco).**

   ```bash
   docker compose exec api df -h /data/uploads
   docker compose exec api df -i /data/uploads
   ```

   `Use%` en 100% confirma falta de espacio. Si el espacio estuviera libre pero `IUse%` en 100% el problema serían inodos (muchos archivos chicos), que también devuelve `ENOSPC`.

4. **Encontrar qué ocupa el espacio.**

   ```bash
   docker compose exec api sh -c 'du -xh --max-depth=2 /data/uploads | sort -h | tail -n 15'
   docker compose exec api sh -c 'find /data/uploads -xdev -type f -size +10M -exec ls -lh {} \;'
   ```

   Lo esperado en operación normal es `receipts/` con algunos KB o MB. Acá aparece `/data/uploads/tmp/export-debug.bin` ocupando casi todo el volumen. La fecha del archivo (`ls -lh --time-style=full-iso`) ayuda a armar la línea de tiempo.

5. **Descartar archivos borrados pero abiertos.** Si `df` dice lleno y `du` no encuentra nada grande, el espacio puede estar retenido por un archivo borrado que un proceso todavía tiene abierto:

   ```bash
   docker compose exec api sh -c 'ls -l /proc/*/fd 2>/dev/null | grep deleted'
   ```

   En este escenario no debería aparecer nada, pero es el paso que separa "alguien llenó el disco" de "un proceso retiene un archivo borrado" (típico después de rotar logs sin reabrirlos).

6. **Revisar la presión de memoria del contenedor.** El volumen es un `tmpfs`: sus páginas cuentan contra el límite de memoria del contenedor (256 MB).

   ```bash
   docker stats --no-stream rca-lab-api-1
   ```

   Llenar 48 MB de tmpfs sube el uso de memoria del contenedor aunque el RSS del proceso no cambie. Si además hubiera una fuga de memoria, el OOM llegaría antes.

7. **Correlacionar con métricas y cambios.** Ver en Grafana cuándo saltó `uploads usado` de golpe (un archivo grande) o si fue creciendo lento (comprobantes acumulados sin retención). Un salto vertical sugiere una acción puntual, una rampa sugiere un problema de retención.

## Consultas PromQL

```promql
# Uso del volumen de uploads (0..1)
1 - uploads_disk_free_bytes{job="orders-api"} / uploads_disk_total_bytes{job="orders-api"}

# Predicción: ¿se llena en las próximas 4 horas?
predict_linear(uploads_disk_free_bytes{job="orders-api"}[1h], 4 * 3600) < 0

# Excepciones de escritura
sum by (type) (rate(app_exceptions_total{job="orders-api"}[5m]))

# Error rate solo en el alta de órdenes
sum(rate(http_requests_total{job="orders-api",method="POST",route="/orders",status=~"5.."}[5m]))
  / sum(rate(http_requests_total{job="orders-api",method="POST",route="/orders"}[5m]))

# Órdenes creadas por segundo (cae a 0)
rate(orders_created_total{job="orders-api"}[5m])

# Disco del host (no muestra el tmpfs del contenedor)
1 - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{fstype!~"tmpfs|overlay"}
```

## Confirmar la causa raíz

1. `df -h /data/uploads` en 100% y un único archivo ajeno a la operación (`tmp/export-debug.bin`) ocupando el volumen.
2. Los errores son todos `OSError` con `Errno 28` en `write_receipt`, y empezaron en el mismo momento en que `uploads_disk_free_bytes` llegó a 0.
3. Prueba controlada: liberar unos pocos MB y repetir el `POST /orders`. Debe devolver 201. Volver a llenar y debe fallar de nuevo.

   ```bash
   docker compose exec api truncate -s -5M /data/uploads/tmp/export-debug.bin
   curl -sS -w '\n%{http_code}\n' --cacert certs/ca.crt -X POST https://localhost:8443/orders \
     -H 'Content-Type: application/json' -d '{"customer_id": 1, "product_id": 1, "quantity": 1}'
   ```

## Mitigación inmediata

1. Antes de borrar, confirmar que el archivo no es un dato de negocio (en un caso real: preguntar al dueño o moverlo a otro volumen). Después liberar el espacio:

   ```bash
   docker compose exec api rm -f /data/uploads/tmp/export-debug.bin
   docker compose exec api df -h /data/uploads
   ```

   `labctl fix disk-full` hace exactamente esto.

2. Verificar que `POST /orders` vuelve a 201 y que `orders_created_total` crece.
3. Avisar a soporte: las órdenes que fallaron hicieron rollback, no quedaron a medias. Los clientes pueden reintentar.

## Solución definitiva

- **Un volumen por propósito.** Los comprobantes no comparten espacio con archivos temporales ni exports de debug. Los exports van a un volumen o bucket propio.
- **Retención y rotación** de `receipts/` (por ejemplo, subir a object storage y borrar localmente pasadas 24 h).
- **Desacoplar la escritura del alta.** Una falla del disco no debería impedir vender. Guardar el comprobante en la base (outbox) y que un proceso aparte lo materialice:

  ```python
  db.add(order)
  db.add(ReceiptOutbox(order_id=order.id, payload=order_to_dict(order)))
  db.commit()  # el archivo lo escribe un worker con reintentos
  ```

- **Cuotas y límites** para que un proceso no pueda llenar el volumen compartido.
- **Readiness más honesto**: incluir un chequeo de espacio mínimo en `/readyz` o al menos una alerta específica, porque hoy los probes no ven el problema.
- **Alertas predictivas** con `predict_linear` además del umbral fijo.

## Alertas

Definidas en `alerts.yml` de esta carpeta:

- **UploadsDiskAlmostFull** (warning): menos de 10% libre durante 2 minutos. Da margen para actuar antes de que fallen las escrituras.
- **UploadsDiskFillingUp** (warning): `predict_linear` sobre la última hora proyecta que el volumen se llena en menos de 4 horas. Detecta el crecimiento lento por falta de retención.
- **OrderWritesFailingDiskErrors** (critical): `OSError` en `app_exceptions_total` y `POST /orders` con 5xx. Es la alerta de impacto: ya se están perdiendo ventas.

## Cómo reproducir

```bash
make up-load
python -m labctl break disk-full
python -m labctl status
# ... diagnosticar con el runbook ...
python -m labctl fix disk-full
```

El volumen se llena en un par de segundos. Con el loadgen por defecto (~4 órdenes por segundo) los 500 aparecen de inmediato y `OrderWritesFailingDiskErrors` dispara en ~1 minuto. Después del fix las altas vuelven a funcionar sin reiniciar nada.
