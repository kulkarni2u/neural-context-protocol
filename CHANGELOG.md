# Changelog

All notable changes to Neural Context Protocol will be documented in this file.

## Unreleased

### Added

- richer `ncp status` output with chunk, tombstone, layer, pipeline, and last-activity visibility
- new `ncp cost` command with total, per-agent, per-model, and recent-entry rollups
- new `ncp explain` command for a short human-readable store summary
- Claude `stream-json` review helper script for bounded review/debug workflows
- public `ncp handoff claude` and `ncp handoff opencode` commands for whisper-driven partner/reviewer loops

### Changed

- provider install guidance now points at `neural-context-protocol[providers]`
- known upstream Cohere warning noise is suppressed at the adapter boundary for the current alpha line
- public docs now reflect the live Sarathi-managed handoff proof and its measured prompt reduction on the `pgvector` storage slice

### Planned next layer

- containerized local infra scaffolding for Postgres/pgvector and Redis is now in place for the `0.2.0` storage kickoff
- pgvector now supports durable chunk writes/query, working-zone reads, recent-ref turn logging, conscious snapshots, cost logging, and pipeline goal-version reads
- a live opt-in pgvector integration suite and runner script now exist for the local Postgres/pgvector path
- Redis remains a deferred ephemeral backend for whisper delivery and short-lived coordination
- the next live storage step is to complete the paired OpenCode review lane on the current `pgvector` task, then continue with Redis-backed ephemeral coordination

## 0.1.0a1 - 2026-05-24

Follow-up alpha release to publish under the PyPI-owned project name
`neural-context-protocol`.

### Changed

- PyPI package name changed from `ncp-sdk` to `neural-context-protocol`
- install documentation updated to reflect the published package name

## 0.1.0a0 - 2026-05-24

Initial alpha release candidate for the SQLite-first NCP V1 spine with HTTP/SSE
MCP as the public transport.

### Added

- launch-critical core models in `ncp/types.py`
- pidgin encoder, chunker, assembler, and SQLite store
- local runtime API in `ncp/api.py`
- provider adapters for Anthropic, OpenAI, Ollama, Gemini, Mistral, and Cohere
- HTTP/SSE MCP server and CLI commands: `ncp init`, `ncp serve`, `ncp status`, `ncp emit`, `ncp dogfood`
- deterministic MCP dogfood harness with Claude/OpenCode/Codex continuation support
- provider parity, benchmark, and dogfood documentation under `docs/`
- launch-critical examples for quickstart, multi-agent handoff, Claude Code, and Codex CLI
- wheel and sdist packaging path with installed CLI smoke proof
- minimal GitHub Actions CI for `ruff`, `pytest`, and `build`

### Changed

- adapter failures now surface as NCP-owned configuration, timeout, and response errors
- SQLite unavailability now surfaces as an explicit store error and clean CLI failure
- trust-boundary coverage now rejects structural-field whitespace injection, immutable `src` changes, invalid write bypasses, dissent broadcasts, fetch over-limit misuse, and dead-end ref ambiguity

### Verified

- full test suite: `176 passed`
- package build: wheel and sdist build successfully
- clean install smoke: installed `ncp init` and `ncp status` work from both wheel and sdist
- live host proof: Claude and OpenCode both connect to the same HTTP MCP endpoint, write shared memory, fetch each other's writes, and deliver whispers across hosts

### Known Notes

- `GeminiAdapter` still uses the deprecated `google.generativeai` SDK because `google.genai` is not yet available in the current supported environment
- the Cohere SDK emits upstream Python deprecation warnings during tests, but functional behavior is green
