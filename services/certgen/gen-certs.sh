#!/bin/sh
# Genera la CA local y el certificado de nginx.
#
# Uso: gen-certs.sh [init|valid|expired|show]
#   init     crea CA y certificado válido solo si no existen (idempotente)
#   valid    reemite un certificado válido firmado por la CA
#   expired  emite un certificado YA VENCIDO (fechas en el pasado) con openssl ca
#   show     muestra sujeto y fechas del certificado actual
#
# Los archivos quedan en $CERT_DIR (por defecto /certs):
#   ca.crt / ca.key        CA del laboratorio (los clientes confían en ca.crt)
#   server.crt / server.key certificado que sirve nginx
set -eu

MODE="${1:-init}"
CERT_DIR="${CERT_DIR:-/certs}"
SAN="${CERT_SAN:-DNS:localhost,DNS:nginx,IP:127.0.0.1}"
CN="${CERT_CN:-localhost}"

mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

ensure_ca() {
  if [ -s ca.key ] && [ -s ca.crt ]; then
    return
  fi
  echo "certgen: creando CA local"
  openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 3650 \
    -keyout ca.key -out ca.crt -subj "/CN=RCA Lab Local CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
  chmod 600 ca.key
  chmod 644 ca.crt
}

write_ext() {
  cat > server.ext <<EOF
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$SAN
EOF
}

new_csr() {
  openssl req -new -newkey rsa:2048 -nodes \
    -keyout server.key.new -out server.csr -subj "/CN=$CN" 2>/dev/null
}

install_new() {
  mv server.key.new server.key
  mv server.crt.new server.crt
  chmod 644 server.crt
  chmod 600 server.key
  rm -f server.csr
}

issue_valid() {
  ensure_ca
  write_ext
  new_csr
  openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -sha256 -days 825 -extfile server.ext -out server.crt.new 2>/dev/null
  install_new
  echo "certgen: certificado válido emitido"
}

issue_expired() {
  ensure_ca
  workdir="$(mktemp -d)"
  mkdir -p "$workdir/newcerts"
  : > "$workdir/index.txt"
  echo 1000 > "$workdir/serial"
  # openssl x509 -days no acepta fechas pasadas, openssl ca sí (-startdate/-enddate)
  cat > "$workdir/ca.cnf" <<EOF
[ ca ]
default_ca = lab_ca

[ lab_ca ]
dir = $workdir
database = \$dir/index.txt
new_certs_dir = \$dir/newcerts
serial = \$dir/serial
certificate = $CERT_DIR/ca.crt
private_key = $CERT_DIR/ca.key
default_md = sha256
policy = policy_any
unique_subject = no
copy_extensions = none

[ policy_any ]
commonName = supplied

[ server_ext ]
basicConstraints = CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = $SAN
EOF
  new_csr
  openssl ca -batch -notext -config "$workdir/ca.cnf" -extensions server_ext \
    -startdate 240101000000Z -enddate 240201000000Z \
    -in server.csr -out server.crt.new 2>/dev/null
  rm -rf "$workdir"
  install_new
  echo "certgen: certificado VENCIDO emitido"
}

show() {
  openssl x509 -in server.crt -noout -subject -issuer -dates
}

case "$MODE" in
  init)
    ensure_ca
    if [ -s server.crt ] && [ -s server.key ]; then
      echo "certgen: certificados existentes, no se regeneran"
    else
      issue_valid
    fi
    ;;
  valid) issue_valid ;;
  expired) issue_expired ;;
  show) ;;
  *)
    echo "modo desconocido: $MODE" >&2
    exit 2
    ;;
esac
show
