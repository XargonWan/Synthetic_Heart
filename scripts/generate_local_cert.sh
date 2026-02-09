#!/usr/bin/env bash
set -euo pipefail

# generate_local_cert.sh - helper to generate locally-trusted TLS certs using mkcert
# Usage: ./scripts/generate_local_cert.sh [host] [cert_dir]
# Example: ./scripts/generate_local_cert.sh localhost /config/ssl

HOST=${1:-localhost}
CERT_DIR=${2:-/config/ssl}

if ! command -v mkcert >/dev/null 2>&1; then
  echo "mkcert is required but not found in PATH. Install mkcert: https://github.com/FiloSottile/mkcert" >&2
  exit 2
fi

mkdir -p "$CERT_DIR"

echo "Installing local CA (may ask for permission)..."
mkcert -install

CERT_FILE="$CERT_DIR/${HOST}.pem"
KEY_FILE="$CERT_DIR/${HOST}-key.pem"

echo "Generating certificate for host: $HOST"
mkcert -cert-file "$CERT_FILE" -key-file "$KEY_FILE" "$HOST"

echo "Certificate generated: $CERT_FILE"
echo "Key generated: $KEY_FILE"

echo "To use these certificates with SyntH Web UI, set environment variables before starting the server:"
echo "  SYNTH_WEBUI_TLS=1"
echo "  SYNTH_WEBUI_CERTFILE=$CERT_FILE"
echo "  SYNTH_WEBUI_KEYFILE=$KEY_FILE"

exit 0
