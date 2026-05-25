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
```

This verifies that the local SQLite-backed store can be opened and that the CLI
is wired correctly. `ncp status` surfaces store and activity rollups; `ncp cost`
surfaces token and USD rollups from `cost_log`.

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

Run the release preflight:

```bash
bash scripts/release_preflight.sh
```
