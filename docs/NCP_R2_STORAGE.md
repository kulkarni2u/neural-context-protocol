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
- `store.type = "pgvector"` now supports durable chunk writes, BM25-first chunk query, working-zone reads, turn persistence, conscious snapshots, cost logging, goal-version reads, and operator reporting
- the current retrieval path now rejects lexical zero-overlap chunks and reranks surviving matches with NCP's trust/age/generation weighting via `effective_score`
- pgvector now delegates whisper delivery and fetch-session limits to Redis-backed coordination
- a live opt-in integration suite now exists at `tests/test_pgvector_integration.py`
- a local runner now exists at `scripts/test_pgvector_integration.sh`
- the current live runner brings up both Postgres/pgvector and Redis for the coordination path
- `store.type = "redis"` remains explicitly deferred
- local infra is now scaffolded with `compose.yaml`
- helper scripts exist:
  - `scripts/infra_up.sh`
  - `scripts/infra_down.sh`
- Sarathi can now route Claude and OpenCode task lanes through NCP handoffs for NCP-enabled workspaces, and one live Claude planning subtask on this storage track recorded a `60.9%` estimated prompt reduction versus the older full bridge prompt
- the local integration runner now validates `6/6` pgvector+Redis integration checks on the compose stack

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

1. carry the same partner/reviewer handoff loop into the next retrieval slice
2. hybrid retrieval beyond the current BM25-first query path
3. only then widen the Redis fast-path beyond whispers and fetch sessions into caches or extra coordination surfaces

Do not start both deep backends at once before pgvector proves the durable path.
