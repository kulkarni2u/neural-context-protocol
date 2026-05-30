# Neural Context Protocol

[![CI](https://github.com/kulkarni2u/neural-context-protocol/actions/workflows/ci.yml/badge.svg)](https://github.com/kulkarni2u/neural-context-protocol/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Neural Context Protocol (NCP) is a local-first context runtime for multi-agent
systems. It keeps context bounded, persists useful memory across turns and
restarts, and exposes that shared context over MCP so multiple tools can work
from the same state instead of replaying full history.

In the included benchmarks, NCP reduced peak prompt size by `17.52x` on a
coding pipeline and `16.35x` on a research pipeline versus naive history replay.

## Why NCP

Multi-agent workflows usually break down in three predictable ways:

- prompt history keeps growing until token cost and latency get ugly
- agents lose useful state between turns or after a restart
- each tool has its own silo, so context does not move cleanly across workers

NCP addresses that with:

- bounded context assembly for the current turn
- durable shared memory in a project-local SQLite store
- targeted mid-turn retrieval with `ncp_fetch`
- cross-agent signaling with whispers
- one MCP surface that multiple coding tools can share

## What Is Proven Today

This repo is in an early alpha V1 state with a SQLite-first runtime and
HTTP/SSE MCP as the public transport.

What is already proven in this repository:

- Claude and OpenCode both connect to the same NCP MCP server over HTTP
- both hosts can write shared memory through MCP
- both hosts can retrieve memory written by the other host
- both hosts can deliver and receive whispers through the shared MCP runtime
- Sarathi can route Claude and OpenCode child-task dispatches through NCP handoffs
- the pgvector durable path now supports Redis-backed whisper delivery and Redis-backed fetch-session limits
- retrieval now filters lexical zero-overlap noise and reranks surviving matches with NCP's trust/age/generation weighting
- `retrieval_mode="trust_recency"` enables pure trust+recency ranking for non-BM25 backends
- `retrieval_mode="vector"` uses pgvector `<=>` cosine ANN search on stored embeddings (pgvector only)
- optional embedding storage: `SubconsciousChunk.embedding` (1536 dims) persisted via migration 003
- retrieval feedback calibration: `query()` tracks `retrieval_count`; `calibrate(feedback_mode=True)` auto-boosts frequently-retrieved chunks
- pgvector connection pooling: `psycopg_pool.ConnectionPool` (psycopg3) used by default; `close()` drains the pool
- pgvector schema migrations with advisory lock, checksums, and UP/DOWN rollback via `ncp migrate`
- opt-in streaming: `ncp_get_context` with `"stream": true` delivers context sections progressively as NDJSON (HTTP) or JSON-RPC notifications (stdio), eliminating timeout risk on large assemblies
- incremental assembly (`assemble_incremental()`) enforces the declared `max_tokens_per_call` budget and yields sections in priority order
- IVF-FLAT ANN index (migration 004): `embedding vector_cosine_ops` indexed with `lists=100`; configurable `ivfflat_probes` (default 10) scoped per transaction for recall tuning without pool leakage
- embedding provider integration: `PgvectorStore` auto-embeds chunks on write and query text at retrieval time via a configured adapter (`openai` → `text-embedding-3-small`; `local` → `sentence-transformers`); enabled via `[embedding]` config section or `NCP_EMBEDDING_ENABLED=true`
- caller-controlled `k`: `store.query(k=N)` now returns up to N results for any N ≥ 1; the previous hardcoded max-4 cap is removed from all retrieval paths (hybrid, trust_recency, vector)
- assembler k-forwarding: `assemble(k=N)` / `api.get_context(k=N)` thread k through to `store.query`; default preserves pressure-based logic (2/4)
- `AsyncPgvectorStore` (`ncp/stores/pgvector_async.py`): async-native pgvector store using `psycopg_pool.AsyncConnectionPool`; eliminates thread-pool shim on write/log/query async paths; full dedup/GC parity with sync store (`_async_soft_gc`, `_async_is_duplicate`, `_async_hard_gc`)
- `AsyncRedisCoordination` (`ncp/stores/redis_coordination.py`): native async whisper coordination using `redis.asyncio`; eliminates all `anyio.to_thread.run_sync` from `AsyncPgvectorStore` whisper path
- `log_cost` CLI command in `.ncp/run.py`: external callers (Sarathi, scripts) can post token usage directly into `ncp cost` without going through the full MCP surface
- restart persistence is validated by the dogfood harness
- bounded-context benchmarks are reproducible and show large prompt reduction

Current benchmark snapshot:

- coding pipeline: peak `174` NCP tokens vs `1927` naive replay, `17.52x` reduction
- research pipeline: peak `156` NCP tokens vs `1700` naive replay, `16.35x` reduction
- live Sarathi handoff route: one real Claude planning subtask dropped from `677` estimated bridge-prompt tokens to `265` estimated handoff tokens, a `60.9%` reduction
- live pgvector + Redis coordination path: `6/6` integration tests green on the local compose stack

## Quick Start

```bash
pip install -e .
ncp init
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/project
ncp status --cwd /path/to/project
ncp cost --cwd /path/to/project
ncp explain --cwd /path/to/project
```

Expected success signals:

- `ncp init` creates `.ncp/config.toml` and `CLAUDE.md`
- `ncp serve` starts the local HTTP MCP server on `127.0.0.1:4242`
- `ncp status` prints store, chunk, layer, pipeline, and activity metrics
- `ncp cost` prints cost totals plus per-agent/per-model rollups
- `ncp explain` turns the current store state into a short human-readable operator summary

Published alpha install path:

```bash
pip install neural-context-protocol
```

For a deeper setup path, see [docs/NCP_SETUP.md](./docs/NCP_SETUP.md).

## How It Works

NCP serves one shared project runtime over MCP. By default that runtime is the
project-local SQLite store; in the `0.2.0` storage line it can also be backed
by pgvector for durable memory plus Redis for ephemeral coordination:

```text
Claude Code  ─┐
Codex        ─┼→  ncp serve (HTTP/SSE MCP)  →  shared NCP store/runtime
OpenCode     ─┘
```

Each agent turn works roughly like this:

1. call `ncp_get_context`
2. get a bounded, assembled context block for the current role and task
3. optionally call `ncp_fetch` for targeted retrieval mid-turn
4. persist useful results with `ncp_write_memory`
5. send light-weight cross-agent signals with `ncp_emit_whisper`

Example assembled context:

```text
[NCP:CONSCIOUS]
agent:planner role:plan task:verify_shared_memory slot:bounded_context

[NCP:SUBCON]
chunk:sub_2267717ed22a layer:semantic
  opencode_http_probe_20260524T230734Z

[NCP:WHISPERS]
wsp from:opencode to:claude t:nudge c:0.96 age:1s
  whisper_probe_opencode_to_claude_20260524T232132Z
```

## MCP Transport

NCP’s public transport is HTTP/SSE MCP:

```bash
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/project
```

Endpoints:

- `GET /healthz`
- `GET /sse`
- `POST /mcp`

Use this endpoint in MCP host configs:

- `http://127.0.0.1:4242/mcp`

The public HTTP path is validated end to end by the dogfood harness, not just
by unit tests.

## Benchmarks

Runnable benchmark commands:

```python
from ncp.benchmarks import run_coding_pipeline_benchmark, run_research_pipeline_benchmark

run_coding_pipeline_benchmark(turns=40)
run_research_pipeline_benchmark(turns=36)
```

Benchmark write-ups:

- [docs/NCP_BENCHMARK_CODING_PIPELINE.md](./docs/NCP_BENCHMARK_CODING_PIPELINE.md)
- [docs/NCP_BENCHMARK_RESEARCH_PIPELINE.md](./docs/NCP_BENCHMARK_RESEARCH_PIPELINE.md)

## How NCP Reduces Token Cost

Multi-agent workflows that replay full history accumulate tokens quadratically.
NCP replaces "replay everything" with "retrieve what is relevant," keeping each
agent turn's context window small and signal-dense regardless of how long the
overall session runs.

### Mechanisms

**Bounded context assembly.** `ncp_get_context` returns a scored, deduplicated
window of the top-k most relevant chunks for the current role and task — not a
raw append of every prior turn. Context size stays roughly constant as the
session grows.

**Hybrid retrieval scoring.** Every candidate chunk is ranked by a weighted
fusion of three signals:

| Signal | Default weight | Effect |
|---|---|---|
| BM25 lexical overlap | 0.5 | surfaces on-topic chunks |
| Recency decay (4h half-life) | 0.3 | older facts rank lower automatically |
| `base_trust` (writer attestation) | 0.2 | user-verified facts outrank inferred ones |

Chunks that share zero lexical overlap with the query are filtered before
scoring, so noise never reaches the assembled context. Pass
`retrieval_mode="trust_recency"` to skip BM25 entirely — useful for backends
that perform their own vector similarity search before handing results to NCP
for trust/recency reranking.

**Retrieval feedback calibration.** Each `query()` call increments a
`retrieval_count` on returned chunks. `calibrate(feedback_mode=True)` then
boosts `base_trust` for frequently-retrieved chunks (proportional to retrieval
count, capped at 10 retrievals, +15% max), so facts that agents actually use
accumulate higher trust over time without manual intervention.

**Deduplication.** The store rejects writes with >92% content similarity in the
same zone/layer/pipeline. The same fact is stored once and updated, not
accumulated.

**Bounded handoff payloads.** When agents hand off work via whispers, the
payload is a compact instruction pointing at relevant chunk IDs. The receiving
agent fetches only the chunks it needs (`ncp_fetch k=2`) rather than receiving
a full transcript of the sender's session.

**Durable cross-turn memory.** Useful decisions are persisted as typed memory
chunks (episodic, procedural, semantic, social, reasoning_trace) at the end of
each turn. The next turn fetches them by relevance query instead of replaying
the full conversation.

### Observed reductions

| Scenario | Naive replay tokens | NCP tokens | Reduction |
|---|---|---|---|
| Coding pipeline (40 turns) | 1 927 peak | 174 peak | 17.5× |
| Research pipeline (36 turns) | 1 700 peak | 156 peak | 16.4× |
| Sarathi planning handoff (live) | ~677 estimated | ~265 estimated | 60.9% |

See [docs/NCP_BENCHMARK_CODING_PIPELINE.md](./docs/NCP_BENCHMARK_CODING_PIPELINE.md)
and [docs/NCP_BENCHMARK_RESEARCH_PIPELINE.md](./docs/NCP_BENCHMARK_RESEARCH_PIPELINE.md)
for methodology and raw numbers.

## Examples

Runnable examples:

```bash
python3 examples/01_quickstart.py
python3 examples/02_multi_agent.py
```

Integration examples:

- `examples/06_claude_code/` - Claude Code setup and MCP config
- `examples/07_codex_cli/` - Codex CLI MCP config and session loop

## Agent Handoffs

NCP can also drive a small partner-review loop over its own whisper queue:

```bash
ncp emit --from-agent codex --to claude --type share --pipeline-id pipe_demo --payload "slice=pgvector files=ncp/stores/pgvector.py ask=implement_and_handoff"
ncp handoff claude --cwd /path/to/project --pipeline-id pipe_demo --emit-to opencode
ncp handoff opencode --cwd /path/to/project --pipeline-id pipe_demo --emit-to claude
```

This keeps the handoff bounded:

- Claude consumes pending whispers for `claude`, works in the bound repo root, and can emit one bounded follow-up whisper.
- OpenCode consumes pending whispers for `opencode`, returns a JSON review payload, and can emit one bounded follow-up whisper.
- Whisper queue reads are non-destructive until the consumer run succeeds, so a failed provider call does not lose the handoff.

When Sarathi routes a child task through this handoff path, it no longer needs
to send the full provider bridge prompt as the primary instruction. The current
live proof on the `pgvector` storage slice reduced one Claude planning handoff
from `677` estimated prompt tokens to `265` estimated handoff tokens by using a
compact instruction plus bounded whisper payload.

When `store.type = "pgvector"` is active, the handoff and whisper path no longer
has to fall back to SQLite. Pgvector now delegates transient coordination to
Redis so `ncp emit`, `ncp handoff`, MCP whisper delivery, and MCP fetch-session
limits can all operate on the same runtime split:

- pgvector for durable memory and retrieval
- Redis for whispers and fetch-session state

## Current Scope

This repository currently ships:

- core NCP types and encoder
- chunking and bounded assembly with incremental `assemble_incremental()` generator
- SQLite-backed persistence
- pgvector durable-store with schema migrations, advisory-lock upgrade tooling, `ncp migrate` CLI, and `ThreadedConnectionPool` by default
- optional embedding storage on pgvector chunks with `retrieval_mode="vector"` ANN query via `<=>` operator
- Redis-backed coordination for pgvector whisper delivery and fetch-session limits
- opt-in live pgvector integration suite for the local Postgres/pgvector path
- HTTP/SSE MCP server
- dogfood validation harness
- local adapter plus provider adapter surface
- release preflight script
- minimal CI for `ruff`, `pytest`, and `build`

Current release:

- `neural-context-protocol==0.9.0`

## Documentation

- [docs/NCP_SETUP.md](./docs/NCP_SETUP.md) - install and first-run setup
- [docs/NCP_0_2_0_HANDOFF_PACKET.md](./docs/NCP_0_2_0_HANDOFF_PACKET.md) - current orchestrator packet for finishing the active post-alpha roadmap
- [docs/NCP_PROTOCOL_SPEC.md](./docs/NCP_PROTOCOL_SPEC.md) - normative protocol reference
- [docs/NCP_MCP_DOGFOOD_LOOP.md](./docs/NCP_MCP_DOGFOOD_LOOP.md) - deterministic MCP proof path
- [docs/NCP_PROVIDER_PARITY_BASELINE.md](./docs/NCP_PROVIDER_PARITY_BASELINE.md) - current live host parity snapshot
- [docs/NCP_POST_V1_ROADMAP.md](./docs/NCP_POST_V1_ROADMAP.md) - recommended path after the first alpha
- [docs/NCP_R2_STORAGE.md](./docs/NCP_R2_STORAGE.md) - pgvector/Redis storage kickoff and local infra direction
- [CHANGELOG.md](./CHANGELOG.md) - release-facing change summary

## Release Preflight

```bash
bash scripts/release_preflight.sh
```

<details>
<summary>Provider notes</summary>

- `GeminiAdapter` uses `google-genai` (`google.genai`) — the current Google SDK.
- `CohereAdapter` is functionally green. Known upstream warning noise is suppressed at the adapter boundary for the current alpha line.

</details>
