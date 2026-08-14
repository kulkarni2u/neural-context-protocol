---
name: ncp-multi-agent
description: Use when dispatching a subagent, coordinating with another agent or host on the same NCP bus, sending or receiving a whisper, or handing off work across a multi-agent pipeline. Builds on the ncp-core skill's per-turn loop.
---

# NCP multi-agent — whispers, dispatch, and handoff

This skill assumes the `ncp-core` per-turn loop (`ncp_get_context` /
`ncp_write_memory` / `ncp_post_turn`). It covers what changes when more
than one agent — including subagents you dispatch yourself — shares the
same NCP bus.

## Whispers: bounded, directed signals

`ncp_emit_whisper` sends a small signal to a specific agent (or `*` for a
pipeline broadcast), not a transcript. Required: `from`, `target`, `type`,
`payload`, `confidence`.

| `type`                | Use it for | Structured payload |
|------------------------|------------|---------------------|
| `share` / `request`    | Handoff — "here's what I found" or "I need X" | `{"ask": "...", "slice"?: "...", "files"?: [...]}` |
| `dissent`              | Disagreement with a specific chunk | `{"issue": "...", "alternatives"?: [...]}` |
| `alert`                | Something needs attention now | `{"alert_code": "...", "description": "..."}` |
| `world_check`          | Report detected drift from the stated intent | `{"anchor_intent": "...", "detected_drift": 0.0-1.0}` |
| `nudge` / `consolidation_ready` | Lightweight signal, plain string payload | string |

Whisper hygiene:

- Keep `payload` to the specific fact/ask (max ~600 normalized chars) —
  never paste a transcript into a whisper.
- Set `ref` to a `chunk_id` when the whisper is *about* that chunk. For
  `dissent` specifically, `ref` is what debits that chunk's trust and
  propagates the penalty along its `caused_by` edge during calibration —
  a dissent whisper without `ref` registers as a complaint but doesn't
  correct the record.
- Whispers expire (`ttl_seconds`, default 1800) — they're for active
  coordination, not long-term storage. If it needs to persist, write it
  with `ncp_write_memory` instead (or in addition).
- On the receiving end, drain whispers via `ncp_get_context`'s
  `[NCP:WHISPERS]` block, act on them, then acknowledge with
  `ncp_post_turn`'s `ack_whisper_ids`. An unacknowledged whisper isn't
  redelivered as "unread" — acknowledging is how the sender's side of the
  loop closes.

## Dispatching a subagent — mandatory prepend/append

A subagent you dispatch starts cold by default — it has no access to your
conversation. If your host supports handing it text instructions (a task
description, a delegated prompt, an `exec`-style call to another agent),
treat the following as non-optional, not a nice-to-have:

**Prepend to the subagent's instructions:**
```
First call ncp_get_context with {"agent_id":"<role>","role":"<role>","task":"<specific_task_slug>","slot":"<specific_slot_slug>","intent":"<intent_slug>"}
```

**Append to the subagent's instructions:**
```
When done call ncp_write_memory with {"content":"<one_sentence_summary_of_what_you_did_and_key_decisions>","layer":"episodic","src":"tool_result","written_by":"<role>"}
```

Skipping either half means the subagent's findings are lost the moment it
exits — there is no other channel carrying them back.

## Five habits that make dispatch actually pay off

The prepend/append above prevents a cold start; these are what convert
that into fewer tokens and better retrieval — skipping them still
"works," it just spends the budget on the wrong things.

1. **Give the subagent specific `task` and `slot` values.** Retrieval is
   scored against `task + slot`, so broad values burn the same budget on
   irrelevant chunks. All conscious pidgin fields, including `intent`, must
   be whitespace-free; use `snake_case` and let `intent` state why the turn
   is happening.
2. **Have it write back the distilled finding, not raw output.** One
   `ncp_write_memory` chunk with the specific fact beats a pasted diff or
   stack trace.
3. **Tag `layer` by what kind of knowledge it is**, not reflexively
   `episodic`. A reusable fix procedure is `procedural`; a fact that
   outlives this run is `semantic`. Mistagging silently degrades every
   later `layer`-filtered retrieval.
4. **Pass `caused_by`/`derived_from` edges when output builds on a prior
   chunk.** Without an edge, a derived finding looks like an independent
   primary source, and trust propagation has nothing to walk.
5. **Budget above the protocol floor.** The context header costs roughly
   50-60 tokens before any chunk content. Budgeting a subagent below
   ~150-200 tokens mostly pays that fixed cost instead of retrieving
   anything useful.

Once the subagent's work is validated, call `ncp_record_outcome` with the
chunk/turn IDs that informed it — this is what lets reputation and future
retrieval ranking actually reflect whether the work held up.

## Cross-host coordination

The same bus can be shared by agents on different hosts (e.g. one agent in
this client, another in a CLI tool, another in an orchestrator), each
registering their own copy of `mcp.json` against the same running
`ncp serve` endpoint. Nothing about the protocol changes — the same
`ncp_get_context` / `ncp_write_memory` / `ncp_emit_whisper` loop is how
they communicate, whether or not they share a vendor.
