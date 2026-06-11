# Claude Code Example

This folder shows the minimum V1 setup for running NCP inside Claude Code.

## Files

- `CLAUDE.md` keeps the turn contract visible inside the project.
- `mcp_servers.json` points Claude Code at the HTTP MCP endpoint.

## Setup

```bash
ncp init
cp examples/06_claude_code/mcp_servers.json .mcp.json
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/your/project
```

Then Claude Code connects to:

- `http://127.0.0.1:4242/mcp`

If `.ncp/config.toml` has `[server].auth_token` set (or `ncp serve` was started
with `NCP_AUTH_TOKEN`/`--auth-token`), add an `Authorization: Bearer <token>`
header to the `ncp` entry in `mcp_servers.json`, e.g. `"headers": {"Authorization": "Bearer <token>"}`.

## Expected tools

Once the MCP server is registered, Claude Code should see:

- `ncp_get_context`
- `ncp_write_memory`
- `ncp_emit_whisper`
- `ncp_fetch`
