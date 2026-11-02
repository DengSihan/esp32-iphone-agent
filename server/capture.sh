#!/usr/bin/env bash
# Independent local AirPlay capture lab; no BLE, models, or .env required.
set -euo pipefail
cd "$(dirname "$0")"
UV=$(command -v uv || true)
[[ -n "$UV" ]] || UV="$HOME/.local/bin/uv"
[[ -x "$UV" ]] || { echo "uv not found; install uv first"; exit 1; }
exec "$UV" run --frozen python -m hidagent.capture
