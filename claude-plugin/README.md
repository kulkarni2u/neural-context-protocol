# NCP — Claude Code plugin

This directory is the installable Claude Code plugin for NCP. It bundles the
same MCP registration, `SessionStart` hook, and skill as
[`examples/06_claude_code`](../examples/06_claude_code), packaged so Claude
Code's plugin manager can install and update it instead of copying files by
hand.

## Install

```
/plugin marketplace add kulkarni2u/neural-context-protocol
/plugin install ncp@neural-context-protocol
```

Then, from your project (requires Python 3.11+):

```bash
pip install neural-context-protocol
ncp init
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/your/project
```

The plugin's `SessionStart` hook health-checks `127.0.0.1:4242/healthz` and
autostarts `ncp serve` if it's down (set `NCP_AUTOSTART=0` to disable), then
injects the protocol instruction — including the mandatory subagent dispatch
rule — into the session. The `/ncp` skill carries the same guidance for
on-demand use.

If `.ncp/config.toml` has `[server].auth_token` set, add an
`Authorization: Bearer <token>` header to the `ncp` entry — either edit this
plugin's `.mcp.json` locally, or register a project-level `.mcp.json` entry
(project config takes precedence) as shown in
[`examples/06_claude_code`](../examples/06_claude_code).

## Layout

- `.claude-plugin/plugin.json` — plugin manifest.
- `.mcp.json` — registers the `ncp` MCP server (`http://127.0.0.1:4242/mcp`).
- `hooks/hooks.json` + `hooks/scripts/ncp-session-start.sh` — the
  `SessionStart` hook that starts/checks the bus and injects the turn
  contract.
- `skills/ncp/SKILL.md` — the on-demand `/ncp` skill.

## Manual setup instead

Prefer copying files into your project directly instead of installing
through the plugin manager? Use
[`examples/06_claude_code`](../examples/06_claude_code) — same behavior,
no marketplace step.
