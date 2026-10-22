#!/bin/zsh
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/python3 "$ROOT/service_supervisor.py" --root "$ROOT"
