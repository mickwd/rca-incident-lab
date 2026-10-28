# Postmortem: <título corto que describa el impacto, no la causa>

> Plantilla sin culpas. Se describen sistemas, decisiones e información disponible en cada momento. No se nombran personas como causa: si alguien se equivocó, la pregunta es qué permitió que el error llegara a producción.

| Campo | Valor |
|---|---|
| Fecha | AAAA-MM-DD |
| Duración | desde la primera señal hasta la resolución |
| Severidad | SEV1 / SEV2 / SEV3 |
| Estado | Borrador / En revisión / Cerrado |
| Responsable del incidente | rol (por ejemplo "guardia de Plataforma") |
| Autores | roles |

## Resumen

Tres o cuatro oraciones: qué se rompió, a quién afectó, cuánto duró, cuál fue la causa raíz y cómo se resolvió. Tiene que entenderse sin leer el resto.

## Impacto

- Usuarios o clientes afectados (cantidad o porcentaje).
- Requests fallidos o degradados, con números de las métricas.
- Datos perdidos o inconsistentes (o confirmar que no hubo).
- Impacto en SLO: cuánto presupuesto de error se consumió.

## Línea de tiempo

Todas las horas en la misma zona horaria (indicarla). Incluir cuándo empezó el problema, no solo cuándo se detectó.

| Hora (UTC-3) | Evento |
|---|---|
| 10:00 | Cambio que dispara el problema (deploy, flag, migración) |
| 10:07 | Primera señal en métricas (aunque nadie la haya visto) |
| 10:12 | Alerta o reporte que dispara la respuesta |
| 10:15 | Se declara el incidente, se asigna responsable |
| 10:25 | Hipótesis descartadas y confirmada |
| 10:30 | Mitigación aplicada |
| 10:40 | Métricas normalizadas, se cierra el incidente |

## Causa raíz

Explicación técnica del mecanismo: qué condición, en qué componente, por qué produjo estos síntomas. Incluir la evidencia que lo confirmó (salida de comandos, queries, gráficos).

## 5 porqués

1. ¿Por qué ...? Porque ...
2. ¿Por qué ...? Porque ...
3. ¿Por qué ...? Porque ...
4. ¿Por qué ...? Porque ...
5. ¿Por qué ...? Porque ...

## Qué salió bien

- Lo que funcionó y conviene mantener (alertas, runbooks, decisiones).

## Qué salió mal

- Lo que demoró la detección, el diagnóstico o la mitigación.

## Dónde tuvimos suerte

- Factores que redujeron el impacto sin que los hayamos diseñado. Son riesgos latentes.

## Action items

Cada acción con dueño (equipo o rol) y prioridad. Tipos: Prevención, Detección, Mitigación, Proceso.

| # | Acción | Tipo | Dueño | Prioridad | Estado |
|---|---|---|---|---|---|
| 1 | Acción concreta y verificable | Prevención | Equipo X | P0 | Pendiente |
| 2 | Alerta sobre la señal que vimos tarde | Detección | Equipo SRE | P1 | Pendiente |
| 3 | Runbook actualizado con los comandos de este incidente | Proceso | Guardia | P2 | Pendiente |

Prioridades: P0 antes de cerrar el postmortem, P1 en el sprint, P2 en el trimestre.

## Lecciones aprendidas

Ideas que valen para otros sistemas, más allá de este incidente.
