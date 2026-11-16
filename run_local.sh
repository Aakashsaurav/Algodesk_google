#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating isolated Python virtual environment (.venv)..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing lightweight Broker Hub dependencies..."
python3 -m pip install --upgrade pip >/dev/null
python3 -m pip install -r requirements-web.txt

echo "Starting AlgoDesk Broker & Data Hub on http://127.0.0.1:8000 ..."
exec python3 -m web.local_server --host 127.0.0.1 --port 8000
