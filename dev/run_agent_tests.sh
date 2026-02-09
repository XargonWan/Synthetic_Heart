#!/bin/bash
set -e

VENV_DIR=venv
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

pip install -r requirements.txt
pip install pytest pytest-asyncio -q

# Run just agent tests
pytest -q tests/test_agent_core.py -q
