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

3. Start the stdio MCP server:

```bash
ncp serve
```

Expected tools:

- `ncp_get_context`
- `ncp_write_memory`
- `ncp_emit_whisper`
- `ncp_fetch`

## Codex CLI setup

1. Initialize the repo with `ncp init`
2. Copy the Codex MCP example config from `examples/07_codex_cli/`
3. Start the stdio MCP server:

```bash
ncp serve
```

Recommended session loop:

1. call `ncp_get_context`
2. do the provider turn
3. persist durable memory with `ncp_write_memory`
4. use `ncp_fetch` only for bounded retrieval

## Dogfood loop

Run the deterministic MCP proof:

```bash
ncp dogfood
```

Run the release preflight:

```bash
bash scripts/release_preflight.sh
```
