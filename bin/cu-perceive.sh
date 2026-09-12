#!/usr/bin/env bash
# Run cu-perceive on Windows Python from WSL (or git-bash / native Windows).
set -euo pipefail
export PYTHONIOENCODING=utf-8
if command -v wslpath >/dev/null 2>&1 && [[ "$(uname -s)" == Linux ]]; then
  # WSL interop does not forward Linux env vars unless listed in WSLENV;
  # keep PYTHONPATH in Linux form, "/p" converts to Windows form at spawn.
  export PYTHONPATH='/mnt/c/Users/jawn/src/cu-perceive:/mnt/c/Users/jawn/src/enikk'
  export WSLENV="PYTHONPATH/p:PYTHONIOENCODING${WSLENV:+:$WSLENV}"
else
  export PYTHONPATH='C:\Users\jawn\src\cu-perceive;C:\Users\jawn\src\enikk'
fi
exec /mnt/c/Users/jawn/miniconda3/python.exe -m cu_perceive "$@"
