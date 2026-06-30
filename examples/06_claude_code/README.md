# Claude Code Example

This folder shows two setups for running NCP inside Claude Code:

- **Minimal** — register the MCP server and keep the turn contract visible.
- **Zero-touch** — add a SessionStart hook that starts the bus and tells the
  agent (and its subagents) to use NCP for all agent-to-agent communication.

## Files

- `mcp_servers.json` — points Claude Code at the HTTP MCP endpoint.
- `CLAUDE.md` — keeps the turn contract visible inside the project.
- `settings.json` — a SessionStart hook config (copy to `.claude/settings.json`).
- `hooks/ncp-session-start.sh` — the hook: health-checks/starts `ncp serve` and
  injects the "route all agent comms through NCP" instruction at session start.
- `skills/ncp/SKILL.md` — a lite `/ncp` skill the agent can invoke on demand.

## Minimal setup

```bash
ncp init
cp examples/06_claude_code/mcp_servers.json .mcp.json
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/your/project
```

Claude Code then connects to `http://127.0.0.1:4242/mcp`.

## Zero-touch setup (SessionStart hook + skill)

From your project root:

```bash
ncp init
cp examples/06_claude_code/mcp_servers.json .mcp.json
mkdir -p .claude/hooks .claude/skills/ncp
cp examples/06_claude_code/settings.json            .claude/settings.json
cp examples/06_claude_code/hooks/ncp-session-start.sh .claude/hooks/
cp examples/06_claude_code/skills/ncp/SKILL.md       .claude/skills/ncp/
chmod +x .claude/hooks/ncp-session-start.sh
```

Now every Claude Code session in this project will, at start:

1. **Ensure the bus is running** — health-check `127.0.0.1:4242/healthz` and, if
   it's down, launch `ncp serve` in the background (logs to `.ncp/serve.log`).
   Set `NCP_AUTOSTART=0` to health-check only.
2. **Inject the protocol instruction** — tell the agent to drive turns through
   `ncp_get_context` / `ncp_write_memory`, coordinate via `ncp_emit_whisper`,
   and — critically — to prepend `ncp_get_context` / append `ncp_write_memory`
   when it dispatches **subagents** (per the `AGENTS.md` dispatch template).

The `/ncp` skill carries the same protocol guidance for on-demand use.

If `.ncp/config.toml` has `[server].auth_token` set (or `ncp serve` was started
with `NCP_AUTH_TOKEN`/`--auth-token`), add an `Authorization: Bearer <token>`
header to the `ncp` entry in `mcp_servers.json`, e.g.
`"headers": {"Authorization": "Bearer <token>"}`.

## What the hook can and can't do

The hook + skill **instruct** the agent to use NCP for all agent-to-agent
communication and make the bus available without a manual step. They cannot
**enforce** it — the model still issues the tool calls. Reliable subagent
coverage comes from the combination: MCP tools registered + the always-loaded
`AGENTS.md` rule + the mandatory dispatch template + the SessionStart nudge.

## Expected tools

Once the MCP server is registered, Claude Code should see:

- `ncp_get_context`
- `ncp_write_memory`
- `ncp_emit_whisper`
- `ncp_post_turn`
- `ncp_fetch`
- `ncp_record_decision`
