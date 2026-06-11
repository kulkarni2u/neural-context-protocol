# 03 - LangGraph integration

This example shows NCP "sitting underneath LangGraph": a 3-node
`planner -> executor -> reviewer` pipeline that runs for two full rounds,
where all cross-agent memory lives in an NCP `SQLiteStore` instead of in the
graph state. There are two runnable variants.

## Variant A: real StateGraph (`pipeline.py`, requires langgraph)

A genuine `StateGraph` wired through LangGraph's graph runtime. Use this when
you want to see NCP integrated into an actual LangGraph application.

```bash
pip install langgraph
python3 examples/03_langgraph/pipeline.py
```

## Variant B: zero-dependency recipe (`run.py`, runs in the default test suite)

The same 3-node pipeline expressed as plain Python node functions, with no
LangGraph dependency. Use this to understand or copy the pattern without
installing anything — it's also what the default test suite runs.

```bash
python3 examples/03_langgraph/run.py
```

Both variants return the same payload shape: `mode`, `nodes`, per-node
context checks, and `pending_whispers_acknowledged`.

## What it shows

- **Bounded per-node context.** Each node calls `Assembler.assemble()` to
  pull a small slice of shared context (recent turns, retrieved chunks,
  pending whispers) from one shared SQLite store. Estimated context tokens
  (`ncp.tokens.estimate_tokens`) are printed for every node, every round, and
  stay under ~200 tokens even as the pipeline accrues history.
- **The NCP turn contract.** Every node: assembles context, does its
  (deterministic) work, then calls `Assembler.post_turn()` to log a
  `TurnRecord`, advance its `recent` ring, and write one durable
  `SubconsciousChunk` summarizing what it did.
- **Whisper handoff.** After the executor's turn, it emits a `share` whisper
  to `reviewer` carrying a `HandoffPayload`-shaped dict (`{"ask": ..., "files": [...]}`
  - see `ncp/types.py`). The reviewer's *next* `assemble()` call drains that
  whisper from its queue and prints the delivered `ask`.
- **Tiny LangGraph state.** `PipelineState` (a `TypedDict`) carries only ids,
  a round counter, and the last short message - history lives in NCP, not in
  the graph.

## No API keys

Every node is a deterministic Python function standing in for an LLM call.
Each spot where a real model would be invoked is marked in `pipeline.py`
with:

```python
# >>> real model call would go here <<<
```

Swap that line for e.g. `llm.invoke(assembly.context + "\n\n" + prompt)` to
wire in a real provider - the assembled `assembly.context` string is exactly
what you'd hand to the model.

## Mapping to MCP tools

For non-Python hosts (or any LangGraph node that lives in a different
process), the same three calls map directly onto NCP's MCP tools:

| Local call (this example)     | MCP tool             |
| ------------------------------ | -------------------- |
| `Assembler.assemble()`         | `ncp_get_context`     |
| `Assembler.post_turn()`        | `ncp_post_turn`       |
| `store.emit_whisper(...)`      | `ncp_emit_whisper`    |

A LangGraph node implemented in TypeScript (or any language) can call
`ncp_get_context` at the start of the node, do its work, then call
`ncp_post_turn` and (when handing off) `ncp_emit_whisper` - the same bounded,
shared-memory pattern shown here.
