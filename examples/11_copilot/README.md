# GitHub Copilot Example

GitHub Copilot Chat (VS Code agent mode, and Copilot on github.com) speaks
MCP directly and picks up repository-level custom instructions
automatically — there's no separate "plugin" packaging format the way
Claude Code has one. Registering the MCP server and keeping the turn
contract in `.github/copilot-instructions.md` is the whole setup.

## Files

- `mcp.json` — registers the `ncp` MCP server (copy to `.vscode/mcp.json`).
- `copilot-instructions.md` — the turn contract Copilot Chat loads
  automatically for every request in this repo (copy to
  `.github/copilot-instructions.md`).

## Setup

Requires Python 3.11+.

```bash
pip install neural-context-protocol
ncp init
mkdir -p .vscode .github
cp examples/11_copilot/mcp.json               .vscode/mcp.json
cp examples/11_copilot/copilot-instructions.md .github/copilot-instructions.md
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/your/project
```

Reload the VS Code window (or restart Copilot Chat) so it picks up
`.vscode/mcp.json`. Copilot Chat's agent mode should then list the `ncp`
tool group; if MCP tools aren't visible, confirm MCP support is enabled for
your Copilot plan/settings and that agent mode is selected.

If `.ncp/config.toml` has `[server].auth_token` set (or `ncp serve` was
started with `NCP_AUTH_TOKEN`/`--auth-token`), add an
`Authorization: Bearer <token>` header to the `ncp` entry in `mcp.json`:

```json
{
  "servers": {
    "ncp": {
      "type": "http",
      "url": "http://127.0.0.1:4242/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

## What's different from Claude Code / Codex / OpenCode

Copilot has no `SessionStart`-style hook and no autostart mechanism for the
bus — `ncp serve` must already be running (or started manually) before you
open a chat. `.github/copilot-instructions.md` is always-loaded context, not
an on-demand skill, so there's no separate `/ncp`-style invocation.

## Expected tools

Once the MCP server is registered, Copilot Chat (agent mode) should see:

- `ncp_get_context`
- `ncp_write_memory`
- `ncp_emit_whisper`
- `ncp_post_turn`
- `ncp_fetch`
- `ncp_record_decision`
