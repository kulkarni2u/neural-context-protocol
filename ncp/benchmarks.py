"""Reproducible benchmark helpers for NCP launch-credibility checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ncp.bench.baselines import (
    BaselineStrategy,
    RawReplayBaseline,
    RollingSummaryBaseline,
    SlidingWindowBaseline,
)

from ncp.assembler import Assembler
from ncp.api import agent
from ncp.config import NCPConfig
from ncp.costs import assembly_overhead, calculate_cost
from ncp.stores.consolidation import reduce_candidates
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_tokens, token_unit
from ncp.types import BudgetContext, NCPResponse, SubconsciousChunk


_CODING_ROLE_ROTATION: list[tuple[str, str, list[str], list[str]]] = [
    ("planner", "plan", ["planning"], ["shipping"]),
    ("executor", "build", ["implementation"], ["review"]),
    ("critic", "review", ["verification"], ["implementation"]),
    ("synthesizer", "synthesize", ["handoff"], ["planning"]),
]

_CODING_TOPICS: list[str] = [
    "auth_refresh",
    "rate_limit_retry",
    "worker_queue_backpressure",
    "stream_resume_logic",
    "sqlite_migration_guard",
    "mcp_stdio_framing",
    "tool_retry_budget",
    "cost_log_rollup",
    "context_budget_pressure",
    "release_cut_scope",
]

_RESEARCH_ROLE_ROTATION: list[tuple[str, str, list[str], list[str]]] = [
    ("lead", "plan", ["hypothesis"], ["implementation"]),
    ("retriever", "retrieve", ["sources"], ["publishing"]),
    ("analyst", "analyze", ["synthesis"], ["guessing"]),
    ("factchecker", "review", ["verification"], ["speculation"]),
    ("writer", "synthesize", ["handoff"], ["retrieval"]),
    ("editor", "review", ["quality"], ["analysis"]),
]

_RESEARCH_TOPICS: list[str] = [
    "market_structure_shift",
    "regulatory_timeline_delta",
    "pricing_signal_breakdown",
    "competitor_launch_wave",
    "supply_chain_constraint",
    "customer_segment_migration",
    "infrastructure_capacity_risk",
    "benchmark_claim_validation",
]


def _baseline_metadata(baseline: BaselineStrategy) -> dict[str, object]:
    metadata: dict[str, object] = {"name": baseline.name}
    if isinstance(baseline, SlidingWindowBaseline):
        metadata["last_entries"] = baseline.last_entries
    if isinstance(baseline, RollingSummaryBaseline):
        metadata["every_k"] = baseline.every_k
        metadata["keep_recent"] = baseline.keep_recent
    return metadata


def run_coding_pipeline_benchmark(
    *,
    store_path: str | Path,
    turns: int = 40,
    pipeline_id: str = "bench_coding_pipeline",
    context_token_budget: int | None = 340,
) -> dict[str, object]:
    """Compare NCP bounded-context growth against naive full-history replay."""

    return _run_pipeline_benchmark(
        store_path=store_path,
        turns=turns,
        pipeline_id=pipeline_id,
        benchmark_name="coding_pipeline",
        role_rotation=_CODING_ROLE_ROTATION,
        topics=_CODING_TOPICS,
        turn_builder=_build_coding_turn,
        result_builder=_make_coding_result_summary,
        critical_window=5,
        context_token_budget=context_token_budget,
    )


def run_research_pipeline_benchmark(
    *,
    store_path: str | Path,
    turns: int = 36,
    pipeline_id: str = "bench_research_pipeline",
    context_token_budget: int | None = None,
) -> dict[str, object]:
    """Compare NCP bounded-context growth against naive replay for a tool-heavy research flow."""

    return _run_pipeline_benchmark(
        store_path=store_path,
        turns=turns,
        pipeline_id=pipeline_id,
        benchmark_name="research_pipeline",
        role_rotation=_RESEARCH_ROLE_ROTATION,
        topics=_RESEARCH_TOPICS,
        turn_builder=_build_research_turn,
        result_builder=_make_research_result_summary,
        critical_window=6,
        context_token_budget=context_token_budget,
    )


def _run_pipeline_benchmark(
    *,
    store_path: str | Path,
    turns: int,
    pipeline_id: str,
    benchmark_name: str,
    role_rotation: list[tuple[str, str, list[str], list[str]]],
    topics: list[str],
    turn_builder,
    result_builder,
    critical_window: int,
    context_token_budget: int | None,
) -> dict[str, object]:
    """Run one deterministic benchmark scenario against the real assembler/store."""

    if turns < 1:
        raise ValueError("turns must be >= 1")

    store = SQLiteStore(store_path)
    assembler = Assembler(store=store)
    transcript: list[str] = []
    recent_by_agent: dict[str, list[str]] = {}
    turn_rows: list[dict[str, object]] = []
    baselines: list[BaselineStrategy] = [
        RawReplayBaseline(),
        SlidingWindowBaseline(last_entries=8),
        RollingSummaryBaseline(every_k=4, keep_recent=4),
    ]

    for index in range(turns):
        role_name, role, owns, must_not = role_rotation[index % len(role_rotation)]
        topic = topics[index % len(topics)]
        agent_id = role_name
        turn_number = index + 1
        task = f"{benchmark_name}_{topic}"
        slot = f"{role_name}_{topic}"
        turn = turn_builder(turn_number=turn_number, topic=topic)

        conscious = agent(
            id=agent_id,
            role=role,
            owns=owns,
            must_not=must_not,
            task=task,
            slot=slot,
            intent="advance_pipeline",
            pipeline_id=pipeline_id,
            recent=recent_by_agent.get(agent_id, []),
            steps_completed=index,
            steps_total=turns,
        )
        budget = BudgetContext(
            ctx_used=min(0.95, index / max(turns, 1)),
            steps_completed=index,
            steps_total=turns,
            elapsed_seconds=float(index * 11),
            pressure="critical" if index >= turns - critical_window else "medium",
        )
        assembly = assembler.assemble(
            conscious=conscious,
            budget=budget,
            query_text=f"{topic} pipeline handoff memory",
            max_tokens=context_token_budget,
        )

        ncp_context_tokens = estimate_tokens(assembly.context)
        ncp_input_tokens = estimate_tokens(assembly.context + "\n" + turn)
        baseline_contexts = {
            baseline.name: baseline.context_for(transcript=transcript, turn=turn)
            for baseline in baselines
        }
        baseline_input_tokens = {
            name: estimate_tokens((context + "\n" + turn).strip())
            for name, context in baseline_contexts.items()
        }

        result_summary = result_builder(
            turn_number=turn_number,
            role_name=role_name,
            topic=topic,
        )
        response = NCPResponse(
            content=result_summary,
            input_tokens=ncp_input_tokens,
            output_tokens=estimate_tokens(result_summary),
            cost_usd=0.0,
            model="benchmark_local",
            pipeline_id=pipeline_id,
            turn_id=f"bench_turn_{turn_number:02d}",
            latency_ms=1,
        )
        memory_chunk = SubconsciousChunk(
            chunk_id=f"bench_chunk_{turn_number:02d}",
            layer="semantic" if turn_number % 2 else "episodic",
            content=result_summary,
            src="synthesis",
            pipeline_id=pipeline_id,
            written_by=agent_id,
            relevance=0.92,
        )
        record = assembler.post_turn(
            conscious=conscious,
            response=response,
            result_summary=result_summary,
            result_full=result_summary,
            memory_chunks=[memory_chunk],
        )
        recent_by_agent[agent_id] = [f"r:sub/{record.turn_id}", *recent_by_agent.get(agent_id, [])][:5]
        transcript.extend(
            [
                f"{agent_id} TURN {turn_number:02d}: {turn}",
                f"{agent_id} RESULT {turn_number:02d}: {result_summary}",
            ]
        )

        turn_rows.append(
            {
                "turn": turn_number,
                "agent_id": agent_id,
                "topic": topic,
                "ncp_context_tokens": ncp_context_tokens,
                "ncp_input_tokens": ncp_input_tokens,
                "naive_input_tokens": baseline_input_tokens["raw_replay"],
                "raw_replay_input_tokens": baseline_input_tokens["raw_replay"],
                "sliding_window_input_tokens": baseline_input_tokens["sliding_window"],
                "rolling_summary_input_tokens": baseline_input_tokens["rolling_summary"],
            }
        )

    peak_ncp = max(int(row["ncp_input_tokens"]) for row in turn_rows)
    final_ncp = int(turn_rows[-1]["ncp_input_tokens"])
    baseline_summary: dict[str, dict[str, object]] = {}
    for baseline in baselines:
        token_key = f"{baseline.name}_input_tokens"
        peak_tokens = max(int(row[token_key]) for row in turn_rows)
        final_tokens = int(turn_rows[-1][token_key])
        reduction_factor = round(final_tokens / final_ncp, 2) if final_ncp else 0.0
        baseline_summary[baseline.name] = {
            "peak_tokens": peak_tokens,
            "final_tokens": final_tokens,
            "reduction_factor_vs_ncp": reduction_factor,
            "config": _baseline_metadata(baseline),
        }
    peak_naive = int(baseline_summary["raw_replay"]["peak_tokens"])
    final_naive = int(baseline_summary["raw_replay"]["final_tokens"])
    reduction_factor = round(final_naive / final_ncp, 2) if final_ncp else 0.0
    embed_tokens = store.embedding_tokens_estimate() if hasattr(store, "embedding_tokens_estimate") else 0
    overhead = assembly_overhead(embed_tokens=embed_tokens, retrieval_ops=turns, whisper_writes=0)
    total_token_savings_vs_raw = sum(
        int(row["raw_replay_input_tokens"]) - int(row["ncp_input_tokens"])
        for row in turn_rows
    )
    net_total_token_equivalent_vs_raw = round(total_token_savings_vs_raw - overhead.token_equivalent, 2)

    return {
        "benchmark": benchmark_name,
        "pipeline_id": pipeline_id,
        "turns": turns,
        "agents": [role for role, _, _, _ in role_rotation],
        "token_unit": token_unit(),
        "context_token_budget": context_token_budget,
        "turn_rows": turn_rows,
        "summary": {
            "ncp": {
                "peak_tokens": peak_ncp,
                "final_tokens": final_ncp,
            },
            "baselines": baseline_summary,
            "peak_ncp_tokens": peak_ncp,
            "peak_naive_tokens": peak_naive,
            "final_ncp_tokens": final_ncp,
            "final_naive_tokens": final_naive,
            "reduction_factor": reduction_factor,
            "bounded_under_2000": peak_ncp <= 2000,
            "beats_naive": peak_ncp < peak_naive,
            "beats_sliding_window": peak_ncp < int(baseline_summary["sliding_window"]["peak_tokens"]),
            "beats_rolling_summary": peak_ncp < int(baseline_summary["rolling_summary"]["peak_tokens"]),
            "economics": {
                "reference_model": "gpt-4o-mini",
                "total_token_savings_vs_raw_replay": total_token_savings_vs_raw,
                "final_turn_savings_vs_raw_replay": final_naive - final_ncp,
                "assembly_overhead_usd": round(overhead.total_cost_usd, 8),
                "assembly_overhead_token_equivalent": round(overhead.token_equivalent, 2),
                "net_total_token_equivalent_vs_raw_replay": net_total_token_equivalent_vs_raw,
            },
            "material_reduction": reduction_factor >= 3.0,
            "pass": (
                peak_ncp <= 2000
                and peak_ncp < peak_naive
                and peak_ncp < int(baseline_summary["sliding_window"]["peak_tokens"])
                and reduction_factor >= 3.0
            ),
        },
    }


def _build_coding_turn(*, turn_number: int, topic: str) -> str:
    return (
        f"Turn {turn_number:02d}: advance {topic} with one concrete update, "
        "preserve prior constraints, and prepare the next handoff."
    )


def _build_research_turn(*, turn_number: int, topic: str) -> str:
    return (
        f"Turn {turn_number:02d}: inspect {topic}, reconcile the retrieved evidence, "
        "keep attribution clear, and hand the result to the next research role."
    )


def _make_coding_result_summary(*, turn_number: int, role_name: str, topic: str) -> str:
    return (
        f"{role_name} turn {turn_number:02d} resolved {topic} by carrying forward the prior contract, "
        f"recording one concrete decision, and preserving the handoff boundary for the next agent on the pipeline."
    )


def _make_research_result_summary(*, turn_number: int, role_name: str, topic: str) -> str:
    return (
        f"{role_name} turn {turn_number:02d} resolved {topic} by comparing retrieved evidence, "
        "documenting one grounded finding, preserving source attribution, and handing off the verified thread."
    )


# ---------------------------------------------------------------------------
# High-fanout benchmark: N parallel workers -> one synthesizer (WI-P3)
# ---------------------------------------------------------------------------
#
# Fixed, hand-authored, deterministic -- no network, no randomness. Four
# topics a code-triage pipeline might surface, each with a pool of
# hand-paraphrased confirming claims (the kind of wording variance real
# parallel workers actually produce -- similar enough to be the same claim,
# never byte-identical, so write-time near-dup suppression at >0.92
# SequenceMatcher similarity does not collapse them before they even land in
# the store) plus one claim that genuinely contradicts the others.


@dataclass(slots=True, frozen=True)
class _FaninTopic:
    topic: str
    claims: list[str]
    contradiction: str


_FANIN_TOPICS: list[_FaninTopic] = [
    _FaninTopic(
        topic="payment_npe",
        claims=[
            "NPE at PaymentProcessor.java:142: retryCount is null when payment_method=ACH and customer.tier=trial.",
            "NullPointerException in PaymentProcessor.java line 142 -- retryCount comes back null for ACH trial customers.",
            "PaymentProcessor.java:142 throws NPE because retryCount is unset for trial-tier ACH payments.",
            "Root cause of the payment crash: retryCount is null at PaymentProcessor.java:142 for ACH trial accounts.",
            "Trial-tier ACH payments hit a null retryCount at PaymentProcessor.java:142, causing an NPE.",
            "PaymentProcessor.java line 142 NPEs on trial ACH customers because retryCount was never initialized.",
            "The crash trace points to PaymentProcessor.java:142 -- retryCount is null for ACH trial-tier customers.",
            "For ACH payments on trial accounts, retryCount is null and PaymentProcessor.java:142 throws an NPE.",
        ],
        contradiction=(
            "PaymentProcessor.java:142 was already patched last release; retryCount now defaults to 0 "
            "for all payment methods."
        ),
    ),
    _FaninTopic(
        topic="cache_race",
        claims=[
            "SessionStore has a cache invalidation race: two writers updating the same key within 50ms can leave a stale entry.",
            "Concurrent writes to SessionStore within a 50ms window race on cache invalidation, leaving stale sessions.",
            "SessionStore's cache invalidation is not atomic -- two near-simultaneous writers can strand a stale key.",
            "A race in SessionStore lets two writers within 50ms of each other skip invalidating the shared cache entry.",
            "SessionStore cache entries can go stale when two writers race on the same key inside a 50ms window.",
            "Cache coherency bug in SessionStore: overlapping writes under 50ms apart don't reliably invalidate the old entry.",
            "SessionStore's invalidation logic loses a race when two writers touch the same key within 50 milliseconds.",
            "Under concurrent writes 50ms apart, SessionStore can serve a stale cached value due to a missed invalidation.",
        ],
        contradiction=(
            "SessionStore's cache invalidation is already atomic per-key; the 50ms race was fixed by the "
            "mutex added in the last sprint."
        ),
    ),
    _FaninTopic(
        topic="webhook_retry",
        claims=[
            "Webhook delivery exhausts its retry budget before the backoff timer completes for rate-limited endpoints.",
            "Rate-limited webhook deliveries run out of retries before the exponential backoff finishes waiting.",
            "The retry budget for webhooks is consumed faster than the backoff schedule allows, dropping rate-limited deliveries.",
            "Webhook retries hit their cap before backoff completes when the target endpoint is rate-limiting us.",
            "For rate-limited webhook targets, the retry count runs out before the backoff delay elapses.",
            "Backoff timing for webhook retries is longer than the retry budget, so rate-limited calls give up too early.",
            "Webhook delivery gives up on rate-limited endpoints because retries are exhausted before backoff finishes.",
            "Rate-limited webhook targets drop deliveries: the retry budget is smaller than the backoff schedule needs.",
        ],
        contradiction=(
            "Webhook retry budget was raised to cover the full backoff schedule; rate-limited endpoints no "
            "longer drop deliveries."
        ),
    ),
    _FaninTopic(
        topic="auth_token_leak",
        claims=[
            "Auth tokens are not invalidated on logout, so a captured token can be replayed until its original TTL expires.",
            "Logout does not revoke the auth token -- it stays valid and replayable for the rest of its TTL window.",
            "A logged-out auth token remains usable until TTL expiry, since logout never invalidates it server-side.",
            "Token replay is possible after logout because the auth token isn't revoked, only the client-side session is cleared.",
            "Logging out clears the client session but leaves the auth token valid, replayable until it naturally expires.",
            "Server-side auth tokens outlive logout: no revocation happens, so tokens are replayable until TTL runs out.",
            "The logout flow forgets to blacklist the auth token, leaving a replay window open until TTL expiry.",
            "Auth token TTL, not logout, is what actually ends a session -- logout alone doesn't revoke the token.",
        ],
        contradiction=(
            "Logout now revokes the auth token server-side immediately; replay after logout is no longer possible."
        ),
    ),
]


def _fanin_worker_content(
    *,
    global_index: int,
    topic_index: int,
    worker_index_in_topic: int,
    contradiction_every: int,
    malformed_every: int,
) -> str:
    if malformed_every > 0 and global_index % malformed_every == 0:
        return ""
    topic = _FANIN_TOPICS[topic_index]
    if contradiction_every > 0 and worker_index_in_topic % contradiction_every == contradiction_every - 1:
        return topic.contradiction
    return topic.claims[worker_index_in_topic % len(topic.claims)]


def run_fanin_reduce_benchmark(
    *,
    store_path: str | Path,
    workers: int = 40,
    pipeline_id: str = "bench_fanin_reduce",
    k: int = 16,
    context_token_budget: int = 4000,
    synthesis_model: str = "claude-sonnet-4-20250514",
    contradiction_every: int = 7,
    malformed_every: int = 13,
) -> dict[str, object]:
    """Compare a raw N-worker fan-in dump against NCP's bounded retrieval,
    with and without the deterministic fan-in reducer (WI-P3).

    Simulates ``workers`` parallel agents (e.g. Haiku triage workers)
    writing paraphrased/duplicate findings across a fixed set of topics into
    one pipeline, including a controlled fraction of genuine contradictions
    and malformed (empty) outputs. A synthesis agent then reads the pool
    three ways, same query and same ``k`` throughout:

    - ``raw_dump``: every worker's raw content concatenated verbatim, no
      bound at all -- the naive fan-in baseline.
    - ``ncp_default``: NCP's existing bounded top-k retrieval (BM25 + trust
      + recency), reducer off. This already beats ``raw_dump`` on tokens
      (that's the pre-existing, separately-benchmarked win of bounded
      retrieval) -- but bounding by count doesn't dedup content, so some of
      those ``k`` slots can be near-duplicates of each other.
    - ``ncp_reduced``: the same retrieval, overfetched past ``k`` so the
      reducer has a real pool to work with, deterministically merged to the
      highest-trust version per near-duplicate cluster (dropping malformed
      candidates, flagging genuine contradictions) before the same top-k
      cap is applied.

    Because ``ncp_default`` and ``ncp_reduced`` return the same chunk count
    at the same ``k``, comparing their raw token totals mostly measures
    which *specific* chunks won the ranking, not reduction quality -- and
    ``ncp_reduced``'s per-chunk contradiction notes add a little text on
    top. The metric that actually isolates the reducer's effect is
    ``wasted_duplicate_slots_in_default``: running the reducer as a
    read-only diagnostic over ``ncp_default``'s own already-capped output
    and counting how many of its ``k`` slots are near-duplicates of another
    slot in that same result -- context budget spent twice on one claim
    instead of once each on two different ones.

    Cost is reported for the synthesis model's input tokens only, at a
    fixed synthetic output-token count held constant across scenarios --
    this isolates the cost effect of what reaches the synthesizer, not
    worker cost (workers already ran regardless of what downstream
    reduction does to their output).
    """

    if workers < 1:
        raise ValueError("workers must be >= 1")

    store = SQLiteStore(store_path)
    raw_segments: list[str] = []
    for index in range(workers):
        topic_index = index % len(_FANIN_TOPICS)
        worker_index_in_topic = index // len(_FANIN_TOPICS)
        content = _fanin_worker_content(
            global_index=index,
            topic_index=topic_index,
            worker_index_in_topic=worker_index_in_topic,
            contradiction_every=contradiction_every,
            malformed_every=malformed_every,
        )
        raw_segments.append(f"worker_{index:02d}: {content or '[no output]'}")
        store.write(
            SubconsciousChunk(
                chunk_id=f"fanin_worker_{index:02d}",
                layer="episodic",
                content=content or " ",
                src="agent_inferred",
                pipeline_id=pipeline_id,
                written_by=f"worker_{index:02d}",
                base_trust=round(0.5 + 0.4 * ((index * 7) % 10) / 10, 2),
            )
        )

    raw_dump_text = "\n".join(raw_segments)
    query_text = " ".join(topic.topic.replace("_", " ") for topic in _FANIN_TOPICS)

    conscious = agent(
        id="synthesizer",
        role="synthesize",
        owns=["handoff"],
        must_not=["speculation"],
        task="triage_fanin_findings",
        slot="synthesize_worker_reports",
        intent="produce_grounded_summary",
        pipeline_id=pipeline_id,
    )
    budget = BudgetContext(
        ctx_used=0.3, steps_completed=workers, steps_total=workers, elapsed_seconds=float(workers)
    )

    default_assembler = Assembler(store=store)
    default_result = default_assembler.assemble(
        conscious=conscious, budget=budget, query_text=query_text, k=k, max_tokens=context_token_budget
    )
    default_config = NCPConfig(values={}, project_root=Path(store_path).parent)
    # Read-only diagnostic: how much of ncp_default's own already-capped
    # output is wasted on a near-duplicate of another slot in that same
    # result? Uses the module defaults (same values reduce_fanin_enabled
    # would use), just applied after the fact instead of before the cap.
    default_self_reduced = reduce_candidates(
        default_result.chunks,
        similarity_threshold=default_config.reduce_fanin_similarity_threshold,
        contradict_floor=default_config.reduce_fanin_contradict_floor,
        min_cluster=3,
    )

    reduced_config = NCPConfig(
        values={"retrieval": {"reduce_fanin_enabled": True, "reduce_fanin_overfetch": 8}},
        project_root=Path(store_path).parent,
    )
    reduced_assembler = Assembler(store=store, config=reduced_config)
    reduced_result = reduced_assembler.assemble(
        conscious=conscious, budget=budget, query_text=query_text, k=k, max_tokens=context_token_budget
    )

    raw_dump_tokens = estimate_tokens(raw_dump_text)
    default_tokens = estimate_tokens(default_result.context)
    reduced_tokens = estimate_tokens(reduced_result.context)

    synth_output_tokens = 200  # fixed across scenarios: isolates the input-token effect of reduction
    raw_dump_cost = calculate_cost(
        model=synthesis_model, input_tokens=raw_dump_tokens, output_tokens=synth_output_tokens
    ).total_cost_usd
    default_cost = calculate_cost(
        model=synthesis_model, input_tokens=default_tokens, output_tokens=synth_output_tokens
    ).total_cost_usd
    reduced_cost = calculate_cost(
        model=synthesis_model, input_tokens=reduced_tokens, output_tokens=synth_output_tokens
    ).total_cost_usd

    def _reduction_ratio(before: int, after: int) -> float:
        return round(1 - (after / before), 4) if before else 0.0

    wasted_duplicate_slots = len(default_self_reduced.merged_away)

    # Ground-truth-backed precision check, possible only because this
    # corpus's topic labels are known: what fraction of flagged
    # contradiction pairs are actually about the same synthetic topic,
    # versus two candidates from different topics that happened to clear
    # the similarity floor together? Real production data has no such
    # ground truth -- this number exists to keep the reducer's contradiction
    # signal honestly characterized, not to claim it as a precision metric
    # end users can expect at unknown corpora.
    def _topic_index(chunk_id: str) -> int:
        return int(chunk_id.rsplit("_", 1)[-1]) % len(_FANIN_TOPICS)

    same_topic_pairs = sum(
        1 for a, b in reduced_result.fanin_contradictions if _topic_index(a) == _topic_index(b)
    )
    contradiction_count = len(reduced_result.fanin_contradictions)
    contradiction_same_topic_fraction = (
        round(same_topic_pairs / contradiction_count, 4) if contradiction_count else None
    )

    return {
        "benchmark": "fanin_reduce",
        "pipeline_id": pipeline_id,
        "token_unit": token_unit(),
        "config": {
            "workers": workers,
            "topics": [topic.topic for topic in _FANIN_TOPICS],
            "k": k,
            "context_token_budget": context_token_budget,
            "synthesis_model": synthesis_model,
            "synth_output_tokens_fixed": synth_output_tokens,
            "contradiction_every": contradiction_every,
            "malformed_every": malformed_every,
        },
        "scenarios": {
            "raw_dump": {
                "description": "All worker outputs concatenated verbatim, no bound at all -- the naive fan-in baseline.",
                "input_tokens": raw_dump_tokens,
                "cost_usd": round(raw_dump_cost, 6),
                "chunk_count": workers,
            },
            "ncp_default": {
                "description": "NCP's existing bounded top-k retrieval (BM25+trust+recency); reduce_fanin off.",
                "input_tokens": default_tokens,
                "cost_usd": round(default_cost, 6),
                "chunk_count": len(default_result.chunks),
                "wasted_duplicate_slots_in_default": wasted_duplicate_slots,
            },
            "ncp_reduced": {
                "description": "Same retrieval, overfetched and reduced before the same top-k cap; reduce_fanin on.",
                "input_tokens": reduced_tokens,
                "cost_usd": round(reduced_cost, 6),
                "chunk_count": len(reduced_result.chunks),
                "fanin_merged_count": reduced_result.fanin_merged_count,
                "fanin_contradictions_count": contradiction_count,
                "fanin_dropped_malformed_count": reduced_result.fanin_dropped_malformed_count,
            },
        },
        "summary": {
            "token_reduction_vs_raw_dump": _reduction_ratio(raw_dump_tokens, reduced_tokens),
            "cost_reduction_usd_vs_raw_dump": round(raw_dump_cost - reduced_cost, 6),
            "wasted_duplicate_slots_in_default": wasted_duplicate_slots,
            "wasted_duplicate_slot_fraction_in_default": (
                round(wasted_duplicate_slots / len(default_result.chunks), 4) if default_result.chunks else 0.0
            ),
            "contradictions_surfaced": contradiction_count,
            "contradiction_same_topic_fraction": contradiction_same_topic_fraction,
            "merged_near_duplicates": reduced_result.fanin_merged_count,
            "malformed_dropped": reduced_result.fanin_dropped_malformed_count,
            "pass": (
                reduced_tokens < raw_dump_tokens
                and reduced_result.fanin_merged_count > 0
                and wasted_duplicate_slots > 0
            ),
        },
    }
