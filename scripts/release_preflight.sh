#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"
WORK_DIR="$(mktemp -d)"
VENV_DIR="$WORK_DIR/venv"
SMOKE_DIR="$WORK_DIR/smoke-project"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

cd "$ROOT_DIR"

echo "==> Running full test suite"
python3 -m pytest -p no:cacheprovider tests

echo "==> Building wheel and sdist"
rm -rf "$DIST_DIR"
python3 -m build

echo "==> Creating clean virtual environment"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip >/dev/null

mkdir -p "$SMOKE_DIR"

echo "==> Smoke testing wheel install"
python -m pip install "$DIST_DIR"/ncp_sdk-*.whl >/dev/null
ncp init --cwd "$SMOKE_DIR" >/dev/null
ncp status --cwd "$SMOKE_DIR" >/dev/null
python -m pip uninstall -y ncp-sdk >/dev/null
rm -rf "$SMOKE_DIR"
mkdir -p "$SMOKE_DIR"

echo "==> Smoke testing sdist install"
python -m pip install "$DIST_DIR"/ncp_sdk-*.tar.gz >/dev/null
ncp init --cwd "$SMOKE_DIR" >/dev/null
ncp status --cwd "$SMOKE_DIR" >/dev/null

echo "Release preflight passed."
