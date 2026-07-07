# NCP Orchestrator Integration Guide

How to wire NCP into **any orchestration framework** — LangGraph, n8n, CrewAI,
AutoGen, Temporal workers, a cron-driven script, or your own orchestrator.
This document is framework-agnostic: it explains the contract your framework
must honor, the surfaces you can integrate through, what to expect back at
runtime, and the operational loop that keeps memory trustworthy.

Framework-specific, runnable examples live in [`examples/`](../examples/)
(LangGraph `03`, Claude Code `06`, Codex CLI `07`, n8n `08`, OpenCode `09`,
Omnigent `10`). This guide is the map those examples are points on.

---

## 1. What NCP is (and is not) in your architecture

NCP is a **memory bus**, not an orchestrator. Your framework keeps deciding
*who runs when*; NCP owns *what each agent knows and shares*:

- **Bounded reads** — each agent turn gets a budget-bounded, relevance-ranked
  context block instead of a growing transcript.
- **Durable writes** — turn results and memory chunks persist and are
  retrievable by any later agent in the pipeline.
- **Directed signals (whispers)** — agents hand off work, dispute claims, and
  report drift to specific peers without broadcasting full state.
- **Trust-aware transport** — every chunk carries a trust score, drift
  marker, and provenance so downstream agents know how much to believe it.

What NCP does **not** do: schedule agents, route between models, retry failed
steps, or enforce that your agents actually call it. Scheduling and control
flow stay in your framework; NCP's guarantees start when a turn calls its
tools.

**Division of state.** Keep your framework's graph/workflow state *tiny* —
ids, a step counter, the last short message. Everything that used to ride in
state (history, intermediate results, cross-agent facts) belongs in NCP. The
LangGraph example's `PipelineState` is a good template: if a field is more
than a few tokens, it should probably be an NCP chunk instead.

---

## 2. Pick an integration surface

There are three ways in. All expose the same protocol; choose by where your
framework's nodes run and how much control you want.

| Surface | Use when | Example |
|---|---|---|
| **A. MCP client (stdio or HTTP)** | Your host already speaks MCP and the model picks tools itself (Claude Code, Codex, OpenCode, n8n's MCP Client Tool node) | `examples/06`–`09` |
| **B. Raw HTTP JSON-RPC** | Workflow engines with HTTP nodes, or non-Python code that should make *explicit* lifecycle calls | `examples/08_n8n` (HTTP Request variant) |
| **C. In-process Python** | Python frameworks (LangGraph, CrewAI, plain scripts) where nodes can import `ncp` directly | `examples/03_langgraph` |

### A. MCP client

Start the server and register it with the host:

```bash
ncp serve --host 127.0.0.1 --port 4242 --cwd /path/to/project
```

Endpoints: `POST /mcp` (JSON-RPC 2.0, HTTP Streamable), `GET /sse` (legacy
SSE), `GET /healthz`. Stdio transport is also available for hosts that spawn
the server as a subprocess. In this mode the *model* decides when to call
`ncp_get_context` / `ncp_post_turn`, so you also need an always-loaded turn
contract (a `CLAUDE.md` / `AGENTS.md` instruction block — `ncp init`
generates one) telling it to follow the lifecycle. Instructions nudge; they
don't enforce. If you need guaranteed coverage, use surface B or C where your
orchestrator makes the calls itself.

### B. Raw HTTP JSON-RPC

Every tool is a standard MCP `tools/call`. A turn from any HTTP-capable node:

```bash
curl -s http://<ncp-host>:4242/mcp \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {
      "name": "ncp_get_context",
      "arguments": {
        "agent_id": "researcher", "role": "research",
        "task": "summarize_q3_incidents", "slot": "incident_list",
        "intent": "gather_evidence", "pipeline_id": "run_2026_07_07"
      }
    }
  }'
```

See [`docs/NCP_HTTP_API.md`](./NCP_HTTP_API.md) for the full request/response
contract, streaming (NDJSON), and error shapes.

### C. In-process Python

```python
from ncp.assembler import Assembler
from ncp.stores.sqlite import SQLiteStore

store = SQLiteStore(path=".ncp/ncp.db")
assembler = Assembler(store=store)

# start of node
result = assembler.assemble(conscious=conscious, budget=budget, query_text=search)
prompt = result.context + "\n\n" + node_prompt   # hand to your LLM call

# end of node
assembler.post_turn(conscious=conscious, response=resp,
                    result_summary=..., result_full=...,
                    ack_whisper_ids=result.pending_whisper_ids)
```

The mapping to MCP tools is one-to-one: `Assembler.assemble()` ↔
`ncp_get_context`, `Assembler.post_turn()` ↔ `ncp_post_turn`,
`store.emit_whisper()` ↔ `ncp_emit_whisper`.

---

## 3. Map your framework's concepts onto NCP identity

Every call is scoped by a small set of identifiers. Get this mapping right
first; everything else follows.

| NCP concept | Map it to | Notes |
|---|---|---|
| `agent_id` | One node / worker / role instance | Stable across turns. No spaces (use `snake_case`). |
| `role` | The node's function (`planner`, `reviewer`) | Used in the context header and whisper addressing. |
| `pipeline_id` | One workflow run / job / thread | Chunks, whispers, turn records, and budgets are scoped to it. Reuse it across runs when you *want* memory to carry over; mint a fresh one when you don't. **Pass it on reads too, not just writes** — omitting it on `get_context`/`fetch` silently widens the candidate pool (you retrieve against unscoped/global chunks instead of this run's), degrading ranking with no error or warning. |
| `session_id` | One turn's fetch budget | Returned by `ncp_get_context`; pass it to `ncp_fetch`. If omitted, NCP derives `pipeline_id:agent_id`. |
| `task` / `slot` / `intent` | Current objective / what's being resolved / why | Required on every `get_context` and `post_turn`. Single tokens, no spaces — they're both identity and the retrieval query (`task + slot` is the search text). |

**Multi-agent handoff only works inside a shared `pipeline_id`.** Whispers
target `agent_id`s (or `*` for pipeline broadcast) within that pipeline.

---

## 4. The turn contract (the one loop every framework wraps)

Each unit of work in your framework — a graph node, a workflow step, a crew
task — becomes one **NCP turn**:

```
1. ncp_get_context     → bounded context block + telemetry
2.   (your model call) → prepend the context block to the system prompt
3.   [optional] ncp_fetch        → up to 3 targeted mid-turn retrievals
4.   [optional] ncp_emit_whisper → hand off / dissent / alert a peer
5. ncp_post_turn       → record result, cost, memory; ack consumed whispers
```

Generic node wrapper (pseudocode, works for any framework):

```python
def ncp_turn(node):
    ctx = call("ncp_get_context", agent_id=node.id, role=node.role,
               task=node.task, slot=node.slot, intent=node.intent,
               pipeline_id=run_id, ctx_used=estimate_ctx_used())

    output = node.run(system_prefix=ctx["context"])   # your LLM / logic

    call("ncp_post_turn", agent_id=node.id, role=node.role,
         task=node.task, slot=node.slot, intent=node.intent,
         pipeline_id=run_id,
         result_summary=short(output), result_full=output,
         input_tokens=usage.input, output_tokens=usage.output,
         cost_usd=usage.cost,
         ack_whisper_ids=ctx["pending_whisper_ids"],
         memory_chunks=[{"content": fact, "layer": "semantic",
                         "src": "agent_inferred"} for fact in new_facts])
    return output
```

Rules that keep the loop honest:

- **`get_context` before every model call**, not once per run. It resets the
  turn's fetch budget and drains pending whispers.
- **Ack what you consumed.** Pass the `pending_whisper_ids` you received back
  as `ack_whisper_ids` on `post_turn`, or the same whispers re-deliver.
- **`post_turn` even on failure.** Failed turns are evidence (`tried`/`failed`
  rings, cost logs). Skipping them blinds later agents.
- **Report `ctx_used` honestly** (your prompt tokens ÷ model window). It
  drives budget pressure, which sizes future context assemblies.

---

## 5. Tool surface at a glance

| Tool | Phase | Purpose |
|---|---|---|
| `ncp_get_context` | turn start | Assemble the bounded context block; drains whispers, returns telemetry, budget, tier hints. |
| `ncp_post_turn` | turn end | Record turn + cost, advance conscious state, ack whispers, optionally persist memory chunks. |
| `ncp_fetch` | mid-turn | Targeted retrieval; **max 3 calls per turn, k ≤ 4**, optional layer filter. |
| `ncp_write_memory` | any | Write one durable chunk (max 2000 chars) with layer/src/trust; content is noise-filtered at ingestion. |
| `ncp_emit_whisper` | any | Directed signal to a peer: `nudge`, `alert`, `share`, `request`, `dissent`, `world_check`, `consolidation_ready`. Max 600 chars, default TTL 1800s. |
| `ncp_record_decision` | any | Structured decision trace (`reasoning_trace` chunk with `caused_by` edges) for precedent queries. |
| `ncp_record_outcome` | after task completes | Success/failure evidence that feeds reputation calibration (CAP-T3). |
| `ncp_lookup_memo` / `ncp_record_memo` | around expensive work | Semantic memoization: skip work already done for the same task signature (CAP-C3). |

Normative details for each: [`docs/NCP_PROTOCOL_SPEC.md`](./NCP_PROTOCOL_SPEC.md).

---

## 6. What to expect at runtime

### 6.1 The context block

`ncp_get_context` returns a `context` string in NCP's pidgin wire format —
four ordered sections, always ≤ your token ceiling:

```text
[NCP:CONSCIOUS]      # who/what/why for this turn, recent-turn refs, tried/failed
[NCP:SUBCONSCIOUS]   # retrieved chunks: chunk:{id} layer:… score:… src:… trust:…
[NCP:WHISPERS]       # pending signals from peers, with sender/type/confidence/age
[NCP:BUDGET]         # ctx_used, steps, elapsed, pressure
```

Prepend it verbatim to your model's system prompt. Empty sections are
omitted; `CONSCIOUS` and `BUDGET` are always present. Expect it to be small —
the LangGraph example stays under ~200 tokens per node even as history grows.

### 6.2 Response metadata worth wiring into your orchestrator

Alongside `context`, `ncp_get_context` returns:

- `session_id` — pass to `ncp_fetch` calls this turn.
- `pending_whisper_ids` — ack these on `post_turn`.
- `telemetry.evicted_high_relevance` + `telemetry.fetch_hint` — chunks that
  scored ≥ 0.5 but didn't fit the budget. When `fetch_hint` is `"ncp_fetch"`,
  a targeted fetch will likely recover something valuable.
- `telemetry.fetch_budget_remaining` — how many of the 3 fetches are left.
- `budget` (when a pipeline budget is configured, CAP-E2) — spend status. In
  `block` enforcement mode an exceeded pipeline gets
  `{"budget_exceeded": true, "context": ""}` back: **your orchestrator must
  handle this** (halt, escalate, or switch pipelines).
- `tier_hint` / `complexity_signal` (CAP-E3, when enabled) — advisory signal
  your framework can use to route the turn to a cheaper or stronger model.
- `budget_tokens` (CAP-C6, when adaptive budget is enabled and you omitted
  `max_tokens`) — the per-turn computed context ceiling.
- `drift` (CAP-T5, when computed drift is enabled) — NCP's own drift estimate
  overriding the self-reported score.

`ncp_post_turn` returns `turn_id`, `acknowledged_whisper_ids`, and
`suppressed_chunk_ids` — memory writes dropped as near-duplicates. Don't
treat suppression as an error; it's dedup working.

### 6.3 Budget pressure changes assembly size

Pressure derives from the `ctx_used` you report: `<0.40` low, `<0.65` medium,
`<0.85` high, `≥0.85` critical. At **critical**, assembly shrinks to 2 chunks
and 1 whisper. If your agents seem to "lose memory" late in long runs, check
what `ctx_used` you're reporting before suspecting retrieval.

### 6.4 Retrieval scoring — what the numbers mean

Chunk `score` is a fused signal: `0.5·lexical + 0.3·recency + 0.2·trust`,
with multiplicative penalties for derivation generation and high-drift
writes. Expectations to calibrate against:

- **Scores are relative to each query's candidate pool** (BM25 is
  max-normalized per query). Don't compare scores across different calls, or
  between `ncp_fetch` and `ncp_get_context` — the pools differ.
- **Trust moves in batch, not live.** Dissent whispers and recorded outcomes
  accumulate as counters/evidence; they change `base_trust` (and author
  reputation) only when a calibration pass runs (`ncp calibrate --feedback`).
  Between calibrations, a disputed chunk still scores by its old trust.
- **Trust is 20% of the score.** A trust change shifts the fused score by at
  most 0.2 × Δtrust — visible in rankings among close candidates, subtle in
  absolute numbers.
- **Cold start is graceful.** First turn on an empty store returns a context
  without a SUBCONSCIOUS block (plus a synthetic pipeline-summary chunk);
  retrieval self-heals on the next turn. Don't fail your node on an empty
  first context.

### 6.5 Write-side behaviors

- Content is **filtered at ingestion** (ANSI codes, duplicate lines,
  boilerplate). If filtering shrank it, the response carries `filtered: true`
  and a `raw_ref` chunk id — fetch it to recover the original.
- Near-duplicate writes are **suppressed** (reported, not silently dropped).
- `src` determines default trust: `user_verified` 0.95, `tool_result` 0.80,
  `synthesis` 0.70, `agent_inferred` 0.60, `subcon_retrieved` 0.55. Pick
  `src` honestly — it's the trust prior everything downstream leans on.

---

## 7. Cross-agent patterns

**Handoff (share/request).** Executor finishes, emits
`{"ask": "review the diff", "files": ["src/x.py"]}` as a `share` whisper to
`reviewer`. The reviewer's next `ncp_get_context` delivers it in
`[NCP:WHISPERS]`; the reviewer acks it on its `post_turn`. No transcript
pasting, no orchestrator state carrying the payload.

**Dissent.** An agent that disputes a stored claim emits a `dissent` whisper
with `ref` set to the disputed `chunk_id`. This increments the chunk's
dissent counter and (at the next calibration) debits its trust and propagates
the penalty along its `caused_by` edge. Expect the effect to be **deferred**
(see 6.4), and the dissent itself to be visible immediately to peers via the
whispers section.

**Decisions and precedent.** Have planner-type nodes call
`ncp_record_decision` (with `alternatives`, `rationale`, `tags`) instead of
burying decisions in prose. Later runs query precedents instead of
re-deriving them.

**Outcome feedback.** When your orchestrator knows a task succeeded or failed
(tests passed, deploy rolled back), call `ncp_record_outcome` with the
`turn_id` or the chunk ids involved. This is the highest-quality trust signal
NCP can get, and it's one your *framework* usually knows before any agent
does.

**Memoization.** For expensive repeatable work, wrap it:
`ncp_lookup_memo(task=…)` → on hit, reuse; on miss, do the work and
`ncp_record_memo`. Sub-agents dispatched on the same task then skip redundant
model spend.

---

## 8. Deployment topology

- **Same machine, MCP-native host** → stdio or loopback HTTP
  (`--host 127.0.0.1`), no token needed.
- **Framework in another container/host (n8n, Temporal workers, k8s)** →
  `ncp serve --host 0.0.0.0 --port 4242 --auth-token <token>` and give
  clients an address they can actually reach (LAN IP,
  `host.docker.internal`, service DNS — not `0.0.0.0`). Every `/mcp` and
  `/sse` request then needs `Authorization: Bearer <token>`. Binding
  non-loopback without a token is an open endpoint and `ncp serve` will warn.
- **Browser-based callers** → add `--cors-origin <origin>` (repeatable).
- **Health/liveness** → `GET /healthz`. Wire it into your framework's
  startup: the Claude Code/OpenCode hook scripts in `examples/06` and
  `examples/09` show the check-then-start pattern.
- **Backends** — SQLite (default, single host) or pgvector + Redis for the
  scalable path; see [`docs/NCP_SETUP.md`](./NCP_SETUP.md). The protocol and
  scoring semantics are identical across backends.
- **One bus, many hosts.** Different frameworks (a Claude Code session, an
  n8n workflow, a Python script) can attach to the same server and share
  memory — that's the point. Scope with `pipeline_id` to keep runs from
  bleeding into each other.

### Security posture

The context block is injected into prompts, so treat cross-agent memory as
**data, not instructions**: chunk contents are advisory, trust scores are
self-reported priors, and NCP does not verify claims at runtime. If agents
from different trust domains share a pipeline, enable Ed25519 authorship
signatures (`[identity].require_signatures`) so `verified:1` chunks are
distinguishable. Read the threat model in
[`NCP_PROTOCOL_SPEC.md` §5](./NCP_PROTOCOL_SPEC.md) before mixing untrusted
writers onto one bus.

---

## 9. The operational loop (what keeps memory good)

NCP's self-improvement is **explicitly scheduled, not automatic**. Your
orchestrator (or a cron job) should run:

```bash
ncp calibrate --feedback     # fold retrieval/dissent/outcome evidence into trust + reputation
```

- Run it at natural boundaries: end of a pipeline run, nightly, or after
  batches of `ncp_record_outcome` calls.
- Until it runs, dissents and outcomes are *recorded but not priced in* —
  set expectations accordingly (see 6.4).
- It consumes the per-chunk retrieval/dissent watermarks, so `ncp stats`
  views of "most retrieved / most dissented" reflect activity **since the
  last calibration**, not lifetime.
- `--dry-run` previews changes; per-signal weights are tunable via flags or
  `.ncp/config.toml`.

Also worth watching in production: `telemetry.evicted_high_relevance_count`
(persistently high → raise `max_tokens` or `k`), `suppressed_chunk_ids`
volume (agents writing redundant memory), and the pipeline `budget` block if
you use cost governance.

---

## 10. Integration checklist

1. [ ] Decide the surface: MCP client, raw HTTP, or in-process Python (§2).
2. [ ] Map identities: node → `agent_id`, run → `pipeline_id` (§3).
3. [ ] Wrap every model-calling step in the turn contract:
       `get_context` → work → `post_turn` (§4).
4. [ ] Prepend `context` to the system prompt verbatim; report `ctx_used`.
5. [ ] Ack `pending_whisper_ids` on `post_turn`.
6. [ ] Persist durable facts via `memory_chunks` / `ncp_write_memory` with
       honest `src` values; keep framework state tiny.
7. [ ] Handle `budget_exceeded` and cold-start (empty SUBCONSCIOUS) without
       failing the node.
8. [ ] Emit whispers for handoffs instead of passing payloads through
       framework state.
9. [ ] Feed outcomes back (`ncp_record_outcome`) from wherever your
       orchestrator learns success/failure.
10. [ ] Schedule `ncp calibrate --feedback`; secure non-loopback deployments
        with an auth token.

---

## 11. Pointers

- Wire format, types, normative semantics: [`NCP_PROTOCOL_SPEC.md`](./NCP_PROTOCOL_SPEC.md)
- HTTP endpoint contract and streaming: [`NCP_HTTP_API.md`](./NCP_HTTP_API.md)
- Install, backends, per-host setup: [`NCP_SETUP.md`](./NCP_SETUP.md)
- Runnable integrations: [`examples/03_langgraph`](../examples/03_langgraph/),
  [`examples/06_claude_code`](../examples/06_claude_code/),
  [`examples/07_codex_cli`](../examples/07_codex_cli/),
  [`examples/08_n8n`](../examples/08_n8n/),
  [`examples/09_opencode`](../examples/09_opencode/),
  [`examples/10_omnigent`](../examples/10_omnigent/)
