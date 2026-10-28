# Postmortem: certificado TLS vencido en el proxy de entrada

- **Fecha del incidente** 2026-03-14
- **Duración** 1 h 52 min (09:03 a 10:55 UTC-3)
- **Severidad** SEV-1
- **Estado** Cerrado, action items en curso
- **Autores** On-call de SRE, líder técnico de plataforma

## Resumen

El certificado TLS que sirve nginx venció a las 09:00 UTC-3. Desde ese momento todos los clientes que validan la cadena (app mobile, integraciones de partners, navegadores) fallaron en el handshake. La API siguió sana y sin errores, por lo que las alertas basadas en métricas del servidor no dispararon. El incidente se detectó por tickets de soporte. Se mitigó emitiendo un certificado nuevo y recargando nginx.

## Impacto

- Aproximadamente 100% de los clientes externos sin servicio durante 1 h 52 min.
- Unas 4.100 órdenes no se pudieron crear (estimado por comparación con el mismo día de la semana anterior).
- 37 tickets de soporte y dos partners escalaron por canal comercial.
- No hubo pérdida ni corrupción de datos.

## Línea de tiempo

| Hora (UTC-3) | Evento |
|---|---|
| 09:00 | Vence el certificado de `nginx`. |
| 09:03 | El RPS de `orders-api` cae un 95%. Ninguna alerta dispara: error rate 0% y latencia normal. |
| 09:25 | Soporte recibe los primeros tickets "la app no conecta". |
| 09:41 | Soporte escala al on-call de SRE. |
| 09:48 | El on-call revisa Grafana: API sana. Sospecha de la red del proveedor. |
| 10:10 | Se prueba desde afuera con `curl -k` y funciona, lo que retrasa el diagnóstico. |
| 10:27 | Se repite sin `-k`: `curl: (60) SSL certificate problem: certificate has expired`. |
| 10:31 | `openssl s_client` confirma `Verify return code: 10`. Se declara SEV-1. |
| 10:44 | Se emite certificado nuevo y se hace `nginx -s reload`. |
| 10:55 | El tráfico vuelve a niveles normales. Se cierra la mitigación. |

## Causa raíz

El certificado del proxy de entrada se renovaba de forma manual una vez por año. La renovación anterior la hizo una persona que ya no estaba en el equipo y el recordatorio de calendario estaba asociado a su usuario. No existía alerta de vencimiento ni un monitoreo sintético que validara la cadena: el único health check externo usaba `insecure_skip_verify`.

## 5 porqués

1. ¿Por qué los clientes no podían conectarse? Porque el certificado servido por nginx estaba vencido y el handshake fallaba.
2. ¿Por qué estaba vencido? Porque nadie lo renovó antes del 14 de marzo.
3. ¿Por qué nadie lo renovó? Porque la renovación era manual y el recordatorio dependía del calendario de una persona que ya no estaba.
4. ¿Por qué no lo detectamos antes de que venciera? Porque no había alerta sobre la fecha de vencimiento y el probe externo no validaba la cadena.
5. ¿Por qué el probe no validaba? Porque se configuró con `insecure_skip_verify` para evitar falsos positivos con la CA interna y nunca se revisó.

## Qué salió bien

- Una vez identificada la causa, la mitigación tomó 13 minutos.
- El `reload` de nginx no cortó conexiones ni requirió reinicio.
- Soporte escaló con ejemplos concretos de error.

## Qué salió mal

- La detección dependió de los usuarios: 41 minutos hasta el primer escalamiento.
- Las métricas del servidor mostraban todo verde y eso desvió la investigación hacia la red.
- La prueba con `curl -k` dio un falso negativo y costó 17 minutos.
- No había inventario de certificados ni dueño claro.

## Dónde tuvimos suerte

- La CA interna seguía vigente, así que bastó con emitir un certificado nuevo.
- El incidente ocurrió en horario laboral con el equipo completo disponible.

## Action items

| # | Acción | Tipo | Dueño | Prioridad | Estado |
|---|---|---|---|---|---|
| 1 | Automatizar renovación con ACME y reload post-renovación | Prevención | Equipo de plataforma | P0 | En curso |
| 2 | Alertas de vencimiento a 30, 14 y 7 días (`TlsCertificateExpiringSoon`) | Detección | On-call de SRE | P0 | Hecho |
| 3 | Probe sintético externo con validación de cadena (`https_verified`) | Detección | On-call de SRE | P1 | Hecho |
| 4 | Inventario de certificados con dueño y procedimiento de renovación | Proceso | Líder técnico de plataforma | P1 | Pendiente |
| 5 | Prohibir `-k` / `insecure_skip_verify` en health checks y runbooks | Proceso | Equipo de SRE | P2 | Pendiente |
| 6 | Alerta de caída de tráfico sin errores (RPS bajo vs semana anterior) | Detección | On-call de SRE | P2 | Pendiente |

## Lecciones aprendidas

- Medir desde el cliente es tan importante como medir desde el servidor: una caída de tráfico sin errores es una señal en sí misma.
- Las tareas manuales con fecha de vencimiento fallan tarde o temprano. Hay que automatizarlas o, como mínimo, alertarlas.
- Las herramientas de diagnóstico que desactivan validaciones pueden ocultar exactamente el problema que se busca.
