# Sarathi (or any orchestrator) + NCP ontology

Sarathi is an orchestrator that runs *on top of* NCP; NCP has no Sarathi
dependency and the ontology adds none. Sarathi talks to NCP like any other
generic MCP client, so the integration is the same JSON-RPC shown in
[`../mcp_client.py`](../mcp_client.py):

| Sarathi step | NCP call | Ontology effect |
|---|---|---|
| Assemble a worker's context | `ncp_get_context` (unchanged arguments, plus the run's `pipeline_id`) | Automatic `[NCP:ONTOLOGY]` block when the task names a known entity with dependency intent |
| Plan / impact analysis before dispatch | `ncp_ontology_query` (`key`, `entity_type`, `max_hops`) | Bounded, verified, evidence-backed paths |
| Audit a fact a worker relied on | `ncp_ontology_explain` (`relation_id`) | Evidence rows, liveness and validity |
| Ingest a verified fact (e.g. from a service catalog) | `ncp_write_memory` (evidence) then `ncp_ontology_assert` | Requires a principal with `verify` for `status: "verified"` |

Rules of the road:

- **No duplicate orchestration.** Sarathi decides what runs and when; NCP
  only supplies context and advisory facts. A graph path is never an
  authorization to act.
- **Tenancy.** Use one `pipeline_id` per run or project; entities and
  relations are only visible inside their pipeline (plus `global`).
- **Credentials.** Give Sarathi's ingestion step its own `[principals]` entry
  with `capabilities = ["verify"]` and `pipelines = [...]`; give workers
  principals without `verify` so they can read and propose `candidate`
  relations but cannot verify them.
- **Capability discovery.** Call `ncp_ontology_capabilities` once per session
  and skip the explicit steps when it reports `enabled: false` (or the tool
  is absent); automatic mode needs no discovery at all.
