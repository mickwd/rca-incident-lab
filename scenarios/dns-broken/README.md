# La API no resuelve el host de la base de datos

Después de un cambio de configuración la API arranca, pasa su health check y queda `healthy`, pero cada request que toca Postgres falla. El proceso está vivo y el contenedor no se reinicia, así que a primera vista "está todo arriba". El problema es de resolución de nombres: la API apunta a un hostname que no existe en la red de Docker.

**Categoría** network · **Dificultad** media · **Herramientas** `docker compose config`, `docker inspect`, `getent`, `dig`, `nslookup`, `docker network inspect`

## Síntomas

- Los endpoints que usan la base devuelven 503:

  ```text
  $ curl -s --cacert certs/ca.crt https://localhost:8443/customers/42/orders
  {"detail":"internal error","error":"OperationalError"}
  ```

- `/readyz` falla pero `/healthz` responde 200:

  ```text
  $ curl -s --cacert certs/ca.crt https://localhost:8443/readyz
  {"status":"degraded","checks":{"postgres":"fail","redis":"ok"}}
  ```

- En los logs de la API:

  ```text
  ERROR orders.api error no controlado en GET /orders/123: (psycopg.OperationalError) [Errno -2] Name or service not known
  ERROR orders.api readyz: postgres no disponible: (psycopg.OperationalError) failed to resolve host 'orders-db.internal': [Errno -2] Name or service not known
  ```

- Falla parcial: `GET /products/{id}` puede seguir respondiendo 200 para productos que están en el cache de Redis, lo que confunde ("algunas cosas andan").
- `docker compose ps` muestra `api` como `healthy` y sin reinicios: el health check de Docker solo mira `/healthz` (liveness), que no toca dependencias.
- En Grafana: error rate alto concentrado en `OperationalError`, `app_dependency_up{dependency="postgres"} = 0` y `probe_success{job="blackbox-readyz"} = 0`.

## Runbook de diagnóstico

1. **Observar el alcance.** ¿Fallan todos los endpoints o solo los que tocan una dependencia?

   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" --cacert certs/ca.crt https://localhost:8443/healthz
   curl -s --cacert certs/ca.crt https://localhost:8443/readyz
   docker compose ps
   ```

   `healthz` 200 + `readyz` 503 con `postgres: fail` apunta a la conexión con la base, no al proceso.

2. **Leer el error exacto.** El tipo de excepción y el mensaje acotan mucho las hipótesis.

   ```bash
   docker compose logs --since=10m api | grep -iE "operationalerror|resolve|name or service" | tail -20
   ```

   Hipótesis posibles: Postgres caído, credenciales, red, DNS. `Name or service not known` / `failed to resolve host` descarta credenciales y Postgres caído: ni siquiera se llegó a abrir el socket.

3. **Verificar que Postgres esté sano por su cuenta.**

   ```bash
   docker compose ps postgres
   docker compose exec postgres pg_isready -h 127.0.0.1
   ```

   Si Postgres responde, el problema está del lado del cliente (la API).

4. **Ver qué hostname está usando la API realmente.** La configuración efectiva del contenedor puede no coincidir con lo que dice el repo.

   ```bash
   docker compose exec api env | grep ^DB_
   docker inspect rca-lab-api-1 --format '{{json .Config.Env}}'
   docker compose config api | grep -A12 environment
   ```

   Compará `DB_HOST` del contenedor (`orders-db.internal`) con el valor esperado (`postgres`).

5. **Probar la resolución desde adentro del contenedor.** Docker resuelve nombres de servicio con su DNS interno `127.0.0.11`.

   ```bash
   docker compose exec api cat /etc/resolv.conf
   docker compose exec api getent hosts orders-db.internal   # sin salida, rc=2
   docker compose exec api getent hosts postgres             # 172.x.x.x postgres
   docker compose exec api dig +short orders-db.internal @127.0.0.11
   docker compose exec api nslookup postgres
   ```

6. **Aislar: confirmar qué nombres existen en la red.**

   ```bash
   docker network inspect rca-lab_default \
     --format '{{range .Containers}}{{.Name}} {{.IPv4Address}}{{println}}{{end}}'
   docker inspect rca-lab-postgres-1 \
     --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}: {{$v.Aliases}}{{println}}{{end}}'
   ```

   Ningún contenedor tiene el alias `orders-db.internal`.

7. **Encontrar el origen del cambio.** Comparar la configuración base con la que se desplegó.

   ```bash
   docker compose config > /tmp/base.yml
   docker compose -f docker-compose.yml -f scenarios/dns-broken/compose.fault.yml config > /tmp/desplegado.yml
   diff /tmp/base.yml /tmp/desplegado.yml
   ```

Nota: el engine usa `pool_pre_ping=True` y `connect_timeout=5`. Con un nombre que no resuelve el error es inmediato (no hay timeout de 5 s), por eso la latencia de los 503 es baja: un error rápido suele indicar DNS o conexión rechazada, uno lento suele indicar timeout de red o pool.

## Consultas PromQL

```promql
# Readiness vista desde afuera
probe_success{job="blackbox-readyz"}

# Último chequeo de dependencias que hizo /readyz
app_dependency_up{job="orders-api"}

# Excepciones por tipo: OperationalError domina
sum by (type) (rate(app_exceptions_total{job="orders-api"}[5m]))

# Error rate por endpoint: los que tocan la base fallan, los cacheados no
sum by (route) (rate(http_requests_total{job="orders-api",status=~"5.."}[5m]))
  / sum by (route) (rate(http_requests_total{job="orders-api"}[5m]))

# El proceso sigue vivo (descarta crash)
up{job="orders-api"}
```

## Confirmar la causa raíz

1. `docker compose exec api env | grep DB_HOST` muestra `orders-db.internal`.
2. `getent hosts orders-db.internal` dentro del contenedor no devuelve nada y `getent hosts postgres` sí.
3. Al recrear la API con la configuración base, `/readyz` vuelve a 200 sin tocar Postgres.

## Mitigación inmediata

Volver a la configuración conocida y recrear solo la API:

```bash
labctl fix dns-broken
# equivalente a:
docker compose up -d --no-deps --force-recreate api
```

Verificar:

```bash
docker compose exec api env | grep DB_HOST
curl -s --cacert certs/ca.crt https://localhost:8443/readyz
```

## Solución definitiva

- Validar la configuración en el arranque: si `DB_HOST` no resuelve, loguear un error claro y no marcar la instancia como lista.
- Smoke test post-deploy contra `/readyz` (no contra `/healthz`) que bloquee el rollout si falla.
- Rollouts canary o por readiness: la versión nueva no recibe tráfico hasta que sus dependencias responden.
- Configuración como código con revisión obligatoria y un diff visible de variables de entorno en cada deploy.
- Alerta sobre el probe de readyz y sobre `app_dependency_up`.
- Documentar los nombres DNS de cada dependencia (servicio, alias, entorno) en un único lugar.

## Alertas

Definidas en [`alerts.yml`](alerts.yml):

| Alerta | Detecta |
|---|---|
| `ApiNotReady` | El probe externo de `/readyz` falla |
| `ApiDependencyDown` | `/readyz` reporta una dependencia caída |
| `ApiDatabaseOperationalErrors` | Excepciones `OperationalError` sostenidas en la API |

## Cómo reproducir

```bash
make up-load
labctl break dns-broken      # recrea la API con DB_HOST=orders-db.internal
# diagnosticar con el runbook
labctl fix dns-broken
```

En modo desafío: `labctl break random` y después `labctl status --reveal`.
