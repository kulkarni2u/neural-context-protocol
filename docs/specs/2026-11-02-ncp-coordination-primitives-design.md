# NCP Coordination Primitives — Design

**Status:** Implemented in 1.8.0 (capabilities A, B, C and D; see CHANGELOG
1.8.0). Deferred: webhook delivery (B.2) and a stdio notification
transport, which this document already scoped out. Open questions in §8
other than the resolved one are not settled by the release.
**Date:** 2026-11-02
**Scope:** Four additive capabilities that let a long-lived *router* agent
coordinate many concurrent *worker* agents over the NCP bus:

| # | Capability | One-line summary |
|---|---|---|
| A | Request/reply whispers | `in_reply_to` / `thread_id` on whispers, plus `progress` and `result` whisper types |
| B | Push delivery | A wake-up signal when an agent has a new whisper: `ncp_wait_whispers` (long-poll), `GET /events` (SSE), Redis pub/sub between processes |
| C | Task ledger | Durable task records with leased claims, heartbeats, fencing tokens and retry limits |
| D | Cross-pipeline overview | `ncp_overview`: one bounded, ranked line per active pipeline |

Implementation order: **A → B → C → D**. Each step is a separate PR that
ships on its own (see [§7](#7-delivery-plan)).

---

## 1. Motivation

The common "super-agent" pattern has a single always-on router. It receives
events (chat, GitHub, email, calendar), dispatches workers in parallel,
collects their progress, and keeps the overall picture. NCP already provides
the shared memory underneath that pattern: bounded context, durable memory,
whispers and trust. A router built on NCP today still runs into four gaps:

1. **No record of in-flight work.** Turns, chunks and decisions record what
   happened. Nothing records what is in progress, who owns it, or whether
   the owner is still alive. A router that restarts can rebuild what it knew,
   but not what it was waiting on.
2. **Pull-only whispers.** A worker's result reaches the router only on the
   router's next `ncp_get_context`. The `/sse` stream only advertises the
   endpoint and sends keepalives (`ncp/mcp/server.py`, `do_GET`). The router
   has to poll.
3. **No request/reply link.** `ref` points at a chunk. Nothing links a
   whisper to the whisper it answers, so the router has to match results to
   dispatches by parsing free text.
4. **One pipeline at a time.** Every read is scoped by `pipeline_id`. If
   `pipeline_id` is omitted, the candidate pool widens to everything (see
   the integration guide §3). A router watching many pipelines must either
   call once per pipeline or accept a noisy view across all of them.

## 2. Non-goals (keep NCP a bus, not an orchestrator)

- **No scheduling.** NCP never decides which worker runs, never starts
  processes, and never retries work on its own. The task ledger records
  claims. The orchestrator decides who claims.
- **No connectors.** Turning Slack, GitHub or email events into turns is the
  host's or orchestrator's job (n8n, webhooks, MCP connectors).
- **No background threads for lease expiry.** Expiry is applied lazily on
  read and claim, the same way whisper TTL is enforced today.
- **No change to the core five-tool profile.** All new tools are off in
  `tool_profile = "core"`.
- **No change to the default `[NCP:*]` wire format.** New output appears
  only when the new tools are called.

---

## 3. Capability A — Request/reply whispers

### 3.1 Data model

New optional fields on `Whisper` (`ncp/types.py`):

| Field | Type | Meaning |
|---|---|---|
| `in_reply_to` | `str?` | `whisper_id` this whisper answers |
| `thread_id` | `str?` | Conversation key shared by every whisper in the exchange |

**Resolving `thread_id` on emit:**

1. If the caller sets `thread_id`, keep it.
2. Otherwise, if `in_reply_to` is set and that whisper is still stored, copy
   its `thread_id`. If it is not stored, use `thread_id = in_reply_to`.
3. Otherwise, for `request` and `share` whispers, set
   `thread_id = whisper_id`, so every request starts a thread.

Step 2 needs a fallback because acknowledged whispers are **deleted**
(`acknowledge_whispers`). By the time a worker replies, the request has
usually been consumed. That is also why `in_reply_to` is not checked for
existence: a reply to an already-acknowledged request must still be
accepted.

Capability C adds a third field, `task_id` (see §5.6).

### 3.2 New whisper types

`WhisperType` gains `progress` and `result`. Both carry typed payloads
through the existing `_whisper_payload_model` path:

```python
class ProgressPayload(NCPModel):
    status: Literal["working", "blocked", "waiting"]
    pct: int | None = None          # 0..100
    note: str | None = None         # <= 200 chars

class ResultPayload(NCPModel):
    status: Literal["succeeded", "failed", "partial", "declined"]
    summary: str                    # <= 300 chars; the distilled finding
    result_chunk_ids: list[str] = []  # <= 10; durable detail lives in memory
    turn_id: str | None = None      # lets the router record outcomes by turn
```

The 600-character normalized payload limit still applies. `ResultPayload` is
meant to *point* at durable memory, not hold it. This matches the
`AGENTS.md` rule to write back the distilled finding.

### 3.3 Routing rules (additions to spec §2.3)

| Type | Confidence filter | Broadcast `*` | Linkage required | Ordering |
|---|---|---|---|---|
| `progress` | yes (`min_confidence`) | prohibited | `in_reply_to` or `thread_id` (or `task_id` after C) | after `alert` |
| `result` | **no** (like `world_check`) | prohibited | `in_reply_to` or `thread_id` (or `task_id` after C) | right after `alert` |

- **Progress coalescing.** When a `progress` whisper is emitted, any
  undelivered `progress` whisper with the same
  `(pipeline_id, from_agent, target, thread_id)` is deleted. A worker that
  reports 10 times leaves one entry in the router's queue, so the
  `whisper_cap_*` budget is not spent on stale percentages.
- **Results skip the confidence filter.** A low-confidence `failed` result
  is still exactly what the router needs to see.

### 3.4 Rendering

A receiver can only reply if it can see the id it is replying to, and today
the whisper id never appears in the context text. So a threaded whisper's
`[NCP:WHISPERS]` header gains:

- `id:<whisper_id>`, always, for a threaded whisper.
- `re:<in_reply_to>`, when set.
- `th:<thread_id>`, only when it differs from both `id` and `re`.

```
wsp from:router to:reviewer t:request c:0.9 age:<1m id:wsp_3f2a
wsp from:reviewer to:router t:result c:0.9 age:<1m id:wsp_7b10 re:wsp_3f2a
```

Unthreaded whispers, including every whisper stored before this change,
render byte-for-byte as before. Newly emitted `share`/`request` whispers are
threaded, so they show `id:`. A test guards both cases.

### 3.5 Storage

| Backend | Change |
|---|---|
| SQLite | `ALTER TABLE whispers ADD COLUMN in_reply_to TEXT`, `ADD COLUMN thread_id TEXT`, and `CREATE INDEX idx_whispers_thread ON whispers(pipeline_id, thread_id)`. Added through the existing idempotent `ALTER` list in `sqlite.py`. |
| pgvector (sync and async) | Whispers live in Redis (`RedisCoordination` / `AsyncRedisCoordination`), so there is no SQL migration. The payload hash gains `in_reply_to` and `thread_id`. Coalescing scans the target's index and deletes matching older progress entries. This is best-effort under concurrency: two simultaneous emits can both survive, costing one extra queue entry. A Lua script can make it atomic later if needed. |

### 3.6 Tool surface

`ncp_emit_whisper` gains optional `in_reply_to` and `thread_id`, and the new
type values. `ncp_post_turn`'s ack list is unchanged.

### 3.7 Signing caveat

Signatures cover `written_by | sha256(payload) | pipeline_id`
(`canonical_authorship_payload`). `in_reply_to` and `thread_id` are
**unsigned routing metadata**. An attacker could therefore attach a forged
`result` to someone else's thread. Mitigations:

- When `task_id` is set (Capability C), the server checks that
  `from_agent` is the task's current owner, or that the request comes from
  the owner's `complete` call.
- A follow-up could extend the canonical payload to
  `written_by | sha256(payload) | pipeline_id | in_reply_to`. That changes
  the signing contract, so it is deferred (see §8).

---

## 4. Capability B — Push delivery

### 4.1 Principle: a notification is a doorbell, not a delivery

A push event says *"something is queued for you"*. It never carries the
payload. The queue and `ncp_get_context` remain the only way to deliver and
acknowledge whispers. This means:

- Reputation gating, confidence filters, whisper budget caps and
  acknowledgement keep working in exactly one place.
- Payloads are never streamed over long-lived connections, so the
  prompt-injection posture (spec §1.4 and §5.1) is unchanged.
- Losing a notification is harmless. A client that reconnects calls
  `peek` (via `ncp_wait_whispers` with `timeout_seconds: 0`) and
  continues.

Event shape (no payload, no chunk content):

```json
{"event": "whisper", "whisper_id": "wsp_…", "type": "result",
 "from": "reviewer", "target": "router", "pipeline_id": "pr_142",
 "thread_id": "wsp_3f2a", "task_id": "tsk_…", "created_at": 1793500000.0}
```

Capability C adds `event: "task"` notifications for `failed` and
`lease_expired` transitions, sent to the task's `created_by`.

### 4.2 Notifier abstraction

New module `ncp/notify.py`:

```python
class Notifier(Protocol):
    def publish(self, event: NotifyEvent) -> None: ...
    def wait(self, *, agent_id: str, pipeline_id: str | None,
             timeout: float) -> list[NotifyEvent]: ...
    def subscribe(self, *, agent_id: str, pipeline_id: str | None
                  ) -> ContextManager[Iterator[NotifyEvent]]: ...
```

| Implementation | When | Mechanism |
|---|---|---|
| `InProcessNotifier` | SQLite | A `threading.Condition` and a small ring buffer per `(pipeline_id, agent_id)`. Broadcast `*` wakes every subscriber in that pipeline. The doorbell stays inside one process; a store-poll fallback (section 4.3.1) covers writers in other processes. |
| `RedisNotifier` | pgvector + Redis, or several `ncp serve` processes | Redis pub/sub on `{stream}:notify:{pipeline_id}:{target}` (`*` for broadcast). Reuses `RedisCoordination`'s client factory. |

Selection: `[notify] backend = "auto"` picks Redis when a Redis URL is
configured, otherwise in-process.

**Publish point.** One helper, `_emit_and_notify(store, notifier, whisper)`,
is called from both `_handle_emit_whisper` and `ncp.api.emit`. It publishes
only after `store.emit_whisper` returns, so a doorbell never rings for a
whisper that failed to persist.

**Pre-filtering at publish.** Target, pipeline and type/confidence rules are
applied, since the whisper object is already in hand. The reputation gate is
**not** applied, because that would need a store read on every emit. The
doorbell can therefore occasionally ring for a whisper that
`ncp_get_context` later filters out. This is documented and acceptable.

### 4.3 Transports

**1. `ncp_wait_whispers` MCP tool (long-poll).** This is the primary path,
because MCP hosts such as Claude Code, Codex and OpenCode cannot consume a
side-channel SSE stream. It is also the only path that works from inside a
model turn.

```
args:    agent_id, pipeline_id?, timeout_seconds (0..max_wait_seconds, default 30),
         types? (filter), thread_id? (filter)
returns: {"ready": bool, "whispers": [{whisper_id, type, from, thread_id, task_id}],
          "timed_out": bool}
```

- It peeks; it never drains. The caller follows with `ncp_get_context`.
- `timeout_seconds: 0` is a non-blocking check, which doubles as
  reconnect catch-up.
- It returns immediately if eligible whispers already exist, because it
  peeks before it waits.

**2. `GET /events?agent_id=…&pipeline_id=…` (SSE).** For orchestrators that
are long-lived processes (custom routers, LangGraph servers):

- Same bearer auth and CORS rules as `/mcp`.
- Sends `event: whisper` and `event: task` frames, using the existing
  `keepalive_seconds` comment frames.
- Sends an initial `event: ready` frame with the count of whispers already
  queued, so subscribers do not miss anything emitted before they connected.
- `ThreadingHTTPServer` uses one thread per connection, so subscribers are
  capped by `[notify] max_subscribers` (default 64). Above the cap the
  server returns `503` with `Retry-After`.

**3. Webhooks (deferred to B.2).** For callers that cannot hold a
connection, such as n8n or serverless functions: a POST to a configured URL
per `agent_id`, signed with HMAC-SHA256 using a per-hook secret. Deferred
because it adds outbound network calls, a retry policy and secret
management. The doorbell model makes it a pure add-on later.

**stdio** is out of scope. One client per process, and hosts do not surface
custom notifications to the model. `ncp_wait_whispers` covers this case.

### 4.3.1 Implementation notes (PR 2)

Where the shipped code differs from the sketch above:

- The helper is public: `ncp.notify.emit_and_notify(store, notifier, whisper)`
  (not underscore-prefixed), because `ncp.api` and the MCP server both import it.
- `backend = "auto"` picks Redis when the store is `pgvector` and a Redis URL
  is set, not merely when a URL is set. The default `[redis] url` is always
  non-empty, so URL presence cannot distinguish a SQLite deployment.
- `subscribe()` yields a `Subscription` with `poll(timeout)` and iteration,
  rather than a bare iterator, so callers can bound each blocking read and
  exit promptly on shutdown. `ncp_wait_whispers` subscribes before its first
  peek so a whisper emitted between the peek and the wait is not missed.
- The confidence pre-filter uses the fixed 0.60 floor the assembler uses
  (`Assembler._peek_whispers`), not `[whispers] min_confidence`, which the
  assembler does not read.
- `task_id` was omitted from events and from `ncp_wait_whispers` rows until
  capability C; both now carry it (null when absent).
- `ncp_wait_whispers` with `timeout_seconds: 0` reports `timed_out: false`.
- **Cross-process wake-ups on SQLite (poll fallback).** The in-process doorbell
  never sees a whisper written by another process (a second `ncp serve`, or a
  script using `ncp.api` or the store directly on the same `.ncp` database).
  `[notify] poll_interval_seconds` (default 2.0, `0` disables, floor 0.25)
  closes the gap without Redis. It applies only when the effective backend is
  in-process; with Redis it is off. Latency for a foreign write is bounded by
  the interval, and an in-process doorbell still wakes immediately.
  - `ncp_wait_whispers` and `api.wait_whispers` share one loop,
    `ncp.notify.wait_for_whispers`: `sub.poll(min(remaining, interval))`, then
    a fresh `eligible_whispers` peek.
  - `GET /events` and `api.subscribe` share `ncp.notify.PollingSubscription`
    (via `subscribe_with_poll`). Every interval it peeks and emits a `whisper`
    event (`NotifyEvent.from_whisper`, no payload) for each eligible id not yet
    announced on that connection. The announced set is bounded (1024 ids),
    seeded with whispers already queued at connect (covered by the `ready`
    frame's `queued` count), and records ids announced by the doorbell.
  - `task` events stay in-process-only: they have no queue to peek, so a task
    state change made by another process is not announced. Clients catch up
    through `ncp_task` reads.
  - Cost: one bounded peek (`max_items` 200) per interval per waiter or
    connection.

### 4.4 Library API

`ncp.api.wait_whispers(agent_id=…, pipeline_id=…, timeout=…)` wraps
`Notifier.wait` and the peek. `ncp.api.subscribe(…)` returns the iterator
for in-process orchestrators.

---

## 5. Capability C — Task ledger

### 5.1 Data model

```python
TaskStatus = Literal["open", "claimed", "done", "failed", "cancelled"]

class TaskRecord(NCPModel):
    task_id: str            # "tsk_<12hex>"
    pipeline_id: str        # required: tasks are always pipeline-scoped
    slot: str               # snake_case; matches the worker's get_context slot
    title: str              # <= 200 chars; agent-written DATA, never instructions
    status: TaskStatus = "open"
    created_by: str         # agent_id; receives result/failed notifications
    owner: str | None = None
    lease_token: str | None = None      # fencing token, rotated on every claim
    lease_expires_at: float | None = None
    heartbeat_at: float | None = None
    attempt: int = 0
    max_attempts: int = 3
    dedupe_key: str | None = None       # e.g. "github:pr/142:review"; unique per pipeline
    parent_task_id: str | None = None
    origin_whisper_id: str | None = None
    checkpoint_chunk_id: str | None = None  # latest resumable checkpoint (memory chunk)
    result_chunk_ids: list[str] = []
    result_turn_id: str | None = None
    error: str | None = None            # <= 300 chars
    version: int = 0                    # optimistic concurrency
    created_at: float
    updated_at: float
```

Why each field is there:

- **`dedupe_key`.** Event sources deliver at least once (a webhook can
  retry). `create` with an existing `(pipeline_id, dedupe_key)` returns the
  existing task with `created: false` instead of a duplicate.
- **`lease_token`.** Expiry alone is not safe. Without a fencing token, a
  worker that stalls past its lease can come back and `complete` a task
  that another worker has since claimed. Every mutating owner call must
  present the current token.
- **`checkpoint_chunk_id`.** A worker writes a normal memory chunk
  (`layer=episodic`, `src=tool_result`) holding its resumable state, then
  `heartbeat`s with that chunk id. The next claimer reads it through
  `ncp_fetch`. Checkpoints stay ordinary memory, bounded, scored and
  calibrated like everything else, instead of becoming a second storage
  system.

### 5.2 State machine

```
            create
              │
              ▼
   ┌──────► open ◄──────────────── release / lease expiry (attempt < max)
   │          │
   │          │ claim (rotates lease_token, attempt += 1)
   │          ▼
   │       claimed ── heartbeat (same token: extends lease, optional checkpoint)
   │          │
   │          ├── complete(status=done)   → done
   │          ├── complete(status=failed) → failed
   │          └── lease expiry when attempt >= max_attempts → failed (error="lease_expired")
   │
   └── cancel (from open or claimed; by created_by) → cancelled
```

`done`, `failed` and `cancelled` are terminal. Re-running a finished task
means creating a new task with `parent_task_id` set.

### 5.3 Lazy expiry

There is no reaper thread. Every `claim`, `list`, `get` and `ncp_overview`
read first runs:

```sql
UPDATE tasks
   SET status = CASE WHEN attempt >= max_attempts THEN 'failed' ELSE 'open' END,
       owner = NULL, lease_token = NULL, lease_expires_at = NULL,
       error = CASE WHEN attempt >= max_attempts THEN 'lease_expired' ELSE error END,
       updated_at = :now, version = version + 1
 WHERE status = 'claimed' AND lease_expires_at < :now
   [AND pipeline_id = :pipeline_id]
RETURNING task_id, status, created_by, pipeline_id;
```

Rows that move to `failed` publish an `event: task` notification
(Capability B) to `created_by`. This matches the existing pattern of
deleting expired whispers at read time.

### 5.4 Atomic claim

`claim` accepts either a specific `task_id` or a filter
(`pipeline_id`, optional `slot`). The filter form claims the oldest open
task that matches.

**SQLite** (inside `BEGIN IMMEDIATE`):

```sql
UPDATE tasks SET status='claimed', owner=:agent, lease_token=:tok,
       lease_expires_at=:now+:lease, heartbeat_at=:now, attempt=attempt+1,
       updated_at=:now, version=version+1
 WHERE task_id = (SELECT task_id FROM tasks
                   WHERE pipeline_id=:p AND status='open' [AND slot=:slot]
                   ORDER BY created_at LIMIT 1)
RETURNING *;
```

`RETURNING` needs SQLite 3.35 or later. On older linked libraries, the
SQLite store falls back to `UPDATE` followed by `SELECT … WHERE lease_token = :tok`
inside the same `IMMEDIATE` transaction, which is equally atomic.

**Postgres** (sync and async): the same statement, with the subquery using
`FOR UPDATE SKIP LOCKED`, so concurrent claimers never block each other or
claim the same task twice.

Owner mutations (`heartbeat`, `release`, `complete`) are all conditional
updates:
`WHERE task_id=:id AND status='claimed' AND lease_token=:tok AND lease_expires_at>=:now`.
If zero rows change, the call returns `{"ok": false, "reason": "lease_lost"}`
and the worker must stop.

### 5.5 Tool surface: one tool, `ncp_task`

A single tool with an `action` enum keeps the MCP tool list short. Every
tool definition costs context tokens in every host.

| action | caller | key args | effect |
|---|---|---|---|
| `create` | router | `pipeline_id, slot, title, dedupe_key?, max_attempts?, parent_task_id?, assign_to?` | Creates an open task. `assign_to` also emits a `request` whisper (thread starts here) with `task_id` set. |
| `claim` | worker | `agent_id, pipeline_id, task_id? \| slot?, lease_seconds?` | Returns `task` and `lease_token`, or `{"claimed": false}`. |
| `heartbeat` | owner | `task_id, lease_token, checkpoint_chunk_id?, progress?` | Extends the lease. If `progress` (a `ProgressPayload`) is given, also emits a coalesced `progress` whisper to `created_by`. |
| `release` | owner | `task_id, lease_token, error?` | `claimed` → `open`. The attempt still counts. |
| `complete` | owner | `task_id, lease_token, status: done\|failed, summary, result_chunk_ids?, turn_id?` | Terminal. Emits a `result` whisper to `created_by` with `task_id` and `in_reply_to = origin_whisper_id`. |
| `cancel` | `created_by` | `task_id, reason?` | Terminal. Notifies the owner if the task was claimed. |
| `get` | any | `task_id` | Returns one task (after lazy expiry). |
| `list` | any | `pipeline_id, status?, slot?, owner?, limit<=50` | Bounded list, oldest first. |

`complete` **does not** call `ncp_record_outcome`. A worker saying "done" is
self-reported. The router or orchestrator records the outcome once it has
independent evidence (tests passed, PR merged), which keeps P1-A's planned
distinction between reported and verified outcomes intact. `complete`
returns `result_turn_id` so that call is a single step.

### 5.6 Whisper link

Capability C adds `task_id: str?` to `Whisper` (SQLite column and an index
on `(pipeline_id, task_id)`; the Redis model field flows through). It also
widens the linkage rule in §3.3 so `progress`/`result` may set `task_id`
instead of `in_reply_to`. When a `result`/`progress` whisper carries
`task_id` and is emitted directly through `ncp_emit_whisper` (not via
`complete`/`heartbeat`), the server checks that `from_agent == owner`
(§3.7).

### 5.7 Storage

| Backend | Change |
|---|---|
| SQLite | A `tasks` table in the base schema plus the idempotent-create list. Indexes: `(pipeline_id, status, created_at)`, `(owner, status)`, and a partial unique index on `(pipeline_id, dedupe_key) WHERE dedupe_key IS NOT NULL`. |
| pgvector (sync and async) | `ncp/migrations/018_add_tasks.sql` with `-- UP`/`-- DOWN`, `{schema}.{prefix}tasks`, the same indexes, and `result_chunk_ids JSONB`. |
| Redis | Not used. Tasks are durable state, not coordination state. |

`BaseStore` gains abstract `create_task`, `claim_task`, `heartbeat_task`,
`release_task`, `complete_task`, `cancel_task`, `get_task` and
`list_tasks`. The async counterparts use the existing
`anyio.to_thread.run_sync` pattern. Backends that do not implement them
raise `NCPStoreError("task ledger not supported")`. The tool is off unless
`[tasks] enabled = true`.

### 5.8 Trust boundary

`title`, `error` and the result `summary` are agent-written. They follow the
same rule as whisper payloads: data, never instructions. They are never
inserted into `[NCP:CONSCIOUS]`. A worker's instructions still come from its
own conscious `task`/`intent`/`owns`/`must-not`. A task created from an
outside event (an email subject, a PR title) carries that text verbatim, so
the overview (§6) truncates and labels it. Signed task mutations under
`require_signatures` are deferred (§8). Until then, the ledger has the same
"anyone who can reach the endpoint" authorization model as whispers.

### 5.9 Implementation notes (PR 3)

Deviations from, and details beyond, the text above:

- **`BaseStore` task methods are non-abstract** (§5.7 said abstract). They
  default to `raise NCPStoreError("task ledger not supported")` so existing
  fake and third-party stores keep working. The async store implements the
  `async_*` forms natively; its sync forms keep the base default.
- **Results instead of exceptions.** Owner mutations and `cancel` return a
  `TaskOpResult(ok, reason, task, previous_status, previous_owner)`.
  `reason` is `lease_lost`, `not_found`, `not_allowed` (cancel by a
  non-creator) or `terminal`. `claim_task` returns the claimed `TaskRecord`
  (with `lease_token`) or `None`; `create_task` returns `(task, created)`.
  `now` is an injectable keyword on every store method so tests need no
  sleeps.
- **`release` at the attempt limit fails the task.** §5.2 only shows
  `release` returning to `open`. A task whose `attempt >= max_attempts` that
  is released would otherwise sit `open` forever (the claim also refuses
  `attempt >= max_attempts`), so it becomes `failed` with
  `error = "max_attempts_exhausted"` (or the supplied `error`) and rings the
  creator like a lease-expiry failure.
- **`complete` summary storage.** `TaskRecord` has no summary field. For
  `status = failed` the summary (truncated to 300 chars) is stored in `error`;
  for `done` it travels only in the `result` whisper.
- **Expiry notifications are published by the tool layer.** The store cannot
  publish, so `ncp_task` calls `expire_task_leases` (and rings `created_by`
  for each row now `failed`) before `claim`, `get`, `list` and owner actions.
  The store also runs expiry inside those operations, so state is correct for
  direct library callers. The `ncp.api` task functions share the same service
  (`ncp/tasks.py`) and therefore ring the doorbell on expiry too; only code
  that calls the store directly and lets it expire a task on its own gets no
  doorbell. A rare race where another thread's
  internal expiry wins the transition before the tool layer's explicit call
  can also skip the doorbell; the state is still correct and a `get`/`list`
  shows it.
- **Expiry scope.** `get` and the owner actions expire only the task they
  touch; `claim` and `list` expire the pipeline (or the one task). Expiry
  uses select-then-update by id inside the write transaction on every SQLite
  version; only `claim` has the `RETURNING` versus fallback split.
- **Ledger enabled check on emit.** The `task_id` owner check (§5.6) also
  requires the task's `pipeline_id` to equal the whisper's, and rejects
  `progress`/`result` carrying `task_id` when `[tasks].enabled = false` or the
  store raises "task ledger not supported". Other whisper types may carry
  `task_id` without the check. `ncp.api.emit` applies the same owner
  check as the tool (shared `check_task_owner` in `ncp/tasks.py`), and
  `ncp.api` exposes `create_task`, `claim_task`, `heartbeat_task`,
  `release_task`, `complete_task`, `cancel_task`, `get_task` and `list_tasks`
  over the same service. They need a synchronous store; the async store keeps
  only its `async_*` task methods.
- **Progress coalescing** now keys on `(pipeline_id, from_agent, target,
  thread_id, task_id)`, so task-only progress for different tasks does not
  replace each other. SQLite matches with `IS`, so a null `thread_id` or
  `task_id` coalesces too.
- **`NotifyEvent` for tasks** reuses the whisper event shape: `event="task"`,
  `type="task"`, `whisper_id=null`, `from="ncp_ledger"` (or the cancelling
  agent), `target`, `pipeline_id`, `task_id`, `status`, `created_at`. The
  whisper event gains `task_id` and `status` (null) so both kinds share one
  schema.
- **Rendering.** A whisper with `task_id` renders `task:<task_id>` after the
  other linkage tokens, and `id:` is shown whenever any of `thread_id`,
  `in_reply_to` or `task_id` is set.
- **Postgres schema.** `ncp/migrations/018_add_tasks.sql` is mirrored in
  `PGVECTOR_SCHEMA_TEMPLATE` (as for decisions). Sync and async stores share
  one set of statement builders in `ncp/stores/task_sql.py`. The dedupe insert
  is `ON CONFLICT (pipeline_id, dedupe_key) WHERE dedupe_key IS NOT NULL DO
  NOTHING` followed by a select of the existing row. `cancel` reads the row
  `FOR UPDATE` to check the creator before updating.
- **`stale_open_seconds`** is parsed and exposed (`tasks_stale_open_seconds`)
  but unused until capability D.
- **`ncp_overview`** (§5.3 mentions it) does not exist yet; lazy expiry there
  is for PR 4.
- **Verified on real pgvector.** Migration 018 and the template were first
  exercised on a plain Postgres 16 without the `vector` extension. They have
  since been run on Postgres 16 with pgvector 0.6.0: a store using its normal
  `_init_db` (the whole template, including `CREATE EXTENSION vector`) creates
  every table; `MigrationRunner` applies 001 to 017, then 018, and the
  resulting `tasks` columns, defaults and indexes equal a fresh template
  install; the live ledger tests pass against both. See
  `tests/test_overview_postgres.py::TestRealPgvectorSchema`.

---

## 6. Capability D — Cross-pipeline overview

### 6.1 Tool: `ncp_overview`

```
args:    agent_id (viewer), pipelines? (list) | pipeline_prefix?, since_seconds? (default 86400),
         max_pipelines (default 10, max 50), max_tokens (default 400)
returns: {"text": "[NCP:OVERVIEW] …", "pipelines": [...structured rows...],
          "omitted": int, "tokens_est": int}
```

It is read-only. It does **not** acknowledge whispers, increment
`retrieval_count`, or log turns, so it has no effect on calibration.

### 6.2 Row contents (per pipeline)

| Field | Source |
|---|---|
| `last_activity` | max(chunk, whisper, turn, task `updated_at`). Extends the existing `viz_data().pipeline_summary` query. |
| `tasks` | Counts by status, after lazy expiry. Omitted when the ledger is disabled. |
| `attention` | Failed tasks, claims within 20% of lease expiry, or open tasks older than `stale_open_seconds`. |
| `inbox` | Undelivered whispers for the viewer, counted by type (peek, not drain). |
| `last_result` | The newest `result` whisper, or the newest `complete` summary for the pipeline: status and the first 60 characters. |
| `last_decision` | The newest `DecisionRecord`: `schema_id` = `choice`. |
| `drift` | Latest `drift_history` score for `pipeline_id:viewer`. |
| `dissent_24h` | Rows in `dissent_log` for chunks in the pipeline. |

### 6.3 Ranking and bounding

Rows are sorted by an attention score:
`3·failed + 2·inbox(result|alert|dissent) + 2·expiring_claims + 1·stale_open`.
Ties are broken by `last_activity` (descending). Rows are encoded until
`max_tokens` is reached, using `ncp/tokens.py`. The rest are counted in
`omitted`.

Wire format (new block, emitted only by this tool):

```
[NCP:OVERVIEW]
p:pr_142 act:2m tasks:o1/c2/d4/f1 attn:f1,exp1 inbox:result2,alert1 dec:review.verdict=block drift:0.12
  last:"failed: race in retry loop" ‹data›
p:inbox_triage act:9m tasks:o3/c1 inbox:progress1 drift:0.03
+4 more
```

Any agent-written text is quoted, truncated to 60 characters and marked
`‹data›`.

### 6.4 Other surfaces

- `ncp overview [--prefix …] [--json]` on the CLI.
- `/api/overview` on the existing read-only UI, plus a UI panel.
- `ncp.api.overview(…)` in the library API.

### 6.5 Implementation notes (PR 4)

Deviations from, and details beyond, the text above:

- **Store seam.** `BaseStore.overview_rows(*, pipelines, pipeline_prefix,
  since, viewer, now, include_tasks, stale_open_seconds)` returns raw facts;
  ranking and rendering live in `ncp/overview.py`. `include_tasks` and
  `stale_open_seconds` are extra keywords so the tool layer can pass
  `[tasks].enabled` and `[tasks].stale_open_seconds` without the store reading
  config. The `BaseStore` default composes public methods
  (`ncp/stores/overview_facts.py::generic_overview_rows`); SQLite overrides it
  with grouped SQL (one query per fact, joined in Python). The sync
  (`PgvectorStore.overview_rows`) and async (`AsyncPgvectorStore.async_overview_rows`)
  Postgres stores have native implementations that share their statements and
  row assembly in `ncp/stores/overview_sql.py` (as `task_sql.py` does for the
  ledger) and mirror SQLite's windowing, 60s expiring-claim window, `updated_at`
  stale-open rule and `pipeline_id` drift key. Pipelines are discovered from
  the union of chunks, turns, decisions and tasks (SQLite now includes
  decisions too, so a decision-only pipeline appears on both). Whispers live in
  Redis, so the inbox counts, the newest unexpired `result` and whisper
  activity come from `coordination` by peeking only (`peek_whispers`, plus the
  new read-only `scan_whispers` on `RedisCoordination` and
  `AsyncRedisCoordination`; the async class also gained a public
  `peek_whispers`). Nothing is acknowledged or deleted. A pipeline that has
  only queued whispers appears (as on SQLite), and without Redis (or if it is
  unreachable) the inbox is empty and `last_result` falls back to failed
  tasks. `dissent_24h` is real (joined to the dissent log). The generic
  `BaseStore` default remains for third-party stores and keeps its documented
  limits (chunk-bearing pipelines only, 50 tasks per status, `dissent_24h = 0`).
  Verified live against Postgres 16 with pgvector 0.6.0: SQLite and Postgres
  return identical rows for the same fixture, and the async store matches the
  sync one (`tests/test_overview_postgres.py`, gated on `NCP_TASKS_PG_DSN`
  and, for whisper facts, `NCP_TASKS_REDIS_URL`).
- **Drift key.** The spec said `drift_history` is keyed by
  `pipeline_id:viewer`. The only writer (`CoherenceChecker._log_drift_history`)
  logs `session_id = pipeline_id`, so `drift` reads the newest row for
  `pipeline_id` and also accepts `pipeline_id:viewer` if a future writer
  produces it. Drift is therefore pipeline-wide, not per viewer.
- **Expiring claims** use a fixed window: `lease_expires_at - now <= 60`
  seconds, not 20% of each lease. With the default 300s lease the two agree,
  and a fixed window keeps the SQL grouped.
- **Stale open** compares `now - updated_at` (not `created_at`), so a task
  released back to `open` is stale only after it has sat unclaimed. A
  `stale_open_seconds` of 0 disables the flag.
- **Failed counts are windowed.** `open` and `claimed` tasks always count, but
  `done`, `failed` and `cancelled` count only when updated within
  `since_seconds`; otherwise one old failure would pin a pipeline to the top
  forever. The windowing applies to the `tasks` counts and therefore to
  `attention.failed`.
- **`last_result`** is the newer of the newest queued `result` whisper (any
  target, unexpired) and the newest failed task's `error`. Whispers are
  deleted when acknowledged, so a result the router has already received is
  no longer shown; the failed-task error then stands in. Task titles are
  never rendered.
- **Inbox** counts the viewer's eligible whispers by type with the same
  filter as `peek_whispers` (own target or broadcast not yet delivered,
  unexpired, confidence >= 0.60 unless the type is exempt). It is not capped
  at the peek's `max_items`.
- **Lazy expiry** runs `expire_task_leases` over all pipelines when the ledger
  is enabled, so the overview does the state correction a `get`/`list` would.
  The `ncp_overview` tool runs the same expire-and-notify step as `ncp_task`
  first, so tasks that fail by expiry ring their creator. Direct library or
  CLI callers of `overview_rows` still correct state without a doorbell.
- **Rendering.** Added tokens beyond the §6.3 example: `stale<N>` in `attn:`,
  `x<N>` (cancelled) in `tasks:`, `dissent24h:<N>`. `dec:` values are reduced
  to inert tokens (<= 32 chars each) instead of being quoted. The `last:` line
  holds `status: summary`, truncated to 60 characters in total, JSON-escaped,
  with any `‹`/`›` replaced. If a row does not fit, its `last:` line is
  dropped before the row is. An empty result renders
  `(no active pipelines)`.
- **Surfaces.** `ncp overview` accepts `--json` (and `--json-output` like the
  other commands), `--pipeline` (repeatable) and `--since` as seconds or
  `30m`/`24h`/`2d`. `GET /api/overview` takes `agent_id`, `prefix`, `since`
  (seconds), `max_pipelines` and `max_tokens`. No UI panel was added: the UI
  has per-view pages but no small extension point for a summary panel, so the
  endpoint is exposed without one.
- **Gating.** `ncp_overview` has no config flag. It is not in
  `CORE_TOOL_NAMES`, so it is available in the `full` profile and absent from
  `core`. With `[tasks].enabled = false` the task fields are omitted.

---

## 7. Delivery plan

| PR | Content | Depends on | Backends |
|---|---|---|---|
| **1 — A** | `in_reply_to`/`thread_id`, `progress`/`result` types and payloads, coalescing, rendering, spec §2.3 update | — | SQLite + Redis |
| **2 — B** | `ncp/notify.py`, `ncp_wait_whispers`, `GET /events`, `[notify]` config, `api.wait_whispers`/`subscribe` | A (thread/type filters) | in-process + Redis |
| **3 — C** | `TaskRecord`, migration 018, `ncp_task`, whisper `task_id`, task notifications, owner check | A, B | SQLite + sync PG + async PG |
| **4 — D** | `ncp_overview`, CLI, `/api/overview`, UI panel | C (task columns degrade to absent without it) | all |
| later B.2 | Webhooks with HMAC | B | — |

Each PR also updates `CHANGELOG.md`, `docs/NCP_PROTOCOL_SPEC.md`,
`docs/NCP_HTTP_API.md`, `docs/NCP_ORCHESTRATOR_INTEGRATION_GUIDE.md` §7 (a
new "Router loop" pattern), the tool schema snapshot, and both plugin skill
files where the tools are described.

### 7.1 Configuration

```toml
[notify]
enabled = true            # doorbell only; no payloads leave the queue
backend = "auto"          # auto | memory | redis
max_subscribers = 64      # concurrent /events streams
max_wait_seconds = 60     # ceiling for ncp_wait_whispers

[tasks]
enabled = false           # opt-in; ncp_task hidden when false or tool_profile = "core"
default_lease_seconds = 300
max_lease_seconds = 3600
default_max_attempts = 3
stale_open_seconds = 1800 # overview attention threshold
```

### 7.2 The resulting router loop (reference pattern)

```text
loop:
  ev = ncp_wait_whispers(agent_id="router", timeout_seconds=30)   # B
  ov = ncp_overview(agent_id="router")                             # D  (cheap, bounded)
  for p in ov.pipelines needing attention:
      ctx = ncp_get_context(agent_id="router", pipeline_id=p, …)   # drains results/progress (A)
      decide → ncp_task(create, assign_to=worker, dedupe_key=…)    # C
      on verified success → ncp_record_outcome(turn_id=result.turn_id)
  ncp_post_turn(…)

worker:
  t = ncp_task(claim, slot=…) → ncp_get_context(task/slot from t)
  … work, periodic ncp_task(heartbeat, checkpoint_chunk_id, progress) …
  ncp_write_memory(finding) → ncp_task(complete, result_chunk_ids, turn_id)
```

---

## 8. Open questions

1. **Signing linkage fields.** Should `in_reply_to`/`task_id` join the
   canonical authorship payload? That would harden thread integrity, but
   changes the signing contract for clients that already sign. A likely
   answer: a `v2` canonical form that clients opt into.
2. **Task in context.** Should `ncp_get_context` accept `task_id` and
   render a one-line `[NCP:TASK]` (status, attempt, checkpoint ref)? This is
   useful for resuming after a crash, but it changes the wire format.
   Proposed: decide after PR 3, based on dogfood use.
3. **Overview scope without `pipeline_id` discipline.** Chunks written with
   no pipeline are invisible to the overview. Should it show a synthetic
   `(unscoped)` row, or omit them silently?
   **Resolved (PR 4):** omit them silently. Rows with a null `pipeline_id`
   (chunks, whispers, turns) are ignored, because an `(unscoped)` row could
   not be followed up with `ncp_get_context(pipeline_id=...)` and would sit
   permanently near the top in deployments that never set a pipeline. Operators
   who need that view can use `ncp status` or `/api/chunks`.
4. **Ledger authorization.** Should task mutations be gated by the planned
   P0-A access policy (allowed pipelines per agent) once it lands, rather
   than by a separate mechanism?

## 9. Test plan

Every PR ships tests on all backends it touches, following the existing
parity convention.

- **A:**
  - Thread resolution when the parent is present versus acknowledged.
  - Progress coalescing, including concurrent emits.
  - `result` skips the confidence filter.
  - Broadcast is rejected for `progress`/`result`.
  - Golden rendering stays unchanged for whispers without linkage fields.
- **B:**
  - `wait` returns immediately when something is already queued.
  - `wait` times out cleanly.
  - Broadcast wakes every subscriber in the pipeline and none outside it.
  - No event is published for a failed `emit_whisper`.
  - The `/events` auth, subscriber cap (`503`) and initial `ready` frame.
  - Redis notifier across two server instances.
  - No payload field ever appears in an event (schema assertion).
- **C:**
  - Concurrent claim race: N threads or processes, exactly one winner per
    task.
  - A stale `lease_token` is rejected after expiry and re-claim.
  - Lazy expiry moves the task to `open`, or to `failed` at `max_attempts`.
  - `dedupe_key` is idempotent.
  - `complete` emits a linked `result` whisper.
  - The owner check rejects a forged task-linked `result`.
  - Migration 018 up and down.
- **D:**
  - Ranking order.
  - The `max_tokens` bound holds as the number of pipelines grows (property
    test).
  - Read-only: no acknowledgements, no `retrieval_count` change.
  - Agent-written text is quoted and truncated.
  - Degrades cleanly when the ledger is disabled.
