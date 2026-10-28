# Fuga de memoria en la API hasta OOM kill

La memoria residente de `orders-api` crece de forma lineal con el tráfico hasta que el contenedor supera su `mem_limit` (256 MiB), el kernel lo mata y Docker lo vuelve a levantar. Mientras la causa siga presente el ciclo se repite: el proceso arranca, vuelve a crecer y se cae otra vez.

**Categoría:** app · **Dificultad:** media · **Herramientas:** `docker stats`, `docker inspect`, `docker events`, `ps`/`top`, `py-spy`, Prometheus, Grafana

## Síntomas

- Desde el cliente aparecen ráfagas de `502 Bad Gateway` cada pocos minutos que duran algunos segundos y después todo "se arregla solo".
- En Grafana (dashboard *RCA Lab - Overview*) el panel **Memoria** muestra un diente de sierra: el RSS de `orders-api` sube en rampa hasta ~256 MiB y cae de golpe. El panel **Reinicios y uptime de la API** muestra el uptime volviendo a cero.
- El panel **Vista del cliente (loadgen)** muestra picos de `http_5xx` alineados con las caídas.
- `docker compose ps` muestra la API con pocos segundos de vida aunque el stack lleve horas arriba.
- Logs de nginx durante el reinicio:

```text
2026/03/12 14:07:31 [error] 29#29: *48211 connect() failed (111: Connection refused) while connecting to upstream, client: 172.20.0.12, server: localhost, request: "GET /products/57 HTTP/1.1", upstream: "http://172.20.0.5:8000/products/57"
172.20.0.12 [12/Mar/2026:14:07:31 +0000] "GET /products/57 HTTP/1.1" 502 157 rt=0.001 urt=0.000 ust=502 rid=3f1c0b...
```

- Logs de la API: no hay ningún error ni traceback antes de la caída (un SIGKILL no deja escribir nada). Solo se ve el arranque repetido:

```text
INFO:     Started server process [1]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

La ausencia de un traceback es en sí una pista: el proceso no terminó por una excepción, lo mataron desde afuera.

## Runbook de diagnóstico

Seguimos el ciclo observar → hipótesis → probar → aislar. Primero RED (requests, errores, duración) para ubicar el impacto, después USE (utilización, saturación, errores) sobre los recursos del contenedor sospechoso.

1. **Observar el estado de los contenedores.** Buscamos qué servicio se reinicia.

   ```bash
   docker compose ps
   docker compose ps api --format 'table {{.Name}}\t{{.Status}}\t{{.RunningFor}}'
   ```

   Si `api` dice `Up 40 seconds` y el resto `Up 2 hours`, el problema está en ese contenedor. Hipótesis iniciales: crash por excepción, healthcheck fallando, OOM kill, alguien haciendo `docker restart`.

2. **Ver cómo terminó el último proceso.** Esto descarta o confirma el OOM en un solo comando.

   ```bash
   docker inspect rca-lab-api-1 --format '{{.State.OOMKilled}} {{.State.ExitCode}} {{.RestartCount}} {{.State.StartedAt}}'
   ```

   `true 137 4 ...` significa: lo mató el OOM killer del cgroup (137 = 128 + SIGKILL) y ya se reinició 4 veces. Un `false 1` apuntaría a una excepción en el arranque, un `false 137` a un SIGKILL externo (por ejemplo un healthcheck de un orquestador).

3. **Confirmar con eventos de Docker y del kernel.**

   ```bash
   docker events --since 30m --filter container=rca-lab-api-1 --filter event=oom --filter event=die
   # En Linux (o dentro de la VM de Docker Desktop)
   sudo dmesg -T | grep -iE 'oom|killed process' | tail
   ```

   Esperado: `container oom` seguido de `container die ... exitCode=137`, y en `dmesg` algo como `Memory cgroup out of memory: Killed process 31337 (uvicorn)`.

4. **Medir la utilización (USE) en vivo.** Ahora sabemos que es memoria; queremos ver si crece de forma sostenida (fuga) o en picos (un request puntual enorme).

   ```bash
   docker stats --no-stream rca-lab-api-1
   # Repetir cada 10s y comparar
   for i in 1 2 3 4 5 6; do docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.MemPerc}}' rca-lab-api-1; sleep 10; done
   ```

   Una rampa estable (por ejemplo +12 MiB cada 10 s) con tráfico constante es la firma de una fuga proporcional al tráfico.

5. **Mirar adentro del contenedor.** Separamos memoria del proceso Python de otros consumidores del cgroup (el tmpfs de `/data/uploads` también cuenta contra el límite).

   ```bash
   docker compose exec api ps -o pid,rss,vsz,etime,cmd -p 1
   docker compose exec api top -b -n 1 | head -15
   docker compose exec api df -h /data/uploads
   ```

   Si el RSS de PID 1 explica casi todo el uso y el tmpfs está casi vacío, la memoria la retiene el proceso de la API.

6. **Correlacionar con el tráfico (RED).** ¿La pendiente depende del tráfico y de qué endpoint? Usá las consultas de la sección siguiente. Si al frenar el loadgen (`docker compose stop loadgen`) la pendiente se aplana, la fuga es por request. Comparando el RPS por ruta con la pendiente vas a ver que solo correlaciona `GET /products/{product_id}`.

7. **Ver qué está haciendo el proceso.**

   ```bash
   docker compose exec api py-spy dump --pid 1
   ```

   En una fuga normalmente no hay nada "trabado", pero el dump muestra los frames de los requests en curso. Si aparece `_memoize_response` (`orders/api.py`) en hilos de `get_product`, ya tenemos el sospechoso: alguien guarda algo en memoria en ese camino.

8. **Aislar en el código.** Revisar el handler y lo que hace con el resultado:

   ```bash
   docker compose exec api grep -n "response_memo\|_memoize_response" -r /app/orders
   docker compose exec redis redis-cli --scan --pattern 'fault:*'
   ```

   `response_memo` es un `dict` global (vive en `LabState`) cuya clave incluye el `X-Request-ID`. nginx genera un id distinto en cada request, así que nunca hay un hit y nunca se borra nada. El flag `fault:memory-leak` activa ese camino.

## Consultas PromQL

```promql
# RSS de la API y su pendiente (bytes por segundo)
process_resident_memory_bytes{job="orders-api"}
deriv(process_resident_memory_bytes{job="orders-api"}[5m])

# Uso respecto del límite del contenedor (cadvisor)
container_memory_working_set_bytes{name="rca-lab-api-1"}
  / container_spec_memory_limit_bytes{name="rca-lab-api-1"}

# Reinicios: cada cambio de process_start_time_seconds es un proceso nuevo
changes(process_start_time_seconds{job="orders-api"}[15m])
time() - process_start_time_seconds{job="orders-api"}

# Qué ruta correlaciona con el crecimiento
sum by (route) (rate(http_requests_total{job="orders-api"}[5m]))

# Impacto visto por el cliente
sum by (outcome) (rate(loadgen_requests_total[1m]))
sum(rate(http_requests_total{job="orders-api",status=~"5.."}[5m]))
```

## Confirmar la causa raíz

La evidencia decisiva es la combinación de:

1. `docker inspect` con `OOMKilled=true`, `ExitCode=137` y `RestartCount` creciendo.
2. Pendiente de `process_resident_memory_bytes` proporcional al RPS de `GET /products/{product_id}` (si cortás solo ese tráfico, la pendiente desaparece).
3. El flag `fault:memory-leak` presente en Redis y el código de `_memoize_response` guardando un buffer por request con una clave que nunca se repite.

Prueba controlada: con el loadgen apagado, hacé 200 requests a un mismo producto y mirá el RSS antes y después.

```bash
docker compose exec api ps -o rss= -p 1
for i in $(seq 1 200); do curl -s -o /dev/null --cacert certs/ca.crt https://localhost:8443/products/1; done
docker compose exec api ps -o rss= -p 1
```

Con ~256 KiB por request el RSS sube unos 50 MiB, aunque siempre se pidió el mismo producto.

## Mitigación inmediata

1. Apagar el camino con la fuga (desactivar el flag / rollback del cambio):

   ```bash
   docker compose exec redis redis-cli DEL fault:memory-leak
   ```

2. Reiniciar la API para liberar lo que ya está retenido (la memoria no vuelve sola):

   ```bash
   docker compose restart api
   ```

`labctl fix memory-leak` hace exactamente esos dos pasos. Si no se puede desactivar el cambio rápido, un paliativo es subir temporalmente `mem_limit` para espaciar los reinicios mientras se prepara el rollback, nunca como solución.

## Solución definitiva

- **Cache acotado y con una clave correcta.** Un cache en proceso siempre tiene que tener tamaño máximo y TTL, y la clave debe identificar el recurso, no el request.

  ```python
  from cachetools import TTLCache

  # Máximo 1000 productos, 30 segundos de vida
  product_memo: TTLCache[int, dict] = TTLCache(maxsize=1000, ttl=30)

  def get_product_cached(product_id: int) -> dict:
      if (data := product_memo.get(product_id)) is None:
          data = load_product(product_id)
          product_memo[product_id] = data
      return data
  ```

  En este servicio ya existe el cache de Redis (`RedisProductCache`) con TTL, así que la memo local directamente sobra.
- **Revisión de código:** cualquier `dict`/`list` a nivel módulo o en estado global de la app que crece en un handler es una bandera roja. Agregarlo al checklist de PR.
- **Pruebas de carga con perfil de memoria** antes de desplegar: correr el loadgen 15 minutos y verificar que `deriv(process_resident_memory_bytes[5m])` vuelve a ~0. Para encontrar el origen, `tracemalloc` (`tracemalloc.take_snapshot().statistics("lineno")`) expuesto en un endpoint de debug protegido.
- **Detección temprana:** alertar por pendiente de memoria y por reinicios, no solo por el OOM.

## Alertas

Definidas en [`alerts.yml`](alerts.yml):

- **ApiMemoryNearLimit** (warning): el working set del contenedor supera el 85% de su límite durante 2 minutos. Avisa antes del OOM kill.
- **ApiMemoryGrowing** (warning): el RSS crece más de 200 KB/s sostenido durante 10 minutos. Detecta la fuga aunque todavía esté lejos del límite. En el laboratorio la fuga es tan rápida que el OOM llega antes de los 10 minutos y disparan primero las otras dos. Esta regla está pensada para la fuga lenta típica de producción, la que tarda días en matar el proceso.
- **ApiRestartLoop** (critical): el proceso se reinició 2 o más veces en 15 minutos (`process_start_time_seconds` cambió). Es el síntoma de crash loop que ven los usuarios como 502 intermitentes.

## Cómo reproducir

```bash
make up-load                       # stack + generador de carga (20 rps)
python -m labctl break memory-leak # activa fault:memory-leak=262144
```

Con la carga por defecto (~5 rps a `GET /products/{id}`) el RSS crece ~1,3 MB/s: el primer OOM kill llega en 2 a 3 minutos y desde ahí se repite mientras el flag siga activo. Para revertir:

```bash
python -m labctl fix memory-leak
```
