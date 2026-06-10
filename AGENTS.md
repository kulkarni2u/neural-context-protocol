# NCP Conventions

## Direct API (preferred when MCP tools unavailable)
Use `.ncp/run.py` — works without MCP server connection:

- **Start of turn:** run `.ncp/run.py get_context '{"agent_id":"<id>","role":"<role>","owns":[],"must_not":[],"task":"<task>","slot":"<slot>","intent":"<intent>"}'`
- **End of turn:** run `.ncp/run.py write_memory '{"content":"...","layer":"episodic|procedural|semantic|social|reasoning_trace","src":"user_verified|tool_result|agent_inferred|synthesis|subcon_retrieved","written_by":"<agent>"}'`
- **Mid-turn fetch:** run `.ncp/run.py fetch '{"query":"...","k":2}'
- **Status:** run `.ncp/run.py status`

## MCP tools (when available)
- Call `ncp_get_context` at the start of each turn once the MCP server exists.
- Write durable memory with `ncp_write_memory` at the end of each turn.
- Keep context bounded and prefer recent refs over full-history replay.

## Subagent Dispatch Template — MANDATORY

When dispatching ANY external subagent (OpenCode via `ncp handoff opencode`,
Codex via `codex exec`, or any other agent), the instruction text MUST include
both NCP calls. No exceptions — skipping them means the subagent starts cold
and its findings are lost on context compaction.

**Prepend to every instruction:**
```
First run: .ncp/run.py get_context '{"agent_id":"<role>","role":"<role>","task":"<task_slug>","slot":"build","intent":"<what_to_implement>"}'
```

**Append to every instruction:**
```
When done run: .ncp/run.py write_memory '{"content":"<one_sentence_summary_of_what_you_did_and_key_decisions>","layer":"episodic","src":"tool_result","written_by":"<role>"}'
```

**Filled-in example (OpenCode, async vector mode):**
```
First run: .ncp/run.py get_context '{"agent_id":"opencode","role":"pravaha","task":"async_vector_mode","slot":"build","intent":"implement _async_query_vector in pgvector_async.py"}'

[... implementation task ...]

When done run: .ncp/run.py write_memory '{"content":"Implemented _async_query_vector: ivfflat_probes param, cosine SELECT <=> op, to_thread auto-embed, diversity loop, retrieval_count UPDATE. 10/10 tests pass.","layer":"episodic","src":"tool_result","written_by":"opencode"}'
```

**Role names by agent:**
- OpenCode implementing → `role: pravaha`
- Codex implementing → `role: pravaha`
- OpenCode reviewing → `role: nirnaya`
- Vichara research agent → `role: vichara`
