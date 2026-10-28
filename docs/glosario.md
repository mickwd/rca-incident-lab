# Glosario

Términos que aparecen en los runbooks y postmortems del laboratorio.

## Confiabilidad y operación

- **RCA (Root Cause Analysis)**: análisis de causa raíz. Proceso para llegar al mecanismo que produjo un incidente y a las condiciones que lo permitieron.
- **Causa raíz / causas contribuyentes**: la causa raíz es la condición sin la cual el incidente no pasa. Las contribuyentes lo agravan o lo demoran (falta de alertas, límites mal dimensionados).
- **Postmortem sin culpas (blameless)**: documento posterior al incidente que analiza sistemas y decisiones, no personas.
- **Mitigación**: acción que devuelve el servicio sin necesariamente corregir la causa (reiniciar, revertir, desactivar un flag).
- **Solución definitiva**: cambio que evita que el problema se repita.
- **MTTD / MTTR**: tiempo medio hasta detectar / hasta resolver.
- **SLI / SLO / SLA**: indicador medido (por ejemplo, porcentaje de requests exitosos), objetivo interno sobre ese indicador y compromiso contractual con clientes.
- **Presupuesto de error (error budget)**: cuánto puede fallar el servicio sin romper el SLO.
- **Runbook**: procedimiento paso a paso para diagnosticar o resolver un tipo de problema.
- **Guardia (on-call)**: persona responsable de responder alertas en un período.
- **Severidad (SEV1..SEV3)**: clasificación del impacto de un incidente.
- **RED**: Rate, Errors, Duration. Señales para servicios que atienden requests.
- **USE**: Utilization, Saturation, Errors. Señales para recursos.
- **Ishikawa / espina de pescado**: diagrama que agrupa causas posibles por categoría.
- **5 porqués**: técnica de preguntar "¿por qué?" de forma encadenada para pasar del síntoma a la causa.
- **Liveness / readiness**: chequeo de "el proceso está vivo" contra "el proceso puede atender tráfico". Mezclarlos genera reinicios innecesarios o tráfico hacia instancias rotas.
- **Monitoreo sintético**: probes que simulan un cliente (en el laboratorio, blackbox exporter).

## Métricas y observabilidad

- **Percentil (p50, p95, p99)**: valor por debajo del cual cae ese porcentaje de las mediciones. El p99 muestra la experiencia de los requests más lentos.
- **Histograma**: métrica de Prometheus que cuenta observaciones por buckets. `histogram_quantile` estima percentiles a partir de ellos.
- **Counter / Gauge**: métrica que solo sube (se usa con `rate`) / métrica que sube y baja.
- **Cardinalidad**: cantidad de combinaciones de labels. Por eso la métrica usa la ruta plantilla (`/orders/{order_id}`) y no la URL real.
- **Scrape**: lectura periódica de `/metrics` por parte de Prometheus. Si el proceso está congelado el scrape falla y `up` vale 0.
- **PromQL**: lenguaje de consultas de Prometheus.
- **Exporter**: proceso que traduce métricas de un sistema (Postgres, Redis, el host) a formato Prometheus.
- **cAdvisor**: exporter de métricas por contenedor (CPU, memoria, límites).
- **Blackbox exporter**: hace probes HTTP/TLS/DNS desde afuera, como un cliente.

## Aplicación y base de datos

- **Pool de conexiones**: conjunto de conexiones reutilizables a la base. SQLAlchemy usa `QueuePool` con `pool_size` + `max_overflow` y espera `pool_timeout` segundos antes de fallar.
- **Fuga de conexiones**: conexiones que se piden al pool y nunca se devuelven.
- **`idle in transaction`**: sesión de Postgres con una transacción abierta que no está ejecutando nada. Retiene locks y snapshots.
- **Lock de fila (`FOR UPDATE`)**: bloqueo que impide que otras transacciones modifiquen esas filas hasta el commit o rollback.
- **`pg_stat_activity`**: vista con una fila por sesión: estado, query, tiempos y en qué está esperando.
- **`pg_locks` / `pg_blocking_pids()`**: locks actuales y qué procesos bloquean a cuál.
- **`pg_stat_statements`**: extensión que acumula estadísticas por query normalizada (llamadas, tiempo total y medio).
- **`EXPLAIN ANALYZE`**: muestra el plan de ejecución real de una consulta con tiempos y filas.
- **Seq Scan / Index Scan**: lectura de la tabla completa contra lectura guiada por un índice.
- **`CREATE INDEX CONCURRENTLY`**: crea un índice sin bloquear escrituras. No puede ejecutarse dentro de una transacción.
- **ReDoS**: denegación de servicio por expresión regular. Una regex con cuantificadores anidados ambiguos tarda tiempo exponencial con ciertas entradas.
- **Backtracking catastrófico**: el motor de regex prueba todas las formas posibles de partir la entrada antes de fallar.
- **GIL**: Global Interpreter Lock de CPython. Un solo hilo ejecuta bytecode Python a la vez. El módulo `re` no lo libera mientras matchea.
- **py-spy**: profiler de muestreo para procesos Python en ejecución. `py-spy dump` muestra el stack de cada hilo sin detener el proceso.
- **Fuga de memoria**: memoria que el proceso retiene y nunca libera, normalmente por referencias vivas en estructuras globales.
- **RSS (Resident Set Size)**: memoria física que ocupa el proceso.

## Infraestructura y red

- **OOM killer**: mecanismo del kernel que mata procesos cuando un cgroup supera su límite de memoria. Docker lo reporta como `OOMKilled: true` y código de salida 137.
- **cgroup**: mecanismo del kernel que limita recursos de un grupo de procesos (lo que usa Docker para `mem_limit`).
- **tmpfs**: sistema de archivos en memoria. En el laboratorio se usa para `/data/uploads` con tamaño fijo, y sus páginas cuentan contra la memoria del contenedor.
- **ENOSPC**: código de error "No space left on device".
- **Inodos**: estructuras que representan archivos. Un disco puede quedarse sin inodos con espacio libre (`df -i`).
- **TLS / handshake**: protocolo de cifrado y la negociación inicial donde el cliente valida el certificado.
- **CA (autoridad certificante)**: emisor en el que confía el cliente. El laboratorio genera la suya (`certs/ca.crt`).
- **notBefore / notAfter**: ventana de validez del certificado.
- **SAN (Subject Alternative Name)**: nombres para los que vale el certificado.
- **DNS interno de Docker**: resolver en `127.0.0.11` que traduce nombres de servicios a IPs dentro de la red del proyecto.
- **`getent hosts` / `dig` / `nslookup`**: herramientas para probar resolución de nombres desde adentro de un contenedor.
- **Reverse proxy**: nginx recibe los requests de los clientes, termina TLS y los reenvía a la API.
- **502 / 503 / 504**: upstream respondió mal o no está / servicio no disponible / upstream no respondió a tiempo.
