#!/usr/bin/env bash
set -euo pipefail

echo "[INFO] Starting claude-agent-http service on port ${PORT:-8765} ..."
cd /home/admin/claude-code-scripts
exec python3 run.py
