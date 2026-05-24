# Neural Context Protocol

Neural Context Protocol (NCP): bounded, persistent context for multi-agent pipelines.

## Repo layout

- `docs/NCP_SETUP.md` - install and first-run setup
- `docs/NCP_PROTOCOL_SPEC.md` - normative protocol reference
- `docs/NCP_MCP_DOGFOOD_LOOP.md` - deterministic MCP dogfood proof
- `docs/NCP_PROVIDER_PARITY_BASELINE.md` - current live parity snapshot
- `docs/NCP_BENCHMARK_CODING_PIPELINE.md` - first bounded-context benchmark result
- `docs/NCP_BENCHMARK_RESEARCH_PIPELINE.md` - research-style benchmark result
- `CHANGELOG.md` - release-facing change summary
- `benchmarks/coding_pipeline/` - runnable benchmark artifact
- `benchmarks/research_pipeline/` - second runnable benchmark artifact
- `ncp/` - Python package
- `tests/` - test suite

## Status

This repository is now in a release-prepared alpha-candidate state for the
SQLite-first V1 spine, with HTTP/SSE MCP as the single public transport.

Current completed slice:

- launch-critical core types in `ncp/types.py`
- pidgin encoder in `ncp/encoder.py`
- type-aware chunker in `ncp/chunker.py`
- SQLite-first persistence slice in `ncp/stores/sqlite.py`
- first local assembler slice in `ncp/assembler.py`
- config loading in `ncp/config.py`
- pricing and cost calculation in `ncp/costs.py`
- first public API slice in `ncp/api.py`
- first local adapter and CLI slice in `ncp/adapters/local.py` and `ncp/cli.py`
- canonical MCP dogfood harness in `ncp/dogfood.py`
- HTTP/SSE MCP transport in `ncp/mcp/server.py`
- focused validation coverage in `tests/test_types.py`
- golden-format encoder coverage in `tests/test_encoder.py`
- chunking coverage in `tests/test_chunker.py`
- persistence coverage in `tests/test_sqlite_store.py`
- assembler/config/cost coverage in `tests/test_assembler.py`, `tests/test_config.py`, and `tests/test_costs.py`
- public API coverage in `tests/test_api.py`
- CLI coverage in `tests/test_cli.py`
- end-to-end MCP dogfood coverage in `tests/test_dogfood.py`
- trust-boundary hardening coverage in `tests/test_mcp_server.py`, `tests/test_sqlite_store.py`, `tests/test_assembler_phase3.py`, and `tests/test_types.py`
- pre-MCP dogfood and provider parity docs in `docs/`
- deterministic MCP dogfood runbook in `docs/NCP_MCP_DOGFOOD_LOOP.md`
- repeatability runner for CLI-backed provider stabilization in `ncp/dogfood.py`
- first coding-pipeline benchmark with real token numbers in `benchmarks/coding_pipeline/`
- second research-pipeline benchmark with real token numbers in `benchmarks/research_pipeline/`
- launch-critical examples in `examples/01_quickstart.py`, `examples/02_multi_agent.py`, `examples/06_claude_code/`, and `examples/07_codex_cli/`
- wheel and sdist install smoke verified through the installed `ncp` CLI
- adapter and store degradation paths now return explicit NCP-owned errors with focused CLI coverage
- minimal GitHub Actions CI runs `ruff`, `pytest`, and `build` on push and pull request

Next release step: publish the first alpha release.

That means:

- confirm the final version/tag
- upload the built artifacts
- cut the first GitHub release

## Quick start

```bash
pip install ncp-sdk
ncp init
ncp status
```

For a guided setup path, see [docs/NCP_SETUP.md](./docs/NCP_SETUP.md).

## HTTP/SSE MCP

NCP’s public transport is HTTP/SSE MCP:

```bash
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/project
```

Endpoints:

- `GET /healthz` - transport readiness
- `GET /sse` - SSE discovery stream
- `POST /mcp` - streamable HTTP / JSON-RPC request endpoint

Each coding tool connects to the same long-lived NCP server over HTTP/SSE while
sharing the same `.ncp/store.db` SQLite file.

```
Claude Code  ─┐
Codex        ─┼→  ncp serve (HTTP/SSE)  →  .ncp/store.db
OpenCode     ─┘
```

That keeps transport, process lifetime, and shared memory behavior simple and
observable.

The public HTTP/SSE path is validated end to end by the dogfood harness, not
just by unit tests.

For MCP host configuration, prefer the HTTP endpoint:

- `http://127.0.0.1:4242/mcp`

## Provider Notes

- `GeminiAdapter` is currently implemented against `google.generativeai`, which is deprecated upstream. The adapter is functionally green in tests, but a future follow-up should migrate it to `google.genai` once that dependency path is available in the supported environment.
- `CohereAdapter` is functionally green, but the current upstream SDK emits Python deprecation warnings during tests. Treat that as an upstream runtime note, not a protocol/runtime correctness failure.

## Benchmarks

Runnable benchmark commands:

```bash
python3 benchmarks/coding_pipeline/run.py --turns 40
python3 benchmarks/research_pipeline/run.py --turns 36
```

Current benchmark snapshot:

- coding pipeline: peak `174` NCP tokens vs `1927` naive replay, `17.52x` final-turn reduction
- research pipeline: peak `156` NCP tokens vs `1700` naive replay, `16.35x` final-turn reduction

Release preflight:

```bash
bash scripts/release_preflight.sh
```

## Examples

Runnable examples:

```bash
python3 examples/01_quickstart.py
python3 examples/02_multi_agent.py
```

Integration setup examples:

- `examples/06_claude_code/` - `CLAUDE.md`, MCP config, and a minimal setup README
- `examples/07_codex_cli/` - Codex CLI MCP config and session loop README
