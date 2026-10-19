#!/usr/bin/env bash
# Run cu-perceive on Windows Python from WSL (or git-bash / native Windows).
#   CU_PERCEIVE_PYTHON  Windows python.exe (WSL path, e.g. /mnt/c/Python312/python.exe).
#                       Default: python.exe on PATH.
set -euo pipefail
export PYTHONIOENCODING=utf-8
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${CU_PERCEIVE_PYTHON:-python.exe}"
if command -v wslpath >/dev/null 2>&1 && [[ "$(uname -s)" == Linux ]]; then
  # WSL interop does not forward Linux env vars unless listed in WSLENV;
  # keep PYTHONPATH in Linux form, "/p" converts to Windows form at spawn.
  export PYTHONPATH="$ROOT"
  export WSLENV="PYTHONPATH/p:PYTHONIOENCODING${WSLENV:+:$WSLENV}"
else
  export PYTHONPATH="$ROOT"
fi
exec "$PY" -m cu_perceive "$@"
