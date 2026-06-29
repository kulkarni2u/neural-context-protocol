#!/usr/bin/env bash
# Idempotently ensure an NCP memory bus is reachable.
#
# Health-checks http://HOST:PORT/healthz and, if the bus is down and autostart
# is enabled, launches `ncp serve` in the background. Safe to call repeatedly.
#
# Env:
#   NCP_HOST       default 127.0.0.1
#   NCP_PORT       default 4242
#   NCP_CWD        project root passed to `ncp serve` (default: current dir)
#   NCP_AUTOSTART  set to 0 to health-check only (never start a server)
#
# All human-readable output goes to stderr so callers can capture a clean
# status on stdout: "up" or "down".
set -euo pipefail

HOST="${NCP_HOST:-127.0.0.1}"
PORT="${NCP_PORT:-4242}"
CWD="${NCP_CWD:-$(pwd)}"
AUTOSTART="${NCP_AUTOSTART:-1}"
URL="http://${HOST}:${PORT}/healthz"

log() { printf '%s\n' "ncp-ensure-serve: $*" >&2; }

healthy() {
  curl -fsS -o /dev/null --max-time 2 "$URL" 2>/dev/null
}

if healthy; then
  log "bus already up at ${HOST}:${PORT}"
  echo "up"
  exit 0
fi

if [ "$AUTOSTART" = "0" ]; then
  log "bus down at ${HOST}:${PORT} (autostart disabled)"
  echo "down"
  exit 0
fi

if ! command -v ncp >/dev/null 2>&1; then
  log "bus down and 'ncp' not on PATH; run 'pip install neural-context-protocol'"
  echo "down"
  exit 0
fi

mkdir -p "${CWD}/.ncp"
log "starting 'ncp serve' on ${HOST}:${PORT} (logs: ${CWD}/.ncp/serve.log)"
nohup ncp serve --host "$HOST" --port "$PORT" --cwd "$CWD" \
  >>"${CWD}/.ncp/serve.log" 2>&1 &

# Wait briefly for the bus to come up.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if healthy; then
    log "bus is up"
    echo "up"
    exit 0
  fi
  sleep 0.5
done

log "bus did not become ready; check ${CWD}/.ncp/serve.log"
echo "down"
