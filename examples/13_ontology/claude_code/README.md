# Claude Code + NCP ontology

The ontology needs **no Claude Code plugin, hook or prompt change**. Claude
Code connects to the same NCP MCP endpoint as in
[`examples/06_claude_code`](../../06_claude_code/README.md); enabling
`[ontology]` on the server is the only change.

## Setup

```bash
cat examples/13_ontology/config.toml >> .ncp/config.toml   # or merge the [ontology] section
ncp serve --host 127.0.0.1 --port 4242 --cwd "$PWD"
cp examples/13_ontology/claude_code/.mcp.json .mcp.json     # same endpoint as 06_claude_code
python examples/13_ontology/seed_graph.py                   # optional demo graph
```

If Claude Code was already connected before you enabled the ontology, run
`/mcp` (reconnect) so it refreshes its cached tool list; automatic mode works
without the refresh.

## Automatic mode (default)

Keep the existing turn contract from `examples/06_claude_code/CLAUDE.md`:
call `ncp_get_context` at the start of the turn, with the usual arguments
plus `pipeline_id` (the ontology namespace; the demo uses `demo`). When the
task names a known entity with dependency or diagnostic intent, e.g.

```json
{"agent_id": "claude", "role": "build", "pipeline_id": "demo",
 "task": "fix_checkout_latency", "slot": "checkout", "intent": "find_upstream_dependencies"}
```

the returned context gains an `[NCP:ONTOLOGY]` block of verified,
evidence-cited paths (`Service:checkout -DEPENDS_ON-> Service:payments
ev:<chunk_id>`), inside the same `max_tokens` budget. Otherwise the context
is ordinary NCP memory.

## Explicit mode

Ask Claude dependency questions directly; it can call the listed tools:

- `ncp_ontology_capabilities` -- packs, entity/relation types, limits
- `ncp_ontology_query` -- `{"pipeline_id": "demo", "key": "payments", "entity_type": "Service", "max_hops": 2}`
- `ncp_ontology_explain` -- `{"pipeline_id": "demo", "relation_id": "<id from a query>"}`

Treat graph paths as evidence-backed context, not as permission to act: they
say what depends on what, not what Claude may change.

With a bearer token (`[server].auth_token` or `[principals]`), add
`"headers": {"Authorization": "Bearer <token>"}` to the `ncp` entry.
