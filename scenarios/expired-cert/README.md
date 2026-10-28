# Certificado TLS vencido en nginx

Los clientes empiezan a fallar con errores de TLS mientras del lado del servidor todo parece sano. Es el caso clásico de "los dashboards están en verde pero nadie puede comprar". El problema no está en la aplicación sino en el borde: nginx sirve un certificado con `notAfter` en el pasado.

**Categoría** network · **Dificultad** baja/media · **Herramientas** `curl -v`, `openssl s_client`, `openssl x509`, `nginx -T`, blackbox exporter

## Síntomas

- Los clientes que validan la cadena (navegadores, SDKs, el loadgen) fallan en el handshake:

  ```text
  $ curl --cacert certs/ca.crt https://localhost:8443/healthz
  curl: (60) SSL certificate problem: certificate has expired
  More details here: https://curl.se/docs/sslcerts.html
  ```

- Con `curl -k` "anda". Es la trampa: `-k` desactiva la validación y esconde el problema.
- El loadgen (perfil `load`) reporta errores del lado cliente:

  ```text
  loadgen_requests_total{endpoint="get_product",outcome="tls_error"}  1532
  loadgen_requests_total{endpoint="get_product",outcome="ok"}           0
  ```

- En la API **no hay errores**: el RPS de `orders-api` cae casi a cero, el error rate 5xx sigue en 0% y la latencia es normal. Los requests nunca llegan.
- En el error log de nginx pueden aparecer líneas del estilo:

  ```text
  [info] SSL_do_handshake() failed (SSL: error:0A000415:SSL routines::sslv3 alert certificate expired:SSL alert number 45) while SSL handshaking, client: 172.20.0.12, server: 0.0.0.0:443
  ```

- El panel "Días hasta vencimiento TLS" de Grafana muestra un número negativo y `probe_success{job="blackbox-https"}` cae a 0.

## Runbook de diagnóstico

1. **Observar desde el cliente, no desde el servidor.** Lo primero es reproducir lo que ve el usuario con validación activa.

   ```bash
   curl -v --cacert certs/ca.crt https://localhost:8443/healthz
   ```

   Buscá en la salida `SSL certificate problem` y el código de salida `60`. Si `curl -k` funciona y sin `-k` no, el problema es de TLS, no de la app.

2. **Mirar el lado servidor para descartar la app.** Si la API está sana y solo bajó el tráfico, la hipótesis "algo en el camino corta las conexiones" gana peso.

   ```bash
   docker compose ps
   docker compose logs --tail=50 api
   docker compose logs --tail=50 nginx
   ```

3. **Inspeccionar el certificado que se sirve realmente.** `s_client` muestra la cadena, el resultado de la verificación y las fechas.

   ```bash
   openssl s_client -connect localhost:8443 -servername localhost -CAfile certs/ca.crt </dev/null 2>/dev/null | grep -E "Verify return code|subject=|issuer="
   # Verify return code: 10 (certificate has expired)

   openssl s_client -connect localhost:8443 -servername localhost </dev/null 2>/dev/null \
     | openssl x509 -noout -subject -dates
   ```

4. **Comparar contra el archivo en disco.** Si el archivo es válido pero el servido no, faltó un `reload`. Si los dos están vencidos, el problema es el archivo.

   ```bash
   openssl x509 -in certs/server.crt -noout -subject -issuer -dates
   docker compose exec nginx openssl x509 -in /etc/nginx/certs/server.crt -noout -dates 2>/dev/null \
     || docker compose run --rm certgen show
   ```

5. **Confirmar qué archivo carga nginx.**

   ```bash
   docker compose exec nginx nginx -T 2>/dev/null | grep -E "ssl_certificate"
   ```

6. **Aislar.** Validar la cadena contra la CA para descartar un problema de confianza (CA equivocada) frente a uno de fechas.

   ```bash
   openssl verify -CAfile certs/ca.crt certs/server.crt
   # error 10 at 0 depth lookup: certificate has expired
   ```

   Error 10 es vencimiento. Error 20/21 sería CA desconocida, error 62 sería hostname/SAN.

## Consultas PromQL

```promql
# Días hasta el vencimiento (negativo = vencido)
(probe_ssl_earliest_cert_expiry{job="blackbox-tls"} - time()) / 86400

# Probe con validación de cadena (0 = los clientes reales fallan)
probe_success{job="blackbox-https"}

# Probe sin validación (1 = nginx responde, el problema es solo el certificado)
probe_success{job="blackbox-tls"}

# Caída de tráfico en la API sin errores
sum(rate(http_requests_total{job="orders-api"}[5m]))

# Vista del cliente (solo con el perfil load)
sum by (outcome) (rate(loadgen_requests_total[1m]))
```

La combinación `blackbox-https = 0` y `blackbox-tls = 1` es la firma de este escenario: el servidor responde, pero la cadena no valida.

## Confirmar la causa raíz

La causa queda confirmada cuando se cumplen las tres:

1. `openssl s_client ... -CAfile certs/ca.crt` devuelve `Verify return code: 10 (certificate has expired)`.
2. `notAfter` del certificado servido es anterior a `date -u`.
3. Al reemplazar el certificado por uno válido y recargar nginx, `curl --cacert certs/ca.crt` vuelve a responder 200 sin tocar la API.

## Mitigación inmediata

Emitir un certificado válido y recargar nginx (sin reiniciar, no corta conexiones en curso):

```bash
labctl fix expired-cert
# equivalente a:
docker compose run --rm certgen valid
docker compose exec -T nginx nginx -s reload
```

Verificar:

```bash
curl -fsS --cacert certs/ca.crt https://localhost:8443/healthz
openssl s_client -connect localhost:8443 -servername localhost </dev/null 2>/dev/null | openssl x509 -noout -dates
```

## Solución definitiva

- Automatizar la renovación (ACME con certbot/lego, cert-manager en Kubernetes) con renovación al llegar a 2/3 de la vida útil.
- Alertas de vencimiento escalonadas a 30, 14 y 7 días, no solo cuando ya venció.
- Inventario de certificados: dónde está cada uno, quién es el dueño y cómo se renueva.
- Monitoreo sintético **con validación de cadena** desde afuera (blackbox `https_verified`).
- Prohibir `-k` / `insecure_skip_verify` en health checks: oculta exactamente esta falla.
- El proceso de renovación tiene que incluir el `reload` y una verificación post-cambio del certificado servido.

## Alertas

Definidas en [`alerts.yml`](alerts.yml):

| Alerta | Detecta |
|---|---|
| `TlsCertificateExpiringSoon` | Faltan menos de 14 días para el vencimiento |
| `TlsCertificateExpired` | El certificado servido ya venció |
| `HttpsProbeFailingWithValidation` | El probe que valida la cadena falla mientras el que no valida anda |

## Cómo reproducir

```bash
make up-load                       # stack + generador de carga
labctl break expired-cert          # emite un certificado vencido y recarga nginx
# diagnosticar con el runbook
labctl fix expired-cert
```

En modo desafío: `labctl break random` y después `labctl status --reveal`.
