#!/usr/bin/env bash
set -e

HOST=${1:-localhost}
PORT=${2:-8000}
URL="https://$HOST:$PORT/"

echo "Checking WebUI at $URL (ignoring TLS)..."
HTTP=$(curl -s -o /dev/null -w "%{http_code}" -k --max-time 5 "$URL" || true)
if [[ "$HTTP" == "200" ]]; then
  echo "OK: WebUI responding with 200"
  exit 0
else
  echo "FAIL: WebUI returned HTTP $HTTP"
  exit 2
fi
