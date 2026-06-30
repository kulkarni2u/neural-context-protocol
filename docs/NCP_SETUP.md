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

Use the installed compose-backed helper:

```bash
podman machine start podman-machine-default || true
NCP_CONTAINER_ENGINE=podman ncp infra up
```

This starts:

- Postgres/pgvector
- Redis

Equivalent local compose stack:

- [compose.yaml](../compose.yaml)
- [scripts/infra_up.sh](../scripts/infra_up.sh) and [scripts/infra_down.sh](../scripts/infra_down.sh), the lower-level repo scripts used by `ncp infra up/down`

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

By default the server requires no token on loopback (`127.0.0.1`/`localhost`/`::1`). Set `[server].auth_token` in `.ncp/config.toml` (generated automatically by `ncp init`), the `NCP_AUTH_TOKEN` env var, or `--auth-token` on `ncp serve` to require an `Authorization: Bearer <token>` header on `/mcp` and `/sse`. Never bind `ncp serve` to a non-loopback host without one of these set.

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

### Example: Java Repo Triage Loop

One practical pattern:

1. `analyzer` inspects `PaymentProcessor.java`, runs the failing test, and writes a compact root-cause chunk.
2. `fixer` retrieves that chunk from NCP, opens the file fresh, applies the fix, and writes the result.
3. `reviewer` receives a bounded whisper with the changed file list and can send back `dissent` or `share` without forcing transcript replay.

That is the point of the runtime. Shared working memory stays bounded even when the codebase is large and the pipeline runs for many turns.

## Tool-Specific Setup Examples

### Claude Code

1. Initialize with `ncp init`. If `claude` is installed and the command is
   running interactively, setup asks whether to add the Claude NCP hook files.
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
- `ncp_post_turn`
- `ncp_fetch`

### Codex CLI

1. Initialize with `ncp init`. If `codex` is installed and the command is
   running interactively, setup asks whether to add `.codex/hooks.json`,
   `.codex/hooks/ncp-session-start.sh`, and the `AGENTS.md` turn contract.
2. Copy the MCP example from `examples/07_codex_cli/`
3. Start the server:

```bash
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/project
```

Recommended loop:

1. call `ncp_get_context`
2. run the provider turn
3. persist durable memory with `ncp_write_memory`
4. call `ncp_post_turn` with consumed `pending_whisper_ids`
5. use `ncp_fetch` only for bounded retrieval

### OpenCode

1. Initialize with `ncp init`. If `opencode` is installed and the command is
   running interactively, setup asks whether to add `opencode.json`,
   `.opencode/plugins/ncp.js`, and the `AGENTS.md` turn contract.
2. Start the server:

```bash
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/project
```

OpenCode loads the project plugin through `opencode.json`; the plugin injects
the NCP turn/subagent contract via `experimental.chat.system.transform`.

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
