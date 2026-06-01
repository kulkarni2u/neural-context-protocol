# NCP Setup

This guide covers first-run setup for Neural Context Protocol.

## Install

Base package:

```bash
pip install neural-context-protocol
```

If you want the scalable local mode too:

```bash
pip install 'neural-context-protocol[pgvector,redis]'
```

## Choose Your Runtime Mode

NCP supports two setup paths:

| Mode | Best for | Backing services |
|---|---|---|
| SQLite | default local-first setup | local `.ncp/store.db` |
| pgvector + Redis | scalable local lab setup | Postgres/pgvector + Redis |

## Initialize a Project

From your project root:

```bash
ncp init
```

Interactive terminals will prompt for the store mode.

You can also choose explicitly:

```bash
ncp init --store sqlite
ncp init --store pgvector
```

Initialization creates:

- `.ncp/config.toml`
- `CLAUDE.md`

## SQLite Setup

This is the default local-first path.

```bash
ncp init --store sqlite
ncp status
ncp cost
ncp explain
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/project
```

Expected behavior:

- store path resolves to `.ncp/store.db`
- no external services are required
- MCP is available at `http://127.0.0.1:4242/mcp`

## pgvector + Redis Setup

This is the scalable local path.

### 1. Initialize

```bash
ncp init --store pgvector
```

### 2. Bring up local infra

Use the repo’s compose-backed helpers:

```bash
podman machine start podman-machine-default || true
NCP_CONTAINER_ENGINE=podman ./scripts/infra_up.sh
```

This starts:

- Postgres/pgvector
- Redis

Equivalent local compose stack:

- [compose.yaml](../compose.yaml)
- [scripts/infra_up.sh](../scripts/infra_up.sh)
- [scripts/infra_down.sh](../scripts/infra_down.sh)

### 3. Apply pgvector schema migrations

```bash
ncp migrate apply --cwd /path/to/project
```

### 4. Verify the scalable path

```bash
ncp status --cwd /path/to/project
ncp cost --cwd /path/to/project
ncp explain --cwd /path/to/project
```

### 5. Start MCP

```bash
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/project
```

### 6. Optional live integration verification

```bash
NCP_CONTAINER_ENGINE=podman ./scripts/test_pgvector_integration.sh
```

This validates:

- durable pgvector behavior
- reporting parity
- Redis-backed coordination

### 7. Optional Sarathi-orchestrated validation

If you want the same local validation driven through Sarathi:

```bash
SARATHI_EXEC_COMMANDS=1 NCP_CONTAINER_ENGINE=podman sarathi run \
  "Validate the live NCP pgvector path end to end: ensure local pgvector+redis infra is running, apply migrations if needed, run scripts/test_pgvector_integration.sh, and report the exact pass/fail result with blockers." \
  --policy-pack /path/to/project/policy-pack \
  --ncp
```

This keeps the execution on the same Podman-backed compose stack while letting
Sarathi orchestrate the flow and capture the lifecycle.

## Start the MCP Server

NCP’s public transport is HTTP/SSE MCP:

```bash
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/project
```

Endpoints:

- `GET /healthz`
- `GET /sse`
- `POST /mcp`

Preferred host endpoint:

- `http://127.0.0.1:4242/mcp`

## Common Validation Commands

```bash
ncp status --cwd /path/to/project
ncp cost --cwd /path/to/project
ncp explain --cwd /path/to/project
ncp dogfood --cwd /path/to/project
```

What they tell you:

- `status` — store/activity rollups
- `cost` — token and USD rollups
- `explain` — human-readable operator summary
- `dogfood` — deterministic MCP proof

## Multi-Tool Sharing

Each coding tool connects to the same NCP server:

```text
Claude / Codex / OpenCode / other MCP host
  -> ncp serve (HTTP/SSE)
  -> shared NCP runtime
  -> SQLite or pgvector + Redis
```

The shared memory is in the runtime/store, not in the client process.

## Tool-Specific Setup Examples

### Claude Code

1. Initialize with `ncp init`
2. Copy the example MCP config:

```bash
cp examples/06_claude_code/mcp_servers.json .mcp.json
```

3. Start the server:

```bash
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/project
```

Expected tool surface:

- `ncp_get_context`
- `ncp_write_memory`
- `ncp_emit_whisper`
- `ncp_fetch`

### Codex CLI

1. Initialize with `ncp init`
2. Copy the MCP example from `examples/07_codex_cli/`
3. Start the server:

```bash
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/project
```

Recommended loop:

1. call `ncp_get_context`
2. run the provider turn
3. persist durable memory with `ncp_write_memory`
4. use `ncp_fetch` only for bounded retrieval

## Optional Whisper Handoff Loop

NCP can also drive a bounded partner/reviewer loop over its whisper queue:

```bash
ncp emit --cwd /path/to/project --from-agent codex --to claude --type share --pipeline-id pipe_demo --payload "slice=pgvector files=ncp/stores/pgvector.py ask=implement_and_handoff"
ncp handoff claude --cwd /path/to/project --pipeline-id pipe_demo --emit-to opencode
ncp handoff opencode --cwd /path/to/project --pipeline-id pipe_demo --emit-to claude
```

Notes:

- queue reads are non-destructive until the provider run succeeds
- the same loop works on SQLite and on pgvector + Redis
- the value is bounded task handoff, not orchestrator lock-in

## Release Preflight

```bash
bash scripts/release_preflight.sh
```
