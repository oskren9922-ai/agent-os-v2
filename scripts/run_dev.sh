#!/usr/bin/env bash
# Agent OS x PayPal: one-step local start for macOS, Linux and Git Bash.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
if [ ! -d .venv ]; then
    echo "[1/4] Creating the virtual environment in .venv ..."
    "$PY" -m venv .venv
fi
if [ -x .venv/bin/python ]; then VENV_PY=.venv/bin/python; else VENV_PY=.venv/Scripts/python.exe; fi

echo "[2/4] Installing dependencies (first run takes a minute) ..."
"$VENV_PY" -m pip install --quiet --disable-pip-version-check -e ".[dev]"

if [ ! -f .env ]; then
    cp .env.example .env
    echo "      Created .env from .env.example (PAYPAL_ENV=mock, no keys needed)."
fi

echo "[3/4] Registering PayPal functions (connect time) ..."
"$VENV_PY" -m cli.main paypal register

echo "[4/4] Starting the daemon ..."
exec "$VENV_PY" -m cli.main serve "$@"
