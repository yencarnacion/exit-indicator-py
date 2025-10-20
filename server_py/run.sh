#!/usr/bin/env bash
set -euo pipefail

# POSIX-safe .env loader (optional)
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

export PYTHONUNBUFFERED=1

# Use config.tws.yaml by default; override with CONFIG_PATH env if you like.
CONFIG_PATH="${CONFIG_PATH:-./config.tws.yaml}"

# Default port; override in YAML.
UVICORN_PORT="$(python3 -c 'import yaml,sys;print(yaml.safe_load(open("'"$CONFIG_PATH"'"))["port"])' 2>/dev/null || echo 8086)"

# Activate venv if present
if [ -d .venv ]; then
  # shellcheck disable=SC1091
  . .venv/bin/activate
fi

exec uvicorn server_py.app:app \
  --host 0.0.0.0 --port "${UVICORN_PORT}" --log-level info \
  --reload
