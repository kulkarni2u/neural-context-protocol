# Codex CLI + NCP ontology

Codex uses the NCP endpoint and `AGENTS.md` turn contract from
[`examples/07_codex_cli`](../../07_codex_cli/README.md) unchanged. Enable the
`[ontology]` section from [`../config.toml`](../config.toml) on the server and
restart `ncp serve`; nothing else changes.

```bash
ncp serve --host 127.0.0.1 --port 4242 --cwd "$PWD"
# add the [mcp_servers.ncp] entry from codex/config.toml to your Codex config
python examples/13_ontology/seed_graph.py      # optional demo graph
```

- **Automatic mode:** the `ncp_get_context` call `AGENTS.md` already requires
  returns an `[NCP:ONTOLOGY]` block when the task names a known entity with
  dependency/diagnostic intent (pass `pipeline_id`, e.g. `demo`). Subagents
  dispatched with `codex exec` get it too, through the same prepended
  `ncp_get_context` instruction.
- **Explicit mode:** `ncp_ontology_capabilities`, `ncp_ontology_query`,
  `ncp_ontology_explain` appear in Codex's MCP tool list (start a new Codex
  session after enabling the ontology so the list is refreshed).

Graph results are context, not execution authorization.
