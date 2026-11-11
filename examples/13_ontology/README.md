# Ontology example: an evidence-backed infrastructure graph over MCP

NCP's ontology is an opt-in domain graph (services, databases, APIs, Kafka
topics, teams) whose relations must cite evidence -- normally NCP memory
chunks. Harnesses use it through the MCP endpoint they already connect to:

- **Automatic mode (zero harness changes):** an ordinary `ncp_get_context`
  call returns an extra `[NCP:ONTOLOGY]` block when the task names a known
  entity with dependency or diagnostic intent.
- **Explicit mode:** `ncp_ontology_capabilities` -> `ncp_ontology_query` ->
  `ncp_ontology_explain` for graph questions.

Graph results are context, not execution authorization. Full reference:
[`docs/NCP_ONTOLOGY.md`](../../docs/NCP_ONTOLOGY.md).

## Files

| File | Purpose |
|---|---|
| `config.toml` | `[ontology]` section to merge into `.ncp/config.toml` |
| `mcp_http.py` | Small standard-library JSON-RPC-over-HTTP client used by both scripts |
| `seed_graph.py` | Seeds a small estate: evidence chunks via `ncp_write_memory`, entities and verified relations via `ncp_ontology_assert` |
| `mcp_client.py` | Generic MCP client: discovery, automatic mode, explicit query + explain |
| `claude_code/` | `.mcp.json` and usage notes for Claude Code |
| `codex/` | `config.toml` MCP entry and usage notes for Codex CLI |
| `sarathi/` | How an orchestrator (Sarathi or any other) uses the same calls |

## Run it

```bash
pip install neural-context-protocol
ncp init
cat examples/13_ontology/config.toml >> .ncp/config.toml
ncp serve --host 127.0.0.1 --port 4242 --cwd "$PWD"

# in another shell
python examples/13_ontology/seed_graph.py
python examples/13_ontology/mcp_client.py
```

`seed_graph.py` is idempotent (fixed chunk ids, keys, idempotency keys and
timestamps). Both scripts accept `--url` (default `$NCP_MCP_URL` or
`http://127.0.0.1:4242/mcp`) and send `Authorization: Bearer $NCP_AUTH_TOKEN`
when that variable is set.

### The seeded graph (pipeline `demo`)

```
edge-gateway -DEPENDS_ON-> checkout -DEPENDS_ON-> payments -DEPENDS_ON-> ledger-db
                               |                      \--DEPENDS_ON-> card-network-api
                               |--DEPENDS_ON-> inventory -DEPENDS_ON-> inventory-db
                               |--DEPENDS_ON-> orders-db
                               \--PRODUCES--> order-events <-CONSUMES- inventory
team-commerce -OWNS-> edge-gateway, checkout, inventory     team-payments -OWNS-> payments
```

Each arrow is a `verified` relation citing one evidence chunk (deployment
manifest notes, runbook lines, CODEOWNERS). One more relation,
`edge-gateway -DEPENDS_ON-> payments`, is asserted as `candidate` from an
"unconfirmed agent note": it never appears in automatic context or in
`verified_only` queries. Deployments: the v1 `infrastructure` pack has no
Deployment type, so environment/version/manifest live in Service attributes
(never serialized into automatic context) and in the evidence chunks.

### Expected output (abridged)

```
ontology tools: ncp_ontology_assert, ncp_ontology_capabilities, ncp_ontology_explain, ncp_ontology_query, ncp_ontology_revoke

== automatic mode: ncp_get_context ==
[NCP:CONSCIOUS]
...
[NCP:SUBCONSCIOUS]
chunk:demo-ev-team-commerce-owns-checkout layer:semantic score:1.0 src:tool_result trust:0.8
  team-commerce owns checkout (CODEOWNERS).
...
[NCP:ONTOLOGY] v:1 status:verified
path:1 score:0.58 Service:checkout -DEPENDS_ON-> Service:payments ev:demo-ev-checkout-depends_on-payments
path:2 score:0.58 Service:checkout -DEPENDS_ON-> Service:inventory ev:demo-ev-checkout-depends_on-inventory
...
path:9 score:0.41 Service:checkout -DEPENDS_ON-> Service:payments -DEPENDS_ON-> Database:ledger-db ev:...
ontology metadata: {"triggered": true, "reason_code": "entity_relationship_intent", "verified_path_count": 13, ...}

== explicit mode ==
query 'checkout': 13 verified paths
explain <relation_id> -> {"status": "verified", ..., "verified_and_supported": true}
```

## Verifying relations: who may do it

`status: "verified"` needs the `verify` capability. `config.toml` grants it to
unauthenticated loopback callers **for this demo only**
(`unauthenticated_capabilities = ["read", "write", "verify"]`). In a shared
deployment drop that line (the default is `["read", "write"]`) and give
`verify` to the one principal that ingests trusted sources:

```toml
[principals.ontology_seeder]
token_env = "NCP_TOKEN_SEEDER"
agent_id = "ontology_seeder"
pipelines = ["demo"]
capabilities = ["verify"]
```

then run `NCP_AUTH_TOKEN=$NCP_TOKEN_SEEDER python examples/13_ontology/seed_graph.py`
(the seed writes its evidence chunks as `written_by = "ontology_seeder"`, so
keep that `agent_id`). With `[principals]` configured every HTTP client needs
its own token, and `mcp_client.py` must pass that principal's `agent_id`:
`NCP_AUTH_TOKEN=<token> python examples/13_ontology/mcp_client.py --agent-id <agent_id>`.

## What a harness has to do

Nothing ontology-specific for read-only use: connect to the MCP endpoint and
keep calling `ncp_get_context` (with a `pipeline_id`). Hosts that cache MCP
tool lists need a refresh to *see* the explicit tools; automatic mode works
without it. `tests/test_ontology_examples.py` runs both scripts against an
ephemeral loopback server.
