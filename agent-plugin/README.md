# NCP — portable Agent Plugin

This is NCP packaged to the vendor-neutral
[Agent Plugins 1.0.0](https://agent-plugins.org) standard — a `plugin.json`
manifest, an `mcp.json` MCP server declaration, and `skills/` — so the same
directory works with any compliant client (Cursor, VS Code/Copilot, Codex
CLI's plugin support, or any other host that implements the spec), not just
Claude Code.

For Claude Code specifically, prefer
[`../claude-plugin`](../claude-plugin) instead: it's a native Claude Code
plugin (installable via `/plugin install`) with a `SessionStart` hook that
health-checks and can autostart the bus. This directory has no equivalent
hook mechanism — the Agent Plugins spec defines skills and MCP server
declarations, not lifecycle hooks — so setup is one step more manual. Both
packages point at the same running `ncp serve` instance; install whichever
matches your client, or both.

## What's in here

- `plugin.json` — manifest (`$schema`, `name`, `version`, `description`,
  `author`, `homepage`, `repository`, `license`, `keywords`).
- `mcp.json` — declares the `ncp` MCP server as `streamable-http` at
  `http://127.0.0.1:4242/mcp` (NCP's `ncp serve` HTTP/SSE endpoint).
- `skills/ncp-core/SKILL.md` — the per-turn loop, tool reference, bounded
  context discipline, and trust/calibration basics. Always relevant.
- `skills/ncp-multi-agent/SKILL.md` — whisper hygiene, subagent dispatch,
  and cross-host coordination. Load when orchestrating multiple agents.

## Setup

NCP is not bundled — it's a separate server this directory points at.
Requires Python 3.11+:

```bash
pip install neural-context-protocol
ncp init                                                    # from your project root
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/project
```

Install `agent-plugin/` with whatever mechanism your client uses to load an
Agent Plugin directory (consult your client's docs — the spec defines the
package format, not a universal install command). Once loaded, a
spec-compliant client should expose `ncp_get_context`, `ncp_write_memory`,
`ncp_emit_whisper`, `ncp_post_turn`, `ncp_fetch`, `ncp_record_decision`, and
`ncp_record_outcome` as tools, and should make `ncp-core` (and
`ncp-multi-agent`, when relevant) available as skills.

If `.ncp/config.toml` has `[server].auth_token` set, this shipped
`mcp.json` intentionally does **not** carry it (see Gaps below) — add a
client-side header override per your client's own mechanism instead of
editing a token into this file.

## Gaps / known limitations

Read this before assuming the package is more turnkey than it is:

- **No stdio transport.** NCP has a `serve-stdio` command, but it's
  registered `hidden=True` in the CLI with the docstring *"Internal
  compatibility transport used by tests and dogfood"* — it isn't a
  supported public interface today. This package therefore declares only
  `streamable-http`, which means a server must already be reachable at
  `127.0.0.1:4242` — no client can subprocess-launch NCP the way stdio
  would allow. If `serve-stdio` is ever promoted to a stable, documented
  command, a stdio entry (with `command: "ncp"`, `args: ["serve-stdio",
  ...]`) could be added as an alternative.
- **`${PLUGIN_ROOT}` / `${PLUGIN_DATA}` are unused.** The spec only
  expands these placeholders in a stdio server's `args`/`env`/`cwd` — not
  in an HTTP server's `url` or `headers`. Since this package is
  streamable-http-only, they don't appear anywhere in `mcp.json`.
- **No project-root concept.** Agent Plugins gives a client-managed
  `PLUGIN_DATA` directory, but NCP's state (`.ncp/config.toml`, the store
  file) is scoped to a *project* directory, which the spec has no
  placeholder for. `ncp serve --cwd <project>` must be started against the
  right directory by hand (or by your own tooling) — this package can't
  discover or set it for you.
- **No autostart, no session-start hook.** Unlike `claude-plugin/`, there
  is no lifecycle hook here. If `ncp serve` isn't already running, the
  `ncp_*` tools simply won't be available — `ncp-core`'s SKILL.md tells the
  agent to say so plainly, but nothing in this package starts the server.
- **Auth is deliberately absent, not deliberately solved.** The spec says
  "auth stays client-managed" but doesn't define a standard mechanism for
  injecting a secret into a committed `mcp.json`'s `headers`. Projects
  running `ncp serve` with `[server].auth_token` set need a client-specific
  way to attach an `Authorization: Bearer <token>` header — there's no
  portable answer to point at yet.
- **Untested against a real Agent Plugins client.** This package validates
  against the published JSON Schemas (`plugin.schema.json`,
  `mcp.schema.json`) and the Agent Skills frontmatter convention, but
  hasn't been run end-to-end inside an actual compliant client yet — do
  that before treating it as verified, not just schema-valid.
