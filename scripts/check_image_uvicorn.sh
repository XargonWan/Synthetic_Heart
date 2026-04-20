#!/usr/bin/env bash
set -euo pipefail

IMAGE="$1"
VENV_DIR="${VENV_DIR:-/opt/venv}"

echo "Running uvicorn import check in image: $IMAGE"
# Run a temporary container and execute the import check
docker run --rm "$IMAGE" sh -c "${VENV_DIR}/bin/python3 -c \"import importlib; importlib.import_module('uvicorn'); print('uvicorn ok', importlib.import_module('uvicorn').__version__)\"" 
