# NCP Token-Efficiency Plan: Harness and Subagent Overhead

Status: **shipped in v1.9.6, except item 5 (cross-turn delta), which is in progress.** Baseline: v1.9.5. Where the code differs from the text below, the [Status](#status) table says so and the code wins.

NCP already bounds its own context block (840 tokens by default, capped chunk
and whisper counts, write-side noise filtering). This plan covers the token
spend that sits *around* that block: the tool catalog, what agents send back,
what subagents return to their parent, and repeated content across turns.

The findings come from a source read of `ncp/assembler.py`, `ncp/encoder.py`,
`ncp/mcp/server.py`, `ncp/agent_handoff.py` and `ncp/skill_cache.py`. Nothing
was measured at runtime, so every item starts by recording a baseline (see
[Measurement](#measurement)).

## Summary

| # | Item | Who benefits | PR | Default behavior changes? |
|---|------|--------------|----|---------------------------|
| 1 | Cap what a handoff returns to the parent | Orchestrator | A | Yes (`--return full` restores it) |
| 2 | Make `result_full` optional in `ncp_post_turn` | Every agent | A | No |
| 4 | Server-held ids instead of model-echoed ids | Every agent | A | No |
| 3 | Per-connection tool profile | Subagents | B | No |
| 9 | Skill cache over MCP, handoff and CLI | Subagents, orchestrator | C | No (off by default) |
| 6 | Stable prompt-cache prefix | Long-lived agents | D | Wire format (opt-out) |
| 7 | Budget accuracy and conscious-block caps | Every agent | D | Yes (stricter budget) |
| 8 | Chunk packing, fan-in and diversity defaults | Synthesis agents | D | Only if benchmarks pass |
| 5 | Cross-turn delta | Long-lived agents | E | No (opt-in) |
| 11 | More lifecycle hooks and `ncp run-turn` | Both | F | No |
| 10 | One source for lifecycle instructions | Both | G | No in phase 1 |

Order: A, B, C first. They affect every worker dispatch and touch no
retrieval logic. D and E change assembly and need the benchmark gates. G is
blocked on the provider-evaluator redesign noted in
`NCP_CONTEXT_ENGINEERING_REVIEW.md`.

## Status

| # | Item | Status | Deviation from the plan |
|---|------|--------|-------------------------|
| 1 | Handoff return cap | Shipped | Full output is stored as a chain of `raw_ref` part chunks (each part links to the next), not one chunk. `ncp run-turn` takes the same `--return` / `--max-return-chars`. `--max-return-chars` is limited to 1-2,000. |
| 2 | Optional `result_full` | Shipped | `result_ref` over the cap needs no copy (the referenced chunk is the original). Response adds `result_truncated` and `raw_ref`. |
| 4 | Server-held ids | Shipped | In-process only: not shared across server processes. Never used for the shared `__default__` session. `echo_ids` on `ncp_get_context` added. |
| 3 | Per-connection tool profile | Shipped | Narrow-only. Adds a `minimal` profile and a per-principal `tools` allowlist. |
| 9 | Skill cache over MCP, handoff, CLI | Shipped | Off by default (`[skill_cache].enabled`). `ncp_skill` is an MCP tool (the design doc's "no new tool" decision was reversed). Scope flag required on `ncp skill cache`. `POST /skills` needs the `cache_skill` capability. |
| 6 | Stable prompt-cache prefix | Shipped | `show_score` off by default. Anthropic split is offset-based. `steps:` omitted when the total is unknown. |
| 7 | Budget accuracy, conscious caps | Shipped | `chunking.max_chunk_tokens` removed rather than wired. The tiktoken property test skips when tiktoken's encoding data is unavailable offline. |
| 8 | Packing, fan-in, diversity defaults | Shipped | `diversity_lambda` 0.7, auto fan-in at 3+ writers, fit swap on. The swap picks its victim by relevance, because a lower-ranked victim can never make room. pgvector parity was not run locally. |
| 5 | Cross-turn delta | In progress | <!-- TODO delta --> |
| 11 | Lifecycle hooks and `ncp run-turn` | Shipped | One script, `ncp-lifecycle-hook.sh`, for all four events. `run-turn` hosts: claude, codex, opencode, mock. |
| 10 | One source for lifecycle instructions | Partly shipped | Generated `CLAUDE.md` and root `AGENTS.md` use `ncp/templates/turn_loop.md.example`. Provider templates are drift-checked only, not generated. |

## PR A: stop routing bulk text through the model

### 1. Cap what a handoff returns to the parent

Today `ncp handoff` prints the subprocess's full stdout to the caller
(`ncp/cli.py`, handoff command). Only the persisted completion chunk
(2,000 chars) and the follow-up whisper (600 chars) are capped.

- Add `--return summary|full` to `ncp handoff`, default `summary`.
- `summary` prints the bounded completion envelope that is already built for
  persistence (`agent_handoff.py`, completion record), plus the chunk id of
  the full output so the parent can `ncp_fetch` it.
- Store the untruncated output as a `raw_ref` chunk linked from the
  completion chunk.
- Add `--max-return-chars` (default 2,000, same ceiling as a chunk).

Acceptance: with a 20 KB provider response, stdout is at most
`--max-return-chars`, includes a fetchable chunk id, and `--return full`
reproduces today's output byte for byte.

Out of scope: capping native host subagents (Claude Code's Agent tool). NCP
does not sit on that return path; item 11 covers what hooks can do there.

### 2. Make `result_full` optional in `ncp_post_turn`

`result_full` is required (`mcp/server.py`, `ncp_post_turn` schema), so the
model re-emits its whole turn as output tokens. There is no size cap.

- Make `result_full` optional. When omitted, store `result_summary` as the
  turn result.
- Accept `result_ref`: the id of a chunk already written this turn, used as
  the full result without resending it.
- Add `[turns].result_full_max_chars` (default 8,000). Longer input is
  truncated for the turn record and the original kept as a `raw_ref` chunk.
- Update the tool description to say `result_full` is for harnesses that
  have the text for free, not something a model should retype.
- Item 11 adds a hook that posts the turn out-of-band, so the model sends
  nothing.

Acceptance: a call with only `result_summary` succeeds on all three stores;
existing callers that send `result_full` see no change below the cap.

Risk: recent-ref retrieval reads the stored turn result. Check that a
summary-only record still gives useful recent refs before changing the
generated instructions to recommend it.

### 4. Server-held ids instead of model-echoed ids

`retrieved_chunk_ids`, `pending_whisper_ids` and `session_id` are returned to
the model and then typed back.

- Record the last `ncp_get_context` result's ids on the existing per-session
  entry (`FetchSession` in `mcp/server.py`).
- `ncp_post_turn` with those fields **omitted** defaults to the recorded
  values. An explicit empty array still means "none".
- `ncp_fetch` with `session_id` omitted already resolves a session key; no
  change needed.
- Never default for the shared `__default__` session key, since two agents
  could ack each other's whispers. Return a clear error asking for
  `agent_id` or `session_id` instead.
- Add `echo_ids` (default `true`) to `ncp_get_context`. With `false`, the
  response omits both id arrays.

Acceptance: omitted ids credit the same chunks and ack the same whispers as
the explicit call; a multi-agent test shows no cross-agent ack.

## PR B: per-connection tool profile

### 3. Let one server advertise different tool sets

The profile is server-wide (`[tools].profile`, `NCP_TOOL_PROFILE`). A worker
that needs two tools still loads the default `standard` catalog of about
14,000 characters.

- HTTP: `?profile=` on `/mcp` and `/sse`, or an `X-NCP-Tool-Profile` header.
  stdio: `ncp serve --profile`.
- A connection can only **narrow** the server's configured profile, never
  widen it.
- Add a `minimal` profile: `ncp_get_context` and `ncp_write_memory`.
- With `[principals]` configured, allow an optional `tools` allowlist per
  principal, intersected with the profile.
- `tools/call` enforces the same set that `tools/list` returned for that
  connection.
- Add a snapshot test over each profile's tool definitions with a character
  budget, so catalog growth fails CI. This also closes the "schema snapshot
  test" gap in `NCP_NEXT_PHASE_GAP_MATRIX.md`.
- Update `.claude/agents/*.md` and the handoff MCP config to point workers at
  the narrow profile.

Acceptance: two clients on one server see different `tools/list` results; a
call outside the connection's set is rejected; an unknown profile name falls
back to the server profile.

## PR C: skill cache over MCP

### 9. Wire `ncp/skill_cache.py` into the agent-facing surface

The module works and is tested, but nothing calls it: no MCP tool, no CLI
command, no handoff path. This reverses the design doc's "no new MCP tool"
decision (`NCP_SKILL_CACHING_DESIGN.md` §1.5); update that doc in the same PR.

**What counts as a skill.** Any reusable content the user registers: a
workflow (a procedure every worker must follow the same way) or any other
reusable skill (a checklist, review rules, reference notes, conventions).
The module was written for third-party skill text; this widens it to
user-owned skills as the main case.

- Each skill records a `kind` in its manifest: `workflow` or `reference`.
  `ncp skill cache --kind workflow|reference`, default `reference`.
- `workflow` skills are always delivered whole and verbatim (`get`), so two
  workers never see different slices of one procedure. They are excluded
  from `recall`.
- `reference` skills can be fetched whole or recalled by section.
- A skill the user registers is seeded at the `user_verified` trust level
  (0.95), not the 0.60 default the module uses for third-party text. Content
  cached by anything other than the user's CLI or an authorised principal
  keeps 0.60 and `src="skill_ref"`.

**Where the saving comes from.** A subagent that needs a skill still pays
input tokens to read it. The savings are:

- The orchestrator dispatches a skill by reference (`skill_id@hash`, about
  10 tokens) instead of pasting the procedure into each worker prompt as
  output tokens.
- Review and checklist skills are recalled by relevance, so a worker reads
  one section, not the whole file.
- Identical verbatim text at the start of each worker prompt can hit the
  provider's prompt cache across parallel workers.

Hosts that already load a skill once per session (Claude Code's native
skills) gain little. The target is fresh-context workers.

**Write path: harness only, never the model.** A model caching a skill would
have to emit the whole text, which defeats the purpose.

- `ncp skill cache <path> --id <skill_id>` calls `ensure_cached` and prints
  `skill_id`, `content_hash`, section count and `cache_hit`.
- `ncp skill list` and `ncp skill show <skill_id>`.
- The HTTP API gets the same operation, restricted to principals with a new
  `cache_skill` capability when `[principals]` is configured.
- No server-side file reads: the CLI reads the file and sends content.

**Read path: one new MCP tool, `ncp_skill`.**

| Action | Arguments | Returns |
|--------|-----------|---------|
| `get` | `skill_id`, optional `content_hash` | Full text, verbatim, in section order |
| `recall` | `query`, `k` (max 4) | Ranked sections |
| `list` | none | `skill_id`, `content_hash`, sections, age |

- Hidden and unreachable unless `[skill_cache].enabled = true` (default
  `false`), the same gating as `ncp_task` and the memo tools.
- In the `full` profile by default; selectable per connection through PR B.
- `get` without `content_hash` resolves the newest complete manifest for
  that `skill_id`. This needs a small "latest manifest" lookup in the module.
- `get` repeated in one session for the same hash returns
  `{"unchanged": true, "content_hash": ...}` instead of the text.
- `recall` counts against the existing 3-call `ncp_fetch` budget.

**Dispatch by reference.**

- `ncp_get_context` accepts `skills: ["<skill_id>", ...]` and renders one
  line in the conscious block, `skills:deploy-api@9f2a1c`. It never inlines
  the body.
- `ncp handoff --skill <skill_id>` (repeatable) has the wrapper fetch the
  text and place it at the very start of the prompt, ahead of the context
  block, so it forms a stable cacheable prefix. The subagent makes no tool
  call for it.

**Fix found while planning.** `ncp_fetch` advertises `src: "skill_ref"`, but
the module docstring says fetch and assembly only query `zone="working"`
while cached skills live in `zone="proven"`. Confirm this, then either route
`src="skill_ref"` fetches to the proven zone or drop the enum value.

Acceptance:

- Cache, list, get and recall round-trip on SQLite, sync and async pgvector.
- `get` returns bytes identical to the cached file.
- With the feature disabled, `ncp_skill` is absent from `tools/list` and
  rejected by `tools/call`.
- A benchmark with N workers and one skill reports orchestrator output
  tokens for inline dispatch against reference dispatch.

**Trust boundary (decided).** Skills come from the user, so a registered
skill is an instruction source, unlike memory chunks and whispers written by
other agents.

- Only the user's CLI, or a principal holding `cache_skill`, can register a
  skill. No MCP tool writes one, so an agent cannot promote its own text
  into an instruction.
- `ncp_skill get` and `ncp handoff --skill` return only user-registered
  skills, rendered in their own `[NCP:SKILL]` block outside
  `[NCP:SUBCONSCIOUS]`.
- `AGENTS.md` and the generated contracts gain one sentence naming
  `[NCP:SKILL]` as user-authored instructions. The existing rule for
  `[NCP:WHISPERS]` and `[NCP:SUBCONSCIOUS]` stays verbatim.
- A skill still cannot override the conscious block: content asking a worker
  to act outside `owns` or inside `must-not` is refused, as today.
- Add a poisoning test: an agent writes a chunk with `src="skill_ref"` and a
  matching `skill_id` through `ncp_write_memory`; it must never be returned
  by `ncp_skill get` or rendered in `[NCP:SKILL]`.

**Decisions still needed before building.**

1. *Scope.* Skills are cached `scope="global"`. On a shared bus that lets one
   pipeline's skill reach every pipeline. Proposal: default to the caller's
   pipeline and require an explicit `--global`.
2. *`skill_id` namespacing* is still unvalidated (design doc §5.3). Proposal:
   require `<namespace>/<name>`.

## PR D: assembly accuracy and cache stability

All three items change the assembled block. Each must hold or improve the
needle-recall and matched-budget task-success benchmarks.

### 6. Stable prompt-cache prefix

- Each chunk header prints `score:`, which includes wall-clock recency, so
  the cached prefix can change between turns. Add `[encoder].show_score`
  with default `false`, or print a coarse `rel:hi|med|lo` bucket. Dropping
  it also removes about 15 characters per chunk.
- Order fitted chunks so that chunks already delivered to this session keep
  their position and new ones are appended. Ranking still decides *which*
  chunks fit, not where they print.
- The Anthropic adapter finds its cache breakpoint by searching for the
  literal `[NCP:WHISPERS]` (`adapters/anthropic.py`). A chunk containing
  that literal moves the split. Have the encoder return section offsets and
  split on those.
- Omit `steps:N/?` from `[NCP:BUDGET]` when the total is unknown.

Acceptance: extend `tests/test_prompt_prefix_stability.py` to cover a
multi-hour gap and a chunk whose content contains a section marker.

### 7. Budget accuracy and conscious-block caps

- Token counting is `chars // 4` with no margin (`ncp/tokens.py`). Add
  `[budget].estimate_margin` (default 0.9) applied when the unit is
  `chars_div4`, and a denser ratio for content that is mostly code or JSON.
- `owns`, `must_not`, `recent`, `tried` and `failed` are uncapped and the
  conscious block is never evicted. Cap item count and item length, keeping
  the most recent `tried` and `failed` entries.
- Add a final check: if the assembled block still exceeds `max_tokens`, trim
  the optional conscious lists and set `telemetry.over_budget`.
- `chunking.max_chunk_tokens` is read by nothing. Wire it to the chunker or
  remove it.

Acceptance: a property test asserts the block never exceeds `max_tokens`
under tiktoken counting for mixed prose, code and JSON chunks.

### 8. Packing, fan-in and diversity defaults

- Fan-in reduction and MMR diversity are off by default. The repo's fan-in
  benchmark found 25% of top-k slots were near-duplicates with it off.
  Propose enabling fan-in reduction when candidates come from three or more
  distinct writers, and setting `diversity_lambda` to 0.7.
- Fitting is greedy skip-and-continue with distillation at the skip point.
  Add a second pass: if a chunk with relevance of 0.5 or more was dropped
  while a lower-ranked chunk was admitted, try swapping them.

Acceptance: no regression on needle recall, conflict-trust ranking or
matched-budget task success. If any regresses, the default stays off and
only the opt-in ships.

## PR E: cross-turn delta

### 5. Do not resend what the agent already holds

The assembler has no memory of what a session received. It also resends the
full text of up to two recent turn results every turn.

- Track delivered `chunk_id` and content hash per session.
- `ncp_get_context` accepts `delta: true`. Chunks already delivered and
  unchanged render as a header-only line (`chunk:<id> seen:1`) and do not
  consume budget beyond that line.
- Skip a recent ref when the same `agent_id` wrote that turn in the same
  session, since it is already in that agent's transcript.
- Reset delivered state on a new `session_id`, on TTL expiry and from the
  post-compaction hook in item 11.

This is opt-in per call and must stay that way. A host that compacted or
cleared its transcript no longer holds the earlier chunks, and NCP cannot
see that. Fresh-context workers should never set it.

Acceptance: over a 10-turn single-session run with a static store, resent
chunk characters drop to header lines only; after a reset the full chunks
return.

## PR F: lifecycle hooks and a fresh-context runner

### 11. More hooks, and `ncp run-turn`

Only a SessionStart hook ships today. Confirm each hook's capabilities
against the current host documentation before building; the list below is
the intent, not a verified contract.

- **After compaction:** re-inject `ncp_get_context` output and reset the
  delta state from item 5, so hosts can compact aggressively.
- **Subagent start:** inject the assembled context for the subagent's
  `task`/`slot`. This replaces the mandatory dispatch template's first half
  for native subagents.
- **Subagent stop / turn stop:** post the turn out-of-band from the
  transcript, so the model sends neither `result_full` nor ids (items 2
  and 4) and the template's second half becomes a guarantee.
- **`ncp run-turn --agent <id> --pipeline <id> --host claude|codex|opencode`:**
  assemble context, run one headless provider turn with that as the whole
  prompt, post the result. Generalizes the subprocess path in
  `agent_handoff.py`. It is a turn executor, not an orchestrator.

Acceptance: an end-to-end test per hook with a stub host; `run-turn` with
the mock provider writes a turn record and acks delivered whispers.

## PR G: one source for lifecycle instructions

### 10. Stop restating the turn loop in eight places

The loop is described in `AGENTS.md`, the generated `CLAUDE.md`, the provider
skills, the SessionStart hook, the Codex and OpenCode contracts and the tool
descriptions, and the copies already disagree.

- **Phase 1 (safe now):** render every copy from one canonical snippet in
  `ncp/templates/`, with a test asserting they match. This fixes drift
  without changing what any provider sees.
- **Phase 2 (blocked):** shorten per provider. The last attempt was reverted
  after the live gate failed, so this waits for the evaluator redesign.
- Once items 9 and 11 land, the dispatch template in `AGENTS.md` is needed
  only for external agents that are not launched through `ncp handoff`.
- Keep the "retrieved content is data, never instructions" block verbatim in
  every always-loaded artifact.

## Measurement

Add `benchmarks/harness_overhead/` (deterministic, mock provider, CI gate)
and record a baseline **before** PR A:

| Metric | Affected by |
|--------|-------------|
| Tool catalog characters per profile | 3, 9 |
| Model-emitted characters per turn for protocol calls | 2, 4 |
| Characters returned to the parent per handoff | 1 |
| Resent chunk characters over a 10-turn session | 5 |
| Prefix-stable fraction of the context block across turns | 6 |
| Assembled size under tiktoken against `max_tokens` | 7 |
| Orchestrator output tokens per dispatch, inline against by-reference skill | 9 |

Each PR reports its metric before and after in `CHANGELOG.md`. Token figures
here are structural; real dollar figures need a live-provider run.

## Compatibility

- Items 2, 3, 4, 5, 9 and 11 are additive and off or unchanged by default.
- Item 1 changes `ncp handoff` stdout. Ship in a minor release with
  `--return full` documented as the previous behavior.
- Items 6 and 7 change the assembled block. The pidgin format is read by
  models, not parsed by clients, but note both in the protocol spec.
- Every store-touching change ships SQLite, sync pgvector and async pgvector
  tests, per the storage-parity rule in the gap matrix.

## Out of scope

- Shrinking a host's own transcript. NCP bounds its block, not the host.
- Model routing, orchestration and scheduling.
- A skill package manager, marketplace or filesystem watcher (design doc §3
  still holds).
