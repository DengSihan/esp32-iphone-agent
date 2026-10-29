#!/usr/bin/env bash
# Start the Mac relay: Bluetooth link to the board + agent + web UI on http://127.0.0.1:8890 (PORT in .env).
# Usage: server/relay.sh [--sim]
#   --sim   a simulated board and phone: try the page, calibration and the agent without hardware
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "created server/.env: set LLM_BASE_URL, LLM_MODEL and LLM_API_KEY there"
fi
[[ ${1:-} == --sim ]] && export LINK=sim
UV=$(command -v uv || echo "$HOME/Library/Python/3.9/bin/uv")
[[ -x $UV ]] || { echo "uv not found: python3 -m pip install --user uv"; exit 1; }
exec "$UV" run --extra dev --env-file .env python -m hidagent
