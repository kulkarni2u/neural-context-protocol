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
- restart persistence is validated by the dogfood harness
- bounded-context benchmarks are reproducible and show large prompt reduction

Current benchmark snapshot:

- coding pipeline: peak `174` NCP tokens vs `1927` naive replay, `17.52x` reduction
- research pipeline: peak `156` NCP tokens vs `1700` naive replay, `16.35x` reduction
- live Sarathi handoff route: one real Claude planning subtask dropped from `677` estimated bridge-prompt tokens to `265` estimated handoff tokens, a `60.9%` reduction
- live pgvector + Redis coordination path: `4/4` integration tests green on the local compose stack

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

NCP keeps one shared SQLite store per project and serves it over MCP:

```text
Claude Code  ─┐
Codex        ─┼→  ncp serve (HTTP/SSE MCP)  →  .ncp/store.db
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

```bash
python3 benchmarks/coding_pipeline/run.py --turns 40
python3 benchmarks/research_pipeline/run.py --turns 36
```

Benchmark write-ups:

- [docs/NCP_BENCHMARK_CODING_PIPELINE.md](./docs/NCP_BENCHMARK_CODING_PIPELINE.md)
- [docs/NCP_BENCHMARK_RESEARCH_PIPELINE.md](./docs/NCP_BENCHMARK_RESEARCH_PIPELINE.md)

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
- chunking and bounded assembly
- SQLite-backed persistence
- pgvector durable-store preview for chunk/query and core runtime telemetry
- Redis-backed coordination for pgvector whisper delivery and fetch-session limits
- opt-in live pgvector integration suite for the local Postgres/pgvector path
- HTTP/SSE MCP server
- dogfood validation harness
- local adapter plus provider adapter surface
- release preflight script
- minimal CI for `ruff`, `pytest`, and `build`

Current published alpha:

- `neural-context-protocol==0.1.0a1`

Next focus:

- Next major focus: production-facing storage and retrieval
- Immediate next step: add reporting parity (`ncp status`, `ncp cost`, `ncp explain`) for the pgvector runtime, then carry the partner/reviewer loop into the next real `0.2.0` implementation task

## Documentation

- [docs/NCP_SETUP.md](./docs/NCP_SETUP.md) - install and first-run setup
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

- `GeminiAdapter` currently uses `google.generativeai`, which is deprecated upstream. The adapter is functionally green in tests, but should migrate to `google.genai` in a future pass.
- `CohereAdapter` is functionally green. Known upstream warning noise is suppressed at the adapter boundary for the current alpha line.

</details>
