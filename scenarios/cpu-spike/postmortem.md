# Postmortem: API congelada por backtracking catastrófico en la validación de referencias

- **Fecha del incidente:** 2026-09-03
- **Duración:** 58 min (10:22 a 11:20 UTC-3)
- **Severidad:** SEV-1
- **Estado:** cerrado, action items en curso
- **Autores:** on-call de plataforma, equipo de órdenes

## Resumen

El release 1.4.0 de `orders-api` incluyó una "simplificación" del validador del campo `reference` que quitó el límite de largo y cambió la regex por `^([A-Za-z0-9]+[-_ ]?)*$`. Esa expresión tiene backtracking exponencial ante códigos largos terminados en un caracter inválido, algo que algunos clientes mandan al pegar referencias desde planillas. Cada uno de esos requests ocupaba la CPU y el GIL durante segundos o minutos, congelando el único proceso de la API. Se mitigó con rollback del validador y reinicio del servicio.

## Impacto

- 58 minutos de servicio intermitente: ráfagas de 20 a 90 s sin respuesta en todos los endpoints.
- 41% de los requests en la ventana terminaron en 504 o timeout del cliente.
- Unas 1.900 órdenes no se pudieron crear. La mayoría se reintentó después; 140 clientes abandonaron la compra.
- Sin pérdida ni corrupción de datos.

## Línea de tiempo

| Hora (UTC-3) | Evento |
|---|---|
| 10:15 | Se activa el validador v2 como parte del release 1.4.0 |
| 10:22 | Primer request con referencia adversarial. La API deja de responder 40 s |
| 10:24 | Dispara `TargetDown` por scrapes fallidos y el probe externo cae |
| 10:27 | On-call ve `rca-lab-api-1` en 100% de un core y memoria estable en `docker stats` |
| 10:31 | Se descarta la base: Postgres y Redis sin carga, sin locks |
| 10:36 | Se reinicia la API. Se recupera 3 minutos y vuelve a congelarse |
| 10:44 | `py-spy dump --pid 1` muestra un hilo en `validate_reference_v2` → `re.match` |
| 10:49 | Access log de nginx: los requests de 30 s son `POST /orders/validate` |
| 10:55 | Se reproduce offline: el tiempo se multiplica por 4 cada 2 caracteres |
| 11:02 | Se decide rollback del validador a v1 |
| 11:08 | Validador v1 activo y reinicio de la API |
| 11:20 | 15 minutos sin congelamientos ni scrapes fallidos. Se cierra el incidente |

## Causa raíz

La regex del validador v2 anida un cuantificador (`(...)*`) sobre un grupo que contiene otro cuantificador (`[A-Za-z0-9]+`) con un separador opcional (`[-_ ]?`). Para una cadena alfanumérica de n caracteres existen 2^(n-1) formas de partirla en bloques. Cuando la cadena termina con un caracter inválido, el motor de `re` las prueba todas antes de fallar. Con 26 caracteres eso son decenas de segundos de CPU. Además se quitó el límite de largo que antes cortaba la entrada antes de llegar a la regex. Como `re` no libera el GIL y la API corre en un único proceso uvicorn, un solo request bloquea a todos los demás, incluidos `/healthz` y `/metrics`.

## 5 porqués

1. ¿Por qué la API dejó de responder? Porque un hilo tenía el GIL tomado ejecutando `re.match` durante decenas de segundos.
2. ¿Por qué `re.match` tardaba tanto? Porque la regex v2 tiene backtracking exponencial con entradas alfanuméricas largas terminadas en un caracter inválido.
3. ¿Por qué llegaron esas entradas? Porque clientes reales pegan referencias con basura al final, y la v2 eliminó el límite de 64 caracteres que las descartaba antes.
4. ¿Por qué un request afectó a todos? Porque la API corre en un solo proceso y el GIL serializa la ejecución de Python.
5. ¿Por qué el cambio pasó la revisión? Porque los tests solo cubrían casos válidos e inválidos cortos, no había pruebas de performance con entradas adversariales ni análisis estático de regex en CI.

## Qué salió bien

- `py-spy` ya estaba instalado en la imagen y con `SYS_PTRACE`, permitió ver el stack sin detener el proceso.
- El access log de nginx con `urt=` permitió encontrar el endpoint disparador rápido.
- El rollback del validador fue un cambio chico y aislado.

## Qué salió mal

- El primer reinicio se hizo sin causa identificada y solo compró 3 minutos.
- Los paneles de la API quedaron con huecos justo cuando más se los necesitaba: las métricas dependen de que el proceso responda.
- Un solo proceso uvicorn convierte cualquier bloqueo de CPU en una caída total.
- Compose marca la API como `unhealthy` pero no la reinicia, nadie miró ese estado.

## Dónde tuvimos suerte

- Las referencias adversariales eran ~1% del tráfico. Con más volumen la API no se hubiera recuperado entre ráfagas.
- Nadie usó el endpoint de validación de forma maliciosa: un atacante podía tirar el servicio con un request por minuto.

## Action items

| # | Acción | Tipo | Dueño | Prioridad | Estado |
|---|---|---|---|---|---|
| 1 | Validador con regex sin ambigüedad y límite de largo previo | Prevención | Equipo de órdenes | P0 | Hecho |
| 2 | Test de performance con entradas adversariales y análisis estático de regex en CI | Prevención | Equipo de órdenes | P1 | En curso |
| 3 | Correr la API con varios workers/réplicas para acotar el radio de impacto | Mitigación | Equipo de plataforma | P1 | Pendiente |
| 4 | Alerta de CPU por contenedor vía cAdvisor y de scrapes fallidos de la API | Detección | On-call de plataforma | P0 | Hecho |
| 5 | Límite de tamaño de body y de campos en el borde (nginx y schema) | Prevención | Equipo de plataforma | P2 | Pendiente |
| 6 | Agregar `py-spy dump` al runbook general de "API sin respuesta" | Proceso | On-call de plataforma | P2 | Hecho |

## Lecciones aprendidas

- Las regex son código ejecutable con complejidad propia. Una entrada de 30 bytes puede costar minutos de CPU.
- Limitar el largo de la entrada antes de procesarla es una defensa barata y efectiva.
- Con el GIL, mover trabajo de CPU a otro hilo no aísla nada. Lo que aísla son procesos.
- Necesitamos señales que no dependan del proceso enfermo: cAdvisor y probes externos siguieron funcionando cuando `/metrics` no.
