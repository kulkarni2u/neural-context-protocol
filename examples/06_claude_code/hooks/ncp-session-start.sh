#!/usr/bin/env bash
set -euo pipefail

HOST="${NCP_HOST:-127.0.0.1}"
PORT="${NCP_PORT:-4242}"
AUTOSTART="${NCP_AUTOSTART:-1}"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
URL="http://${HOST}:${PORT}/healthz"

healthy() { curl -fsS -o /dev/null --max-time 2 "$URL" 2>/dev/null; }

STATUS="down"
if healthy; then
  STATUS="up"
elif [ "$AUTOSTART" != "0" ] && command -v ncp >/dev/null 2>&1; then
  mkdir -p "${PROJECT_DIR}/.ncp"
  nohup ncp serve --host "$HOST" --port "$PORT" --cwd "$PROJECT_DIR" \
    >>"${PROJECT_DIR}/.ncp/serve.log" 2>&1 &
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if healthy; then STATUS="up"; break; fi
    sleep 0.5
  done
fi

if [ "$STATUS" = "up" ]; then
  read -r -d '' MSG <<EOF || true
NCP memory bus is connected at http://${HOST}:${PORT}/mcp.
EOF
else
  read -r -d '' MSG <<EOF || true
NCP memory bus is not connected: http://${HOST}:${PORT}/mcp is NOT reachable.
Autostart was unavailable or did not connect. Start it with
\`ncp serve --host ${HOST} --port ${PORT} --cwd ${PROJECT_DIR}\`; until then,
continue without cross-agent memory.
EOF
fi

printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s}}\n' \
  "$(printf '%s' "$MSG" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
