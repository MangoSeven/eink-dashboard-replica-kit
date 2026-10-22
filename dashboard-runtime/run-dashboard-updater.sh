#!/bin/zsh
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT/.venv/bin/python" "$ROOT/dashboard_updater.py"
