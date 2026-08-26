#!/usr/bin/env bash
# Run cu-perceive on Windows Python from WSL.
set -euo pipefail
export PYTHONPATH='C:\Users\jawn\src\cu-perceive;C:\Users\jawn\src\enikk'
export PYTHONIOENCODING=utf-8
exec /mnt/c/Users/jawn/miniconda3/python.exe -m cu_perceive "$@"
