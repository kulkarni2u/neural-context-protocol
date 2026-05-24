# Codex CLI Example

This folder shows the minimum V1 setup for running NCP inside Codex CLI.

## Files

- `mcp_servers.json` points Codex CLI at the HTTP MCP endpoint.

## Setup

Copy the config into your Codex MCP config location, then start a session in a
project that already has `ncp init` applied.

```bash
ncp init
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/your/project
```

Then point Codex CLI at:

- `http://127.0.0.1:4242/mcp`

## Session reminder

At session start, keep the working loop explicit:

1. call `ncp_get_context`
2. do the provider turn
3. write durable memory with `ncp_write_memory`
4. use `ncp_fetch` only when bounded retrieval is necessary
