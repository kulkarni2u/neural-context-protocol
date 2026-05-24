# Claude Code Example

This folder shows the minimum V1 setup for running NCP inside Claude Code.

## Files

- `CLAUDE.md` keeps the turn contract visible inside the project.
- `mcp_servers.json` wires Claude Code to the stdio MCP server.

## Setup

```bash
ncp init
cp examples/06_claude_code/mcp_servers.json ~/.claude/mcp_servers.json
ncp serve --cwd /path/to/your/project
```

The explicit `--cwd` matters. Some MCP hosts launch the server from a session
directory that is not your project root. Without that flag, `ncp serve` can
resolve the wrong `.ncp/config.toml` and the tools may fail to register.

## Expected tools

Once the MCP server is registered, Claude Code should see:

- `ncp_get_context`
- `ncp_write_memory`
- `ncp_emit_whisper`
- `ncp_fetch`
