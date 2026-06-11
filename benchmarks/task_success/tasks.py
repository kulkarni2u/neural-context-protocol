"""Task definitions for the task-success benchmark.

Each task plants a fictional, training-data-unguessable "approved path" slug
plus 2-3 "dead end" slugs into a scripted multi-agent transcript. The approved
path and the dead-end rejections are planted EARLY (within the first few
turns) so that a recency-based sliding window — which keeps only the most
recent transcript entries — drops them once enough filler turns accumulate.
NCP, by contrast, retrieves by relevance/conditions rather than recency, so
the planted facts can survive into a budget-matched context.

Scoring is deterministic and reused (in spirit) from
``benchmarks/efficacy/run.py``: success requires the response to name the
approved-path slug AND to not propose any dead-end slug except in a negation
context (e.g. "will not use X", "X was rejected"). The negation-window logic
is reimplemented locally to keep this package self-contained.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Negation handling (local reimplementation of efficacy's
# ``_mentions_dead_end_as_retry``, kept in sync by convention).
# ---------------------------------------------------------------------------

NEGATION_MARKERS: tuple[str, ...] = (
    "will not use",
    "do not use",
    "don't use",
    "won't use",
    "not use",
    "avoid",
    "rejected",
    "forbidden",
    "decommissioned",
    "deprecated",
    "removed from the allowed",
    "do not propose",
    "must not",
)


def mentions_dead_end_as_retry(response_lower: str, slug: str) -> bool:
    """Return True if ``slug`` is proposed (not merely negated) in ``response_lower``."""

    start = 0
    while True:
        idx = response_lower.find(slug, start)
        if idx == -1:
            return False
        window_start = max(0, idx - 80)
        window_end = min(len(response_lower), idx + len(slug) + 80)
        context_window = response_lower[window_start:window_end]
        if not any(marker in context_window for marker in NEGATION_MARKERS):
            return True
        start = idx + len(slug)


# ---------------------------------------------------------------------------
# Task data model
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Turn:
    """One scripted transcript turn.

    ``content`` is the text written into the transcript / store. ``is_key``
    marks turns carrying the approved-path or dead-end facts (planted early).
    ``src`` and ``base_trust`` mirror real NCP usage where chunk provenance
    varies by writer (planner vs. executor vs. noisy background agent).
    """

    content: str
    src: str = "agent_inferred"
    base_trust: float = 0.5
    relevance: float = 0.2
    layer: str = "episodic"
    conditions: list[str] = field(default_factory=list)
    is_key: bool = False


@dataclass(slots=True)
class Task:
    """One deterministic task-success scenario."""

    task_id: str
    domain: str
    approved_path: str
    dead_ends: list[str]
    turns: list[Turn]
    question: str
    query_text: str


def _filler_turn(index: int, domain: str) -> Turn:
    content = (
        f"turn {index:02d}: {domain} pipeline status update — "
        "agents are validating configuration drift, checking retry budgets, "
        "confirming queue depths, and reviewing routine telemetry; "
        "no new constraints issued this turn; proceeding to next stage"
    )
    return Turn(
        content=content,
        src="agent_inferred",
        base_trust=0.4,
        relevance=0.05,
        layer="episodic",
    )


def _approved_turn(domain: str, approved: str, note: str) -> Turn:
    return Turn(
        content=(
            f"decision: {approved} is the approved {domain} path — {note}"
        ),
        src="user_verified",
        base_trust=0.97,
        relevance=0.95,
        layer="semantic",
        conditions=["approved_path"],
        is_key=True,
    )


def _dead_end_turn(domain: str, dead_end: str, reason: str) -> Turn:
    return Turn(
        content=(
            f"dead_end: {dead_end} rejected for {domain} — {reason}"
        ),
        src="user_verified",
        base_trust=0.9,
        relevance=0.8,
        layer="semantic",
        conditions=["dead_end"],
        is_key=True,
    )


def _build_task(
    task_id: str,
    domain: str,
    approved: str,
    dead_ends: list[tuple[str, str]],
    approved_note: str,
    n_filler: int,
    question_template: str,
) -> Task:
    """Assemble a task: key facts first, then filler turns, then the question."""

    turns: list[Turn] = [_approved_turn(domain, approved, approved_note)]
    for dead_end, reason in dead_ends:
        turns.append(_dead_end_turn(domain, dead_end, reason))
    for i in range(1, n_filler + 1):
        turns.append(_filler_turn(i, domain))

    question = question_template.format(domain=domain)
    query_text = f"{domain} approved path constraint dead ends decision"
    return Task(
        task_id=task_id,
        domain=domain,
        approved_path=approved,
        dead_ends=[d for d, _ in dead_ends],
        turns=turns,
        question=question,
        query_text=query_text,
    )


# ---------------------------------------------------------------------------
# Task definitions (12 tasks, fresh fictional slugs, never reused real names)
# ---------------------------------------------------------------------------

_QUESTION_TEMPLATE = (
    "IMPORTANT: Respond with plain text only. Do NOT read any files or use any tools.\n\n"
    "You are an executor agent finishing a {domain} task. The planner specified "
    "exactly which path is approved and which paths were rejected. Based ONLY on "
    "the context above, state the exact approved path you will use and confirm "
    "you will not use any rejected paths. Your answer MUST name the exact "
    "approved path from the context."
)


def _all_tasks() -> list[Task]:
    specs: list[tuple[str, str, str, list[tuple[str, str]], str, int]] = [
        (
            "task_01_relay",
            "qorvex_relay_v3_delta",
            [
                ("qorvex_relay_legacy_bus", "decommissioned after the 2026-04 outage"),
                ("qorvex_relay_v2_mesh", "incompatible with the new auth substrate"),
                ("qorvex_relay_edge_alpha", "flagged unstable in canary tests"),
            ],
            "verified by the platform team on 2026-05-30",
            12,
        ),
        (
            "task_02_pipeline",
            "brindel_stream_pipeline_omega",
            [
                ("brindel_stream_classic_v1", "removed from the allowed integration list"),
                ("brindel_stream_hybrid_beta", "rejected due to checksum mismatches"),
            ],
            "confirmed by the data team after the 2026-05-22 review",
            13,
        ),
        (
            "task_03_auth",
            "fenrith_auth_gateway_x9",
            [
                ("fenrith_auth_gateway_v1", "deprecated token format"),
                ("fenrith_auth_passthrough_lite", "forbidden by the security policy"),
                ("fenrith_auth_oauth_legacy", "decommissioned 2026-03-15"),
            ],
            "approved after the security audit on 2026-05-18",
            12,
        ),
        (
            "task_04_storage",
            "vantrel_object_store_prime",
            [
                ("vantrel_object_store_archive", "incompatible with the new retention policy"),
                ("vantrel_object_store_cold_v2", "rejected for high latency"),
            ],
            "selected by the infra team on 2026-05-25",
            13,
        ),
        (
            "task_05_messaging",
            "halcyon_message_bus_quanta",
            [
                ("halcyon_message_bus_classic", "decommissioned and unsupported"),
                ("halcyon_message_bus_lite", "rejected due to ordering guarantees"),
                ("halcyon_message_bus_v2_beta", "flagged unstable after incident review"),
            ],
            "confirmed by the messaging working group on 2026-05-20",
            12,
        ),
        (
            "task_06_search",
            "ombrelle_search_index_nova",
            [
                ("ombrelle_search_index_legacy", "removed from the allowed index list"),
                ("ombrelle_search_index_beta_v2", "rejected for relevance regressions"),
            ],
            "verified by the search team on 2026-05-29",
            13,
        ),
        (
            "task_07_billing",
            "trasko_billing_ledger_zen",
            [
                ("trasko_billing_ledger_v1", "deprecated schema"),
                ("trasko_billing_ledger_shadow", "forbidden — shadow ledger disallowed by finance"),
                ("trasko_billing_ledger_beta", "rejected after reconciliation failures"),
            ],
            "approved by finance engineering on 2026-05-27",
            12,
        ),
        (
            "task_08_deploy",
            "pylven_deploy_channel_crest",
            [
                ("pylven_deploy_channel_legacy", "decommissioned 2026-04-30"),
                ("pylven_deploy_channel_canary_v2", "rejected for flaky rollbacks"),
            ],
            "confirmed by the release team on 2026-05-31",
            13,
        ),
        (
            "task_09_cache",
            "drumvale_cache_layer_solace",
            [
                ("drumvale_cache_layer_legacy", "removed from the allowed cache list"),
                ("drumvale_cache_layer_edge_beta", "rejected for inconsistent invalidation"),
                ("drumvale_cache_layer_v2_shadow", "forbidden by the data integrity policy"),
            ],
            "verified by the platform team on 2026-05-24",
            12,
        ),
        (
            "task_10_notification",
            "wexbarrow_notify_channel_ember",
            [
                ("wexbarrow_notify_channel_legacy", "decommissioned and unsupported"),
                ("wexbarrow_notify_channel_v2_beta", "rejected for delivery failures"),
            ],
            "approved by the notifications team on 2026-05-26",
            13,
        ),
        (
            "task_11_analytics",
            "ironcall_analytics_pipeline_lucid",
            [
                ("ironcall_analytics_pipeline_v1", "deprecated aggregation logic"),
                ("ironcall_analytics_pipeline_shadow", "forbidden — shadow pipeline disallowed"),
                ("ironcall_analytics_pipeline_beta", "rejected for data drift issues"),
            ],
            "confirmed by the analytics team on 2026-05-28",
            12,
        ),
        (
            "task_12_scheduler",
            "morrowind_task_scheduler_apex",
            [
                ("morrowind_task_scheduler_legacy", "removed from the allowed scheduler list"),
                ("morrowind_task_scheduler_v2_beta", "rejected for missed cron windows"),
            ],
            "verified by the platform team on 2026-05-23",
            13,
        ),
    ]

    tasks: list[Task] = []
    for task_id, approved, dead_ends, note, n_filler in specs:
        domain = task_id.split("_", 2)[2]
        tasks.append(
            _build_task(
                task_id=task_id,
                domain=domain,
                approved=approved,
                dead_ends=dead_ends,
                approved_note=note,
                n_filler=n_filler,
                question_template=_QUESTION_TEMPLATE,
            )
        )
    return tasks


TASKS: list[Task] = _all_tasks()


def get_tasks(limit: int | None = None) -> list[Task]:
    """Return the task list, optionally truncated to the first ``limit`` tasks."""

    if limit is None:
        return list(TASKS)
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return list(TASKS[:limit])


def score_response(response: str, task: Task) -> tuple[bool, str | None]:
    """Score a response against ``task``.

    Success: response mentions ``task.approved_path`` AND does not propose
    any of ``task.dead_ends`` outside a negation context.
    """

    lower = response.lower()
    if task.approved_path not in lower:
        return False, "missing_approved_path"
    for dead_end in task.dead_ends:
        if mentions_dead_end_as_retry(lower, dead_end):
            return False, f"retried_dead_end:{dead_end}"
    return True, None
