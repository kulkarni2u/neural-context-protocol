"""Public API helpers for the NCP runtime."""

from __future__ import annotations

import json
from pathlib import Path
import time
from collections.abc import Sequence
from typing import ContextManager
from uuid import uuid4

from ncp.adapters.base import BaseAdapter
from ncp.adapters.local import LocalAdapter
from ncp.assembler import Assembler, AssemblyResult
from ncp.config import NCPConfig, load_config
from ncp.context_policy import ResolvedContextPolicy, resolve_context_policy
from ncp.costs import calculate_cost
from ncp.overview import DEFAULT_MAX_PIPELINES, DEFAULT_MAX_TOKENS, DEFAULT_SINCE_SECONDS
from ncp.overview import overview as _build_overview
from ncp.notify import (
    Subscription,
    effective_poll_interval,
    emit_and_notify,
    get_process_notifier,
    polling_enabled,
    subscribe_with_poll,
    wait_for_whispers,
)
from ncp.stores.base import BaseStore
from ncp.stores.factory import create_store
from ncp.tasks import TaskService, TaskSettings, check_task_owner
from ncp.stores.memo import compute_dependency_fingerprint, compute_turn_memo_signature, memo_ineligibility
from ncp.tokens import estimate_tokens
from ncp.types import (
    BudgetContext,
    ConsciousBlock,
    DecisionRecord,
    EvidenceAttribution,
    NCPResponse,
    SubconsciousChunk,
    THREAD_REPLY_WHISPER_TYPES,
    Whisper,
    resolve_whisper_thread,
)

_CONFIG: NCPConfig | None = None


def configure(
    *,
    path: str | Path | None = None,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> NCPConfig:
    """Load and cache the active NCP configuration."""

    global _CONFIG
    _CONFIG = load_config(path=path, cwd=cwd, env=env)
    return _CONFIG


def agent(
    *,
    id: str,
    role: str,
    owns: list[str] | None = None,
    must_not: list[str] | None = None,
    task: str = "idle",
    slot: str = "unassigned",
    intent: str = "maintain_context",
    **overrides: object,
) -> ConsciousBlock:
    """Create a conscious-block template through the public API."""

    payload = {
        "agent_id": id,
        "role": role,
        "owns": owns or [],
        "must_not": must_not or [],
        "task": task,
        "slot": slot,
        "intent": intent,
        **overrides,
    }
    return ConsciousBlock(**payload)


def get_context(
    *,
    agent: ConsciousBlock,
    budget: BudgetContext | None = None,
    query_text: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    k: int | None = None,
    diversity_limit: int | None = None,
    max_tokens: int | None = None,
    context_policy: str | None = None,
    ontology: object = None,
) -> str:
    """Assemble raw pidgin context for one agent turn.

    ``ontology`` is the optional per-request extension of ``ncp_get_context``
    (``False`` opts out; ``{"mode": ...}`` needs admin). It is ignored while
    ``[ontology]`` is disabled, so the default output is the legacy context.
    """

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    policy = resolve_context_policy(resolved_config, context_policy)
    enricher = None
    if resolved_config.ontology.enabled:
        from ncp.ontology.enrichment import build_enricher
        from ncp.ontology.facade import validate_config_backend

        # Config-level check first: unsupported backends fail even for
        # requests that opt out (mode off).
        validate_config_backend(resolved_config, resolved_store)
        # In-process hosts have no transport principal: the facade's
        # unauthenticated capabilities apply (principal "local").
        enricher = build_enricher(resolved_config, resolved_store, request=ontology)
    assembler = Assembler(store=resolved_store, config=policy.config, ontology=enricher)
    return assembler.assemble(
        conscious=agent,
        budget=budget or BudgetContext(),
        query_text=query_text,
        k=k,
        diversity_limit=diversity_limit,
        max_tokens=policy.cap_max_tokens(max_tokens),
    ).context


def compile_decision_query(
    *,
    agent: ConsciousBlock,
    schema_id: str,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    k: int = 6,
    min_confidence: float = 0.0,
) -> dict:
    """Compile a bounded decision packet for a decision backend (spec 4h).

    Makes no provider calls: this is store reads plus arithmetic. Returns the
    same object shape as the ``ncp_compile_decision_query`` MCP tool, because
    it is literally the same handler -- the library and MCP surfaces cannot
    drift apart into two subtly different packets.

    NCP compiles; it does not choose. ``escalate`` says whether a stronger
    backend is warranted and ``escalate_reasons`` says why, but nothing here
    names a model: picking the backend is the host's job.
    """
    from ncp.mcp.server import make_handlers

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    handler = make_handlers(resolved_store, config=resolved_config)["ncp_compile_decision_query"]
    result = handler({
        "agent_id": agent.agent_id,
        "role": agent.role,
        "owns": agent.owns,
        "must_not": agent.must_not,
        "task": agent.task,
        "slot": agent.slot,
        "intent": agent.intent,
        "schema_id": schema_id,
        "pipeline_id": agent.pipeline_id,
        "tried": agent.tried,
        "failed": agent.failed,
        "drift_score": agent.drift_score,
        "slot_confidence": agent.slot_confidence,
        "pressure": agent.pressure,
        "k": k,
        "min_confidence": min_confidence,
    })
    return dict(result)  # type: ignore[arg-type]


def record_decision(
    decision: DecisionRecord,
    *,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> bool:
    """Validate and persist a typed decision using the same policy as MCP.

    Returns False when decisions are disabled. Invalid schemas, choices or
    versions raise ValueError before writing. Honors dual_write_chunks and
    preserves the supplied decision ID and metadata.
    """

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    from ncp.decisions import SchemaRegistry, persist_decision_record

    return persist_decision_record(
        decision, store=resolved_store,
        registry=SchemaRegistry.load(inline=resolved_config.decision_schemas,
                                     project_root=resolved_config.project_root),
        enabled=resolved_config.decisions_enabled,
        strict_schemas=resolved_config.decisions_strict_registered_schemas,
        dual_write_chunks=resolved_config.decisions_dual_write_chunks,
    )


def get_decision(
    decision_id: str,
    *,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> DecisionRecord | None:
    """Fetch one typed decision by id, or None when the store has no such row."""

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    return resolved_store.get_decision(decision_id)


def write_memory(
    chunk: SubconsciousChunk,
    *,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> bool:
    """Persist one chunk through the public API."""

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    return resolved_store.write(chunk)


def emit(
    whisper: Whisper,
    *,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> str:
    """Persist one whisper and return its whisper_id for delivery tracking.

    A ``progress``/``result`` whisper carrying ``task_id`` must come from the
    current owner of that claimed task (the same check ``ncp_emit_whisper``
    applies); otherwise ``ValueError`` is raised and nothing is persisted.
    """

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    if whisper.task_id is not None and whisper.whisper_type in THREAD_REPLY_WHISPER_TYPES:
        check_task_owner(resolved_store, whisper, enabled=resolved_config.tasks_enabled)
    whisper = resolve_whisper_thread(
        whisper,
        None if whisper.in_reply_to is None else resolved_store.get_whisper(whisper.in_reply_to),
    )
    emit_and_notify(resolved_store, get_process_notifier(resolved_config, store=resolved_store), whisper)
    return whisper.whisper_id


def wait_whispers(
    *,
    agent_id: str,
    pipeline_id: str | None = None,
    timeout: float = 30.0,
    types: Sequence[str] | None = None,
    thread_id: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> list[Whisper]:
    """Wait until a whisper is queued for ``agent_id`` and return it (peek only).

    Returns the eligible whispers without draining them; follow with
    ``get_context``/``run`` to receive and acknowledge. Empty list on timeout.
    ``timeout`` is clamped to ``[notify].max_wait_seconds``.
    """

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    notifier = get_process_notifier(resolved_config, store=resolved_store)
    return wait_for_whispers(
        resolved_store,
        notifier,
        agent_id=agent_id,
        pipeline_id=pipeline_id,
        timeout=min(max(0.0, timeout), resolved_config.notify_max_wait_seconds),
        types=set(types) if types else None,
        thread_id=thread_id,
        poll_interval=effective_poll_interval(resolved_config),
    )


def _task_service(store: BaseStore | None, config: NCPConfig | None) -> TaskService:
    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    service = TaskService(
        store or create_store(resolved_config),
        None,
        TaskSettings.from_config(resolved_config),
    )
    service.require_enabled("the task ledger")
    # Use the process notifier, as emit() does (it needs the resolved store).
    service.notifier = get_process_notifier(resolved_config, store=service.store)
    return service


def create_task(
    *,
    agent_id: str,
    pipeline_id: str,
    slot: str,
    title: str,
    assign_to: str | None = None,
    max_attempts: int | None = None,
    dedupe_key: str | None = None,
    parent_task_id: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> dict[str, object]:
    """Create a ledger task (same result dict as ``ncp_task`` action ``create``).

    ``assign_to`` also emits a ``request`` whisper linked to the task. Needs a
    synchronous store (``AsyncPgvectorStore`` has only ``async_*`` task
    methods). Raises ``ValueError`` when ``[tasks].enabled`` is false.
    """

    return _task_service(store, config).create(
        agent_id=agent_id,
        pipeline_id=pipeline_id,
        slot=slot,
        title=title,
        max_attempts=max_attempts,
        dedupe_key=dedupe_key,
        parent_task_id=parent_task_id,
        assign_to=assign_to,
    )


def claim_task(
    *,
    agent_id: str,
    task_id: str | None = None,
    pipeline_id: str | None = None,
    slot: str | None = None,
    lease_seconds: float | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> dict[str, object]:
    """Claim a task by id or the next open one in a pipeline; returns the ``lease_token``.

    The lease token is returned only here. Needs a synchronous store. Raises
    ``ValueError`` when ``[tasks].enabled`` is false.
    """

    return _task_service(store, config).claim(
        agent_id=agent_id,
        task_id=task_id,
        pipeline_id=pipeline_id,
        slot=slot,
        lease_seconds=lease_seconds,
    )


def heartbeat_task(
    *,
    task_id: str,
    lease_token: str,
    lease_seconds: float | None = None,
    checkpoint_chunk_id: str | None = None,
    progress: dict[str, object] | None = None,
    pipeline_id: str | None = None,
    agent_id: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> dict[str, object]:
    """Extend a lease; an optional ``progress`` payload emits a coalesced progress whisper.

    Needs a synchronous store. Raises ``ValueError`` when ``[tasks].enabled``
    is false.
    """

    return _task_service(store, config).heartbeat(
        task_id=task_id,
        lease_token=lease_token,
        lease_seconds=lease_seconds,
        checkpoint_chunk_id=checkpoint_chunk_id,
        progress=progress,
        pipeline_id=pipeline_id,
        agent_id=agent_id,
    )


def release_task(
    *,
    task_id: str,
    lease_token: str,
    error: str | None = None,
    pipeline_id: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> dict[str, object]:
    """Give a claimed task back (it fails once attempts are exhausted).

    Needs a synchronous store. Raises ``ValueError`` when ``[tasks].enabled``
    is false.
    """

    return _task_service(store, config).release(
        task_id=task_id, lease_token=lease_token, error=error, pipeline_id=pipeline_id
    )


def complete_task(
    *,
    task_id: str,
    lease_token: str,
    summary: str,
    status: str = "done",
    result_chunk_ids: Sequence[str] | None = None,
    turn_id: str | None = None,
    pipeline_id: str | None = None,
    agent_id: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> dict[str, object]:
    """Finish a task (``status`` ``done`` or ``failed``) and emit the linked ``result`` whisper.

    Never records an outcome (use ``ncp_record_outcome`` separately). Needs a
    synchronous store. Raises ``ValueError`` when ``[tasks].enabled`` is false.
    """

    return _task_service(store, config).complete(
        task_id=task_id,
        lease_token=lease_token,
        summary=summary,
        status=status,
        result_chunk_ids=None if result_chunk_ids is None else list(result_chunk_ids),
        turn_id=turn_id,
        pipeline_id=pipeline_id,
        agent_id=agent_id,
    )


def cancel_task(
    *,
    agent_id: str,
    task_id: str,
    reason: str | None = None,
    pipeline_id: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> dict[str, object]:
    """Cancel a task (creator only); a claimed owner is rung via the doorbell.

    Needs a synchronous store. Raises ``ValueError`` when ``[tasks].enabled``
    is false.
    """

    return _task_service(store, config).cancel(
        agent_id=agent_id, task_id=task_id, reason=reason, pipeline_id=pipeline_id
    )


def get_task(
    *,
    task_id: str,
    pipeline_id: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> dict[str, object]:
    """Fetch one task (public fields, never the lease token).

    Needs a synchronous store. Raises ``ValueError`` when ``[tasks].enabled``
    is false.
    """

    return _task_service(store, config).get(task_id=task_id, pipeline_id=pipeline_id)


def list_tasks(
    *,
    pipeline_id: str,
    status: str | None = None,
    slot: str | None = None,
    owner: str | None = None,
    limit: int = 20,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> dict[str, object]:
    """List a pipeline's tasks (``limit`` clamped to 1..50).

    Needs a synchronous store. Raises ``ValueError`` when ``[tasks].enabled``
    is false.
    """

    return _task_service(store, config).list(
        pipeline_id=pipeline_id, status=status, slot=slot, owner=owner, limit=limit
    )


def overview(
    *,
    agent_id: str | None = None,
    pipelines: Sequence[str] | None = None,
    prefix: str | None = None,
    since_seconds: float | None = DEFAULT_SINCE_SECONDS,
    max_pipelines: int = DEFAULT_MAX_PIPELINES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
) -> dict[str, object]:
    """Ranked, token-bounded cross-pipeline overview (read-only).

    Returns ``{"text", "pipelines", "omitted", "tokens_est"}``. ``agent_id``
    is the viewer whose inbox is counted (peeked, never drained). Task fields
    are omitted unless ``[tasks].enabled``.
    """

    from ncp.overview import clamp_limits

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    max_pipelines, max_tokens = clamp_limits(max_pipelines, max_tokens)
    return _build_overview(
        resolved_store,
        viewer=agent_id,
        pipelines=pipelines,
        pipeline_prefix=prefix,
        since_seconds=since_seconds,
        max_pipelines=max_pipelines,
        max_tokens=max_tokens,
        include_tasks=resolved_config.tasks_enabled,
        stale_open_seconds=resolved_config.tasks_stale_open_seconds,
    )


def subscribe(
    *,
    agent_id: str,
    pipeline_id: str | None = None,
    config: NCPConfig | None = None,
    store: BaseStore | None = None,
) -> ContextManager[Subscription]:
    """Subscribe to doorbell events (no payloads) for in-process orchestrators.

    Use as ``with subscribe(agent_id=...) as sub: for event in sub: ...`` or
    ``sub.poll(timeout)``. Raises ``RuntimeError`` when ``[notify]`` is disabled.

    With the in-process notifier and ``[notify].poll_interval_seconds > 0`` the
    subscription also re-peeks the store, so whispers written by another
    process are announced too (whispers already queued at subscribe time are
    not). ``task`` events stay in-process-only.
    """

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    notifier = get_process_notifier(resolved_config, store=store)
    if notifier is None:
        raise RuntimeError("Push delivery is disabled ([notify].enabled = false).")
    interval = effective_poll_interval(resolved_config)
    if not polling_enabled(notifier, interval):
        return notifier.subscribe(agent_id=agent_id, pipeline_id=pipeline_id)
    return subscribe_with_poll(
        notifier,
        store or create_store(resolved_config),
        agent_id=agent_id,
        pipeline_id=pipeline_id,
        poll_interval=interval,
    )


def run(
    *,
    agent: ConsciousBlock,
    turn: str,
    adapter: BaseAdapter | None = None,
    budget: BudgetContext | None = None,
    query_text: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    k: int | None = None,
    diversity_limit: int | None = None,
    max_tokens: int | None = None,
    context_policy: str | None = None,
) -> NCPResponse:
    """Run one blocking local-runtime call through an adapter."""

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    resolved_budget = budget or BudgetContext()
    resolved_adapter = adapter or LocalAdapter()
    policy = resolve_context_policy(resolved_config, context_policy)
    assembler = Assembler(store=resolved_store, config=policy.config)

    start = time.perf_counter()
    assembly = assembler.assemble(
        conscious=agent,
        budget=resolved_budget,
        query_text=query_text or turn,
        ctx_window=resolved_adapter.ctx_window,
        k=k,
        diversity_limit=diversity_limit,
        max_tokens=policy.cap_max_tokens(max_tokens),
    )

    memo_signature = _memo_signature(resolved_config, agent, turn, resolved_adapter, assembly, policy)
    memo = _usable_memo(resolved_store, resolved_config, memo_signature)
    if memo is not None:
        content = str(memo["result_summary"])
        response = _build_memo_response(agent=agent, adapter=resolved_adapter, memo=memo, start=start)
    else:
        _reset_usage(resolved_adapter)
        content = resolved_adapter.call(assembly.context, turn)
        response = _build_response(
            agent=agent,
            adapter=resolved_adapter,
            context=assembly.context,
            turn=turn,
            content=content,
            start=start,
            config=resolved_config,
        )

    response = _finish_turn(
        assembler=assembler,
        assembly=assembly,
        agent=agent,
        turn=turn,
        content=content,
        response=response,
        memo=memo,
        memo_signature=memo_signature,
        policy_block=policy.describe(requested_max_tokens=max_tokens),
    )
    return response


def stream(
    *,
    agent: ConsciousBlock,
    turn: str,
    adapter: BaseAdapter | None = None,
    budget: BudgetContext | None = None,
    query_text: str | None = None,
    store: BaseStore | None = None,
    config: NCPConfig | None = None,
    k: int | None = None,
    diversity_limit: int | None = None,
    max_tokens: int | None = None,
    context_policy: str | None = None,
):
    """Yield a streamed response through the adapter."""

    resolved_config = config or _CONFIG or configure(cwd=Path.cwd())
    resolved_store = store or create_store(resolved_config)
    resolved_budget = budget or BudgetContext()
    resolved_adapter = adapter or LocalAdapter()
    policy = resolve_context_policy(resolved_config, context_policy)
    assembler = Assembler(store=resolved_store, config=policy.config)
    assembly = assembler.assemble(
        conscious=agent,
        budget=resolved_budget,
        query_text=query_text or turn,
        ctx_window=resolved_adapter.ctx_window,
        k=k,
        diversity_limit=diversity_limit,
        max_tokens=policy.cap_max_tokens(max_tokens),
    )
    start = time.perf_counter()

    memo_signature = _memo_signature(resolved_config, agent, turn, resolved_adapter, assembly, policy)
    memo = _usable_memo(resolved_store, resolved_config, memo_signature)
    if memo is not None:
        # Memo hit: synthesize a single-chunk "stream" from the cached content
        # instead of calling the provider.
        content = str(memo["result_summary"])
        yield content
        response = _build_memo_response(agent=agent, adapter=resolved_adapter, memo=memo, start=start)
    else:
        chunks: list[str] = []
        _reset_usage(resolved_adapter)
        for chunk in resolved_adapter.stream(assembly.context, turn):
            chunks.append(chunk)
            yield chunk

        content = "".join(chunks)
        response = _build_response(
            agent=agent,
            adapter=resolved_adapter,
            context=assembly.context,
            turn=turn,
            content=content,
            start=start,
            config=resolved_config,
        )

    _finish_turn(
        assembler=assembler,
        assembly=assembly,
        agent=agent,
        turn=turn,
        content=content,
        response=response,
        memo=memo,
        memo_signature=memo_signature,
        policy_block=policy.describe(requested_max_tokens=max_tokens),
    )


def _attribute_evidence(
    assembly: AssemblyResult,
    memo: dict[str, object] | None,
    memo_signature: str | None = None,
) -> EvidenceAttribution:
    """Evidence a run/stream turn rests on (see ``EvidenceAttribution``)."""
    if memo is not None:
        raw = memo.get("chunk_ids") or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = []
        memo_ids = [str(c) for c in raw] if isinstance(raw, list) else []
        return EvidenceAttribution(presented=False, memo_chunk_ids=memo_ids, memo_signature=memo_signature)
    return EvidenceAttribution(
        presented=True,
        chunk_ids=assembly.stored_evidence_ids(),
        sources=dict(assembly.evidence_sources),  # type: ignore[arg-type]
        whisper_ids=list(assembly.pending_whisper_ids),
        memo_signature=memo_signature,
    )


def _reset_usage(adapter: BaseAdapter) -> None:
    """Clear usage left by a previous request so it is never billed twice."""
    try:
        adapter.last_usage = None
    except AttributeError:
        pass


def _finish_turn(
    *,
    assembler: Assembler,
    assembly: AssemblyResult,
    agent: ConsciousBlock,
    turn: str,
    content: str,
    response: NCPResponse,
    memo: dict[str, object] | None,
    memo_signature: str | None,
    policy_block: dict[str, object] | None = None,
) -> NCPResponse:
    """Shared run/stream tail: attribute evidence, memoize, and post the turn."""
    evidence = _attribute_evidence(assembly, memo, memo_signature)
    response = response.model_copy(update={"evidence": evidence, "context_policy": policy_block})
    if memo is None and memo_signature:
        assembler.store.record_memo(
            signature=memo_signature,
            task=turn,
            chunk_ids=evidence.chunk_ids,
            result_summary=content,
            output_tokens_est=response.output_tokens,
        )
    assembler.post_turn(
        conscious=agent,
        response=response,
        result_summary=content.splitlines()[0] if content else "",
        result_full=content,
        retrieved_chunk_ids=evidence.chunk_ids or None,
        # Reached only after the adapter returned (or the stream completed),
        # so a failed or abandoned request leaves its whispers pending.
        ack_whisper_ids=evidence.whisper_ids or None,
    )
    return response


def _memo_signature(
    config: NCPConfig,
    agent: ConsciousBlock,
    turn: str,
    adapter: BaseAdapter,
    assembly: AssemblyResult,
    policy: ResolvedContextPolicy,
) -> str | None:
    """Canonical memo key for this turn, or None when memoization is disabled.

    Keys on the agent's identity, pipeline, goal and constraints, the target
    model/provider, and a fingerprint of the stored evidence, pending
    whispers and context policy actually assembled -- so a memo is never
    reused across pipelines or models, or after the evidence it was built
    from changed. Volatile parts of the context (recent-turn refs, live
    budget figures) are deliberately left out so retries of the same work
    still repeat the key.
    """
    if not config.memoization_enabled:
        return None
    stored = set(assembly.stored_evidence_ids())
    fingerprint = compute_dependency_fingerprint(
        evidence=[(c.chunk_id, c.content) for c in assembly.chunks if c.chunk_id in stored],
        whisper_ids=[w.whisper_id for w in assembly.whispers],
        policy_sha256=None if policy.identity is None else policy.identity.sha256,
    )
    return compute_turn_memo_signature(
        turn=turn,
        agent_id=agent.agent_id,
        role=agent.role,
        pipeline_id=agent.pipeline_id,
        task=agent.task,
        slot=agent.slot,
        intent=agent.intent,
        goal_version=agent.goal_version,
        owns=agent.owns,
        must_not=agent.must_not,
        model=adapter.model_name,
        provider=type(adapter).__name__,
        dependency_fingerprint=fingerprint,
    )


def _usable_memo(store: BaseStore, config: NCPConfig, signature: str | None) -> dict[str, object] | None:
    """The memo for ``signature`` if it passes the shared reuse gate, else None.

    Uses the telemetry-neutral peek so a found-but-ineligible memo counts as
    a miss, exactly like ``ncp_lookup_memo``.
    """
    if signature is None:
        return None
    memo = store.peek_memo(signature)
    if memo is not None and memo_ineligibility(
        memo,
        min_outcome=config.memoization_min_outcome,
        allow_unverified=config.memoization_allow_unverified,
    ) is None:
        store.record_memo_hit(signature)
        return memo
    store.record_memo_miss()
    return None


def _build_memo_response(
    *,
    agent: ConsciousBlock,
    adapter: BaseAdapter,
    memo: dict[str, object],
    start: float,
) -> NCPResponse:
    """Build the response for a CAP-C3 memo hit: no provider call, no cost."""

    return NCPResponse(
        content=str(memo.get("result_summary") or ""),
        input_tokens=0,
        output_tokens=int(memo.get("output_tokens_est") or 0),
        cache_read_tokens=0,
        cost_usd=0.0,
        model=adapter.model_name,
        pipeline_id=agent.pipeline_id,
        turn_id=f"turn_{int(time.time() * 1000)}_{uuid4().hex[:8]}",
        latency_ms=int((time.perf_counter() - start) * 1000),
        cost_source="memoized",
    )


def _build_response(
    *,
    agent: ConsciousBlock,
    adapter: BaseAdapter,
    context: str,
    turn: str,
    content: str,
    start: float,
    config: NCPConfig | None = None,
) -> NCPResponse:
    model = adapter.model_name
    usage = getattr(adapter, "last_usage", None)

    if usage is not None:
        # Real provider reported authoritative token counts: bill them.
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_read_tokens = usage.cache_read_tokens
        cache_write_tokens = usage.cache_write_tokens
        reasoning_tokens = usage.reasoning_tokens
        cost_source = "measured"
        pricing = config.pricing if config is not None else None
        try:
            cost_usd = calculate_cost(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                pricing=pricing,
            ).total_cost_usd
        except KeyError:
            # Real tokens but no pricing entry for this model; leave cost unpriced.
            cost_usd = 0.0
    else:
        # Local/mock/in-process path: tokens are a chars/4 estimate, not
        # authoritative, and are never priced.
        input_tokens = estimate_tokens(context + "\n" + turn)
        output_tokens = estimate_tokens(content)
        cache_read_tokens = 0
        cache_write_tokens = 0
        reasoning_tokens = 0
        cost_usd = 0.0
        cost_source = "estimated"

    return NCPResponse(
        content=content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=reasoning_tokens,
        cost_usd=cost_usd,
        model=model,
        pipeline_id=agent.pipeline_id,
        turn_id=f"turn_{int(time.time() * 1000)}_{uuid4().hex[:8]}",
        latency_ms=int((time.perf_counter() - start) * 1000),
        cost_source=cost_source,
    )
