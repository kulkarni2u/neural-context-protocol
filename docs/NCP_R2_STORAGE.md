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

- SQLite remains the only implemented store
- `redis` and `pgvector` remain forward-compatible config values
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

1. pgvector backend skeleton with real schema creation
2. backend selection helper from config
3. Redis-backed ephemeral coordination helper for whispers and fetch sessions
4. integration tests against the local infra path

Do not start both deep backends at once before pgvector proves the durable path.
