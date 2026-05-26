#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/compose.yaml"
ENGINE="${NCP_CONTAINER_ENGINE:-auto}"

resolve_compose() {
  if [[ "$ENGINE" == "docker" ]]; then
    echo "docker compose"
    return
  fi
  if [[ "$ENGINE" == "podman" ]]; then
    echo "podman compose"
    return
  fi
  if command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
    echo "podman compose"
    return
  fi
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
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

cd "$ROOT_DIR"
echo "Using: $COMPOSE_CMD"
$COMPOSE_CMD -f "$COMPOSE_FILE" down
