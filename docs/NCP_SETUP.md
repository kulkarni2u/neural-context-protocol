# NCP Setup

This guide covers the public first-run setup for Neural Context Protocol.

## Install

Base package:

```bash
pip install ncp-sdk
```

With a provider SDK:

```bash
pip install 'ncp-sdk[providers]'
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
```

This verifies that the local SQLite-backed store can be opened and that the CLI
is wired correctly.

## Run the examples

```bash
python3 examples/01_quickstart.py
python3 examples/02_multi_agent.py
```

## Claude Code setup

1. Initialize the repo with `ncp init`
2. Copy the example MCP config:

```bash
cp examples/06_claude_code/mcp_servers.json ~/.claude/mcp_servers.json
```

3. Make sure the MCP config uses the project path explicitly via `--cwd`
4. Start the stdio MCP server:

```bash
ncp serve --cwd /path/to/your/project
```

Expected tools:

- `ncp_get_context`
- `ncp_write_memory`
- `ncp_emit_whisper`
- `ncp_fetch`

## Codex CLI setup

1. Initialize the repo with `ncp init`
2. Copy the Codex MCP example config from `examples/07_codex_cli/`
3. Make sure the MCP config uses the project path explicitly via `--cwd`
4. Start the stdio MCP server:

```bash
ncp serve --cwd /path/to/your/project
```

Recommended session loop:

1. call `ncp_get_context`
2. do the provider turn
3. persist durable memory with `ncp_write_memory`
4. use `ncp_fetch` only for bounded retrieval

## How multi-tool sharing works

Each coding tool (Claude Code, Codex, OpenCode) spawns its own `ncp serve`
process connected via stdio. They do not share a process — but they all read
and write to the same `.ncp/store.db` SQLite file.

```
Claude Code  →  ncp serve (process A)  ─┐
Codex        →  ncp serve (process B)  ─┤─  .ncp/store.db
OpenCode     →  ncp serve (process C)  ─┘
```

The store is the shared memory, not the process. A memory written by an agent
in Claude Code is visible to an agent in Codex on its next `ncp_get_context`
call, because they point at the same database.

You do not need to coordinate the processes — each tool manages its own
`ncp serve` lifecycle based on its MCP config.

The important part is that each MCP config should launch NCP with:

```bash
ncp serve --cwd /path/to/your/project
```

That avoids host-specific startup directories causing silent config/store
mis-resolution.

## Dogfood loop

Run the deterministic MCP proof:

```bash
ncp dogfood
```

Run the release preflight:

```bash
bash scripts/release_preflight.sh
```
