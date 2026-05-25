#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/compose.yaml"
ENGINE="${NCP_CONTAINER_ENGINE:-auto}"
DSN="${NCP_PGVECTOR_DSN:-postgresql://postgres:postgres@127.0.0.1:5432/ncp}"

cd "$ROOT_DIR"

resolve_compose() {
  if [[ "$ENGINE" == "docker" ]]; then
    echo "docker compose"
    return
  fi
  if [[ "$ENGINE" == "podman" ]]; then
    echo "podman compose"
    return
  fi
  if command -v podman >/dev/null 2>&1; then
    echo "podman compose"
    return
  fi
  if command -v docker >/dev/null 2>&1; then
    echo "docker compose"
    return
  fi
  echo ""
}

COMPOSE_CMD="$(resolve_compose)"
if [[ -z "$COMPOSE_CMD" ]]; then
  echo "No supported container engine found. Install docker or podman, or set NCP_CONTAINER_ENGINE." >&2
  exit 1
fi

STARTED_POSTGRES=0

cleanup() {
  local status=$?
  if [[ "$STARTED_POSTGRES" -eq 1 && "${NCP_KEEP_INFRA:-0}" != "1" ]]; then
    $COMPOSE_CMD -f "$COMPOSE_FILE" down >/dev/null 2>&1 || true
  fi
  exit "$status"
}

trap cleanup EXIT

python3 - <<'PY'
import importlib.util
import sys

if importlib.util.find_spec("psycopg2") is None:
    print("psycopg2 is not installed. Install neural-context-protocol[pgvector] or psycopg2-binary first.", file=sys.stderr)
    raise SystemExit(1)
PY

echo "Using: $COMPOSE_CMD"
$COMPOSE_CMD -f "$COMPOSE_FILE" up -d postgres
STARTED_POSTGRES=1

python3 - <<'PY'
import os
import sys
import time

import psycopg2

dsn = os.environ.get("NCP_PGVECTOR_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/ncp")
deadline = time.time() + 45
last_error = None
while time.time() < deadline:
    try:
        conn = psycopg2.connect(dsn)
        conn.close()
        print("pgvector integration target is ready.")
        raise SystemExit(0)
    except Exception as exc:  # pragma: no cover - readiness loop only
        last_error = exc
        time.sleep(1.5)
print(f"Timed out waiting for Postgres/pgvector: {last_error}", file=sys.stderr)
raise SystemExit(1)
PY

export NCP_RUN_PGVECTOR_INTEGRATION=1
export NCP_PGVECTOR_DSN="$DSN"
python3 -m pytest tests/test_pgvector_integration.py "$@"
