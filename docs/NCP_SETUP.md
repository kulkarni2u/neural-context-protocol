# NCP Setup

This guide covers the public first-run setup for Neural Context Protocol.

## Install

Base package:

```bash
pip install neural-context-protocol
```

With a provider SDK:

```bash
pip install 'neural-context-protocol[providers]'
```

## Initialize a project

From your project root:

```bash
ncp init
```

This creates:

- `.ncp/config.toml`
- `CLAUDE.md`

## Check local status

```bash
ncp status
ncp cost
ncp explain
```

This verifies that the active NCP store can be opened and that the CLI is wired
correctly. `ncp status` surfaces store and activity rollups; `ncp cost`
surfaces token and USD rollups from `cost_log`; `ncp explain` summarizes the
same state in a short human-readable operator view.

## Start the MCP server

NCP’s public transport is HTTP/SSE MCP:

```bash
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/your/project
```

HTTP endpoints:

- `GET /healthz`
- `GET /sse`
- `POST /mcp`

For host configs, prefer `http://127.0.0.1:4242/mcp`.

## R2 local infra preview

The `0.2.0` storage path (current version `0.6.0`) uses local containerized
infrastructure for Postgres/pgvector and Redis:

```bash
./scripts/infra_up.sh
```

This does not change the current default store. SQLite remains the active
implementation by default. `store.type = "pgvector"` supports the durable
chunk/query path, core turn/cost/conscious persistence, Redis-backed
coordination for whispers plus fetch-session limits, and operator reporting via
`ncp status`, `ncp cost`, and `ncp explain`.

To run the live pgvector integration suite against the local containerized
stack:

```bash
./scripts/test_pgvector_integration.sh
```

This runner brings up both Postgres/pgvector and Redis, then validates durable
pgvector behavior, reporting parity, and Redis-backed coordination on the same
local stack.

## Run the examples

```bash
python3 examples/01_quickstart.py
python3 examples/02_multi_agent.py
```

## Claude Code setup

1. Initialize the repo with `ncp init`
2. Copy the example MCP config:

```bash
cp examples/06_claude_code/mcp_servers.json .mcp.json
```

3. Start the HTTP/SSE MCP server:

```bash
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/your/project
```

Expected tools:

- `ncp_get_context`
- `ncp_write_memory`
- `ncp_emit_whisper`
- `ncp_fetch`

## Codex CLI setup

1. Initialize the repo with `ncp init`
2. Copy the Codex MCP example config from `examples/07_codex_cli/` into the MCP
   config location your Codex build uses
3. Start the HTTP/SSE MCP server:

```bash
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/your/project
```

Recommended session loop:

1. call `ncp_get_context`
2. do the provider turn
3. persist durable memory with `ncp_write_memory`
4. use `ncp_fetch` only for bounded retrieval

## How multi-tool sharing works

Each coding tool (Claude Code, Codex, OpenCode) connects to one shared
`ncp serve` process over HTTP/SSE. They all read and write to the same
`.ncp/store.db` SQLite file.

```
Claude Code  ─┐
Codex        ─┼→  ncp serve (HTTP/SSE)  →  .ncp/store.db
OpenCode     ─┘
```

The store is the shared memory, not the process. A memory written by an agent
in Claude Code is visible to an agent in Codex on its next `ncp_get_context`
call, because they point at the same database.

You do not need to coordinate multiple local MCP processes. The important part
is that each MCP config should point at the same NCP server:

- SSE discovery: `http://127.0.0.1:4242/sse`
- JSON-RPC POST endpoint: `http://127.0.0.1:4242/mcp`

When the client supports HTTP transport directly, configure `http://127.0.0.1:4242/mcp`.
Keep `/sse` available as the discovery stream.

## Dogfood loop

Run the deterministic MCP proof:

```bash
ncp dogfood
```

## Whisper handoff loop

NCP can also drive a bounded partner/reviewer loop over its own whisper queue:

```bash
ncp emit --cwd /path/to/your/project --from-agent codex --to claude --type share --pipeline-id pipe_demo --payload "slice=pgvector files=ncp/stores/pgvector.py ask=implement_and_handoff"
ncp handoff claude --cwd /path/to/your/project --pipeline-id pipe_demo --emit-to opencode
ncp handoff opencode --cwd /path/to/your/project --pipeline-id pipe_demo --emit-to claude
```

Operational notes:

- handoff queue reads are non-destructive until the provider run succeeds
- Claude works best as the bounded implementation/planning partner in this loop
- OpenCode works well as the bounded reviewer
- the public value of the loop is not only coordination correctness, but prompt-size reduction from whisper-based task deltas instead of replaying the full task prompt
- with `store.type = "pgvector"`, the same loop now works through Redis-backed coordination instead of requiring a SQLite store

In the current live Sarathi-managed proof for the `pgvector` storage slice, the
compact handoff route reduced one Claude planning dispatch from `677`
estimated bridge-prompt tokens to `265` estimated handoff tokens.

Run the release preflight:

```bash
bash scripts/release_preflight.sh
```
