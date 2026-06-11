#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DSN="${NCP_PGVECTOR_DSN:-postgresql://postgres:postgres@127.0.0.1:5432/ncp}"
REDIS_URL="${NCP_REDIS_URL:-redis://127.0.0.1:6379/0}"

cd "$ROOT_DIR"

"$ROOT_DIR/scripts/infra_up.sh"

python3 - <<'PY'
import os
import sys
import time

try:
    import psycopg
except ImportError:
    print("psycopg is required for dev_up. Install with: python3 -m pip install -e '.[dev,pgvector,redis]'", file=sys.stderr)
    raise SystemExit(1)

try:
    import redis
except ImportError:
    print("redis is required for dev_up. Install with: python3 -m pip install -e '.[dev,pgvector,redis]'", file=sys.stderr)
    raise SystemExit(1)

from ncp.stores.migrations import MigrationRunner

dsn = os.environ.get("NCP_PGVECTOR_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/ncp")
redis_url = os.environ.get("NCP_REDIS_URL", "redis://127.0.0.1:6379/0")
deadline = time.time() + 60
last_error = "unknown"

while time.time() < deadline:
    try:
        conn = psycopg.connect(dsn)
        redis.from_url(redis_url, decode_responses=True).ping()
        break
    except Exception as exc:
        last_error = str(exc)
        time.sleep(1.5)
else:
    print(f"Timed out waiting for dev infra: {last_error}", file=sys.stderr)
    raise SystemExit(1)

schema = os.environ.get("NCP_PGVECTOR_SCHEMA", "ncp")
prefix = os.environ.get("NCP_PGVECTOR_TABLE_PREFIX", "ncp_")
runner = MigrationRunner(conn, schema=schema, table_prefix=prefix)
applied = runner.apply_all()
conn.close()
print(f"Applied {len(applied)} pgvector migrations.")
PY

export NCP_PGVECTOR_DSN="$DSN"
export NCP_REDIS_URL="$REDIS_URL"
export NCP_RUN_PGVECTOR_INTEGRATION="${NCP_RUN_PGVECTOR_INTEGRATION:-1}"

python3 -m pytest "$@"
