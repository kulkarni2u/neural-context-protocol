# NCP R2 Storage Kickoff

This document captures the first concrete storage implementation step after the
published V1 alpha line.

## Decision

Use containerized local infrastructure for development and testing:

- Postgres + pgvector for the durable production-oriented store
- Redis for ephemeral coordination and fast-path state

This is a developer/operator convenience layer, not the product runtime
contract. NCP itself should continue to speak in terms of DSNs and URLs, not
shell out to Docker or Podman.

## Current State

Today:

- SQLite remains the default and only fully implemented store
- `store.type = "pgvector"` now supports durable chunk writes, BM25-backed chunk query, working-zone reads, turn persistence, conscious snapshots, cost logging, and goal-version reads
- pgvector still does not provide whisper delivery; that remains intentionally deferred to Redis-backed coordination
- a live opt-in integration suite now exists at `tests/test_pgvector_integration.py`
- a local runner now exists at `scripts/test_pgvector_integration.sh`
- the current live runner brings up the Postgres/pgvector service only; Redis is not part of this validation slice yet
- `store.type = "redis"` remains explicitly deferred
- local infra is now scaffolded with `compose.yaml`
- helper scripts exist:
  - `scripts/infra_up.sh`
  - `scripts/infra_down.sh`

## Intended Role Split

### pgvector

Primary durable backend for:

- chunks
- tombstones
- turn records
- conscious log
- cost log
- hybrid retrieval metadata

### Redis

Ephemeral backend for:

- whisper delivery
- `ncp_fetch` session/rate-limit state
- hot caches
- short-lived coordination state

Redis should not become a second durable source of truth.

## Local Infra

Start:

```bash
./scripts/infra_up.sh
```

Stop:

```bash
./scripts/infra_down.sh
```

Defaults:

- Postgres/pgvector: `postgresql://postgres:postgres@127.0.0.1:5432/ncp`
- Redis: `redis://127.0.0.1:6379/0`

## Next Implementation Step

The next real `0.2.0` code slice should be:

1. run and harden the new live integration path against local Postgres/pgvector infra
2. Redis-backed ephemeral coordination helper for whispers and fetch sessions
3. reporting parity beyond the current SQLite-only `status`, `cost`, and `explain` commands
4. retrieval-hardening decisions after the durable path sees real infra usage

Do not start both deep backends at once before pgvector proves the durable path.
