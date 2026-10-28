# CPU al 100% por regex vulnerable (ReDoS)

Un cambio en la validación del campo `reference` introdujo una expresión regular con backtracking catastrófico. Ciertas entradas que el tráfico real ya trae (un código largo con un caracter inválido al final) tardan segundos o minutos en evaluarse. El módulo `re` de Python no suelta el GIL mientras matchea, así que el único proceso uvicorn se congela entero: no responde ni `/healthz` ni `/metrics`.

**Categoría:** app · **Dificultad:** media-alta · **Herramientas:** `docker stats`, `top -H`, `py-spy`, logs de nginx, Prometheus, Python

## Síntomas

- Lo que ve el usuario: toda la API deja de responder por ráfagas de decenas de segundos. nginx devuelve **504 Gateway Timeout** después de 30 s. Entre ráfagas todo parece normal.
- Dashboard *RCA Lab - Overview*:
  - Latencia p50/p95/p99 de **todas** las rutas sube a la vez (no es un endpoint puntual).
  - Huecos en los paneles de la API: Prometheus no puede scrapear `orders-api` (timeout de 4 s), `up{job="orders-api"}` alterna entre 0 y 1.
  - "CPU": `rca-lab-api-1` clavado cerca de 1 core (100% de un núcleo), el resto del stack tranquilo.
  - "Probes blackbox": `probe_success` de `blackbox-https` cae.
  - Memoria, conexiones de DB y disco normales.
- `docker compose ps` muestra la API como `unhealthy` (el healthcheck `curl /healthz` no contesta en 3 s). Compose no la reinicia por estar unhealthy, solo cambia el estado.
- En los logs de la API no hay excepciones: el proceso no falla, está ocupado.

## Runbook de diagnóstico

1. **Observar (RED).** ¿Afecta a una ruta o a todas? Si todas suben juntas y los errores son 504/timeouts, el proceso entero está trabado, no una dependencia de un endpoint.

   ```bash
   for p in /healthz /products/1 /customers/10/orders; do
     curl -s -o /dev/null -m 10 -w "$p %{http_code} %{time_total}s\n" --cacert certs/ca.crt "https://localhost:8443$p"
   done
   ```

2. **Recursos (USE).** CPU, memoria, red por contenedor:

   ```bash
   docker stats --no-stream
   docker compose ps
   ```

   La API usa ~100% CPU (un core) con memoria estable. Postgres y Redis sin carga. Hipótesis: la API está consumiendo CPU en su propio código, no esperando a nadie.

3. **Hilos dentro del contenedor.** ¿Un solo hilo o muchos?

   ```bash
   docker compose exec api top -H -b -n 1 | head -20
   docker compose exec api ps -o pid,nlwp,pcpu,rss,etime,cmd -p 1
   ```

   Un hilo del proceso uvicorn concentra la CPU. Al ser Python, eso implica GIL tomado por ese hilo.

4. **Probar: qué está ejecutando.** `py-spy` muestra el stack de cada hilo sin detener el proceso (el contenedor tiene `SYS_PTRACE`):

   ```bash
   docker compose exec api py-spy dump --pid 1
   docker compose exec api py-spy top --pid 1      # Ctrl+C para salir
   ```

   Buscar un hilo con `validate_reference_v2 (orders/validation.py)` llamado desde `validate`/`create_order` (`orders/api.py`) y dentro de `re.match`. El resto de los hilos esperan el GIL.

5. **Aislar el disparador.** Qué requests coinciden con los congelamientos. En el access log de nginx `urt=` es el tiempo del upstream:

   ```bash
   docker compose logs --since 15m nginx | grep -E 'urt=(2[0-9]|30)\.' | head
   docker compose logs --since 15m nginx | grep ' 504 ' | awk '{print $4, $5}' | sort | uniq -c | sort -rn | head
   ```

   Los requests que tardan 30 s son `POST /orders/validate` (y eventualmente `POST /orders` con `reference`). Los 504 de otras rutas son víctimas, no causa.

6. **Correlacionar con cambios.** Revisar qué cambió en la validación: deploys recientes, flags activos y el diff de `orders/validation.py`:

   ```bash
   curl -s --cacert certs/ca.crt -H "X-Admin-Token: ${ADMIN_TOKEN:-change-me}" https://localhost:8443/admin/faults
   ```

7. **Reproducir fuera de producción.** Medir la regex con entradas crecientes. Si el tiempo se multiplica por ~4 cada 2 caracteres, es backtracking exponencial:

   ```bash
   python -c "
   import re, time
   p = re.compile(r'^([A-Za-z0-9]+[-_ ]?)*$')
   for n in (16, 18, 20, 22, 24):
       t = time.perf_counter(); p.match('A' * n + '#'); print(n, round(time.perf_counter() - t, 3), 's')
   "
   ```

## Consultas PromQL

```promql
# CPU del proceso de la API en cores (con huecos si el scrape falla)
rate(process_cpu_seconds_total{job="orders-api"}[1m])

# CPU del contenedor medida por cAdvisor (no depende de que la API responda)
rate(container_cpu_usage_seconds_total{name="rca-lab-api-1"}[1m])

# Scrapes fallidos de la API en los últimos 10 minutos
count_over_time((up{job="orders-api"} == 0)[10m:5s])

# Latencia p99 por ruta: sube en todas a la vez
histogram_quantile(0.99, sum by (le, route) (rate(http_request_duration_seconds_bucket{job="orders-api"}[5m])))

# Ruta disparadora: duración del validador
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job="orders-api",route="/orders/validate"}[5m])))

# Requests en curso apilados mientras el GIL está tomado
http_requests_in_progress{job="orders-api"}

# Probe externo
probe_success{job="blackbox-https"}
```

## Confirmar la causa raíz

1. `py-spy dump` muestra repetidamente un hilo dentro de `validate_reference_v2` → `re.match` mientras la CPU está al 100%.
2. La regex `^([A-Za-z0-9]+[-_ ]?)*$` tiene un cuantificador anidado con separador opcional: una cadena alfanumérica de largo n puede partirse de 2^(n-1) formas y, si al final hay un caracter inválido, el motor prueba todas.
3. La reproducción offline muestra crecimiento exponencial (0,18 s con 20 caracteres, 2,9 s con 24).
4. Con el validador v1 la misma entrada se rechaza en microsegundos y los congelamientos desaparecen.

Descartar: GC o memoria (RSS estable), una dependencia lenta (Postgres y Redis sin carga, el hilo no está en I/O) y loops en otras rutas.

## Mitigación inmediata

Volver al validador anterior y reiniciar la API. Reiniciar es necesario: un hilo que está dentro de `re.match` no se puede cancelar desde Python.

```bash
labctl fix cpu-spike
```

Equivalente manual:

```bash
docker compose exec -T redis redis-cli DEL fault:cpu-spike
docker compose restart api
```

Si no hay rollback disponible, una mitigación de borde es bloquear en nginx el endpoint `POST /orders/validate` o limitar el tamaño de `reference` antes de que llegue a la aplicación.

## Solución definitiva

- Regex sin ambigüedad: separador obligatorio entre bloques y límite de largo **antes** de evaluar la regex:

  ```python
  MAX_REFERENCE_LENGTH = 64
  SAFE_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:[-_ ][A-Za-z0-9]+)*$")

  def validate_reference_v1(value: str) -> bool:
      if not value or len(value) > MAX_REFERENCE_LENGTH:
          return False
      return SAFE_PATTERN.match(value) is not None
  ```

- Si la regex tiene que tolerar ambigüedad, usar grupos atómicos o cuantificadores posesivos (Python 3.11+): `^(?>[A-Za-z0-9]+[-_ ]?)*$` o `^(?:[A-Za-z0-9]++[-_ ]?)*+$`. Otra opción es un motor de tiempo lineal como RE2 (`google-re2`).
- Mover la validación a un executor no alcanza: el GIL se sigue bloqueando. Lo que limita el radio de impacto es correr varios procesos (`--workers` o réplicas) y que un proceso trabado no tome a los demás.
- Tests de performance de regex: el repo tiene un test que mide el crecimiento del tiempo con entradas adversariales. Sumar fuzzing/property tests y análisis estático de regex en CI.
- Validar largo máximo también en el borde (nginx `client_max_body_size`, schema de la API).

## Alertas

Definidas en [`alerts.yml`](alerts.yml):

| Alerta | Señal | Severidad |
|---|---|---|
| `ApiContainerCpuSaturated` | CPU del contenedor de la API > 0,9 cores por 2m | warning |
| `ApiScrapeFlapping` | 3 o más scrapes fallidos de `orders-api` en 5m | critical |
| `ValidateEndpointSlow` | p99 de `/orders/validate` > 1 s por 2m | warning |

## Cómo reproducir

```bash
make up-load               # el loadgen manda 1% de referencias "raras"
labctl break cpu-spike     # activa el validador v2 y manda 3 entradas adversariales
# seguir el runbook mientras la API se congela por ráfagas
labctl fix cpu-spike
```
