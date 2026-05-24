# Codex CLI Example

This folder shows the minimum V1 setup for running NCP inside Codex CLI.

## Files

- `mcp_servers.json` registers the NCP stdio server with Codex CLI.

## Setup

Copy the config into your Codex MCP config location, then start a session in a
project that already has `ncp init` applied.

```bash
ncp init
ncp serve --cwd /path/to/your/project
```

Use the explicit `--cwd` form in MCP configs. Some hosts spawn the server from
outside the repo root, and NCP needs the project path to resolve the right
store and config reliably.

## Session reminder

At session start, keep the working loop explicit:

1. call `ncp_get_context`
2. do the provider turn
3. write durable memory with `ncp_write_memory`
4. use `ncp_fetch` only when bounded retrieval is necessary
