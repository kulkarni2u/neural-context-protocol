"""Public building blocks for matched-budget evals against NCP.

These are the pieces ``benchmarks/task_success`` and ``benchmarks/efficacy``
already use internally to compare NCP's relevance-based retrieval against a
recency-only sliding window and an unbounded transcript replay, at the same
token budget. They live in the installed ``ncp`` package — unlike
``benchmarks/``, which ships only with the repo, not with `pip install
neural-context-protocol` — so an external eval harness can build the same
kind of matched-budget comparison without vendoring the benchmark suite.

Typical use::

    from ncp.eval import EvalTurn, matched_budget_conditions

    turns = [EvalTurn(content="...", src="user_verified", base_trust=0.9,
                       relevance=0.9, layer="semantic")]
    conditions = matched_budget_conditions(
        turns, budget=400, query_text="...", pipeline_id="my_eval",
    )
    conditions["ncp"].context          # budget-bounded via Assembler
    conditions["sliding_window"].context
    conditions["raw_replay"].context   # unbounded reference, exempt from budget

Score the resulting responses with ``term_appears_unnegated`` when checking
whether a model proposed something it should have avoided (a retired path, a
forbidden option) rather than merely mentioning it in a negated sentence
("will not use X").

Recording the verdict back onto the bus for outcome-calibrated trust is a
separate step, and deliberately not this module's job: call the
``ncp_record_outcome`` MCP tool (or ``store.record_outcome`` directly) with
the chunk/turn IDs that informed the scored response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tempfile

from ncp.api import agent as make_agent
from ncp.assembler import Assembler
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_tokens
from ncp.types import BudgetContext, SubconsciousChunk

__all__ = [
    "NEGATION_MARKERS",
    "term_appears_unnegated",
    "EvalTurn",
    "ConditionResult",
    "ncp_condition",
    "sliding_window_condition",
    "raw_replay_condition",
    "matched_budget_conditions",
]


# ---------------------------------------------------------------------------
# Negation-aware term scoring
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


def term_appears_unnegated(
    text_lower: str,
    term: str,
    *,
    negation_markers: tuple[str, ...] = NEGATION_MARKERS,
    window_chars: int = 80,
) -> bool:
    """Return True if ``term`` is proposed (not merely negated) in ``text_lower``.

    Scans every occurrence of ``term`` and checks a window of
    ``window_chars`` characters on each side for a negation marker. This is
    the generic form of the check that tells "the model proposed the
    forbidden option" apart from "the model correctly said it would avoid
    it" — the same distinction ``benchmarks/task_success`` and
    ``benchmarks/efficacy`` score dead-end retries with.
    """

    start = 0
    while True:
        idx = text_lower.find(term, start)
        if idx == -1:
            return False
        window_start = max(0, idx - window_chars)
        window_end = min(len(text_lower), idx + len(term) + window_chars)
        context_window = text_lower[window_start:window_end]
        if not any(marker in context_window for marker in negation_markers):
            return True
        start = idx + len(term)


# ---------------------------------------------------------------------------
# Matched-budget condition construction
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EvalTurn:
    """One scripted transcript turn to feed into a matched-budget comparison."""

    content: str
    src: str = "agent_inferred"
    base_trust: float = 0.5
    relevance: float = 0.2
    layer: str = "episodic"
    conditions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConditionResult:
    """One condition's assembled context and its token accounting."""

    condition: str
    context: str
    context_tokens: int
    unbounded: bool


def ncp_condition(
    turns: list[EvalTurn],
    *,
    budget: int,
    query_text: str,
    pipeline_id: str,
    store_path: str | Path | None = None,
    agent_id: str = "eval_executor",
    role: str = "pravaha",
    owns: list[str] | None = None,
    must_not: list[str] | None = None,
    task: str = "eval_question",
    slot: str = "build",
    intent: str = "answer_question",
    ctx_used: float = 0.7,
    pressure: str = "medium",
    k: int = 8,
) -> ConditionResult:
    """Write ``turns`` as chunks and assemble the budget-bounded NCP context.

    Uses a fresh SQLite store at ``store_path`` (a tempdir if omitted) so
    repeated calls don't cross-contaminate. This is the same construction
    ``benchmarks/task_success`` uses for its ``ncp`` condition.
    """

    def _run(path: Path) -> ConditionResult:
        store = SQLiteStore(path)
        for index, turn in enumerate(turns, start=1):
            chunk = SubconsciousChunk(
                layer=turn.layer,  # type: ignore[arg-type]
                src=turn.src,  # type: ignore[arg-type]
                base_trust=turn.base_trust,
                relevance=turn.relevance,
                content=turn.content,
                conditions=list(turn.conditions),
                pipeline_id=pipeline_id,
                written_by=f"eval_turn_{index:02d}",
            )
            store.write(chunk)

        conscious = make_agent(
            id=agent_id,
            role=role,
            owns=owns,
            must_not=must_not,
            task=task,
            slot=slot,
            intent=intent,
            pipeline_id=pipeline_id,
        )
        assembler = Assembler(store=store)
        assembly = assembler.assemble(
            conscious=conscious,
            budget=BudgetContext(ctx_used=ctx_used, pressure=pressure),
            query_text=query_text,
            k=k,
            max_tokens=budget,
        )
        return ConditionResult(
            condition="ncp",
            context=assembly.context,
            context_tokens=estimate_tokens(assembly.context),
            unbounded=False,
        )

    if store_path is not None:
        return _run(Path(store_path))
    with tempfile.TemporaryDirectory(prefix="ncp-eval-") as tmpdir:
        return _run(Path(tmpdir) / "eval.db")


def sliding_window_condition(turns: list[EvalTurn], *, budget: int) -> ConditionResult:
    """Most-recent turns that fit within ``budget`` tokens — recency, no retrieval."""

    selected: list[str] = []
    used = 0
    for turn in reversed(turns):
        tokens = estimate_tokens(turn.content)
        if used + tokens > budget:
            break
        selected.append(turn.content)
        used += tokens
    context = "\n".join(reversed(selected)) if selected else ""
    return ConditionResult(
        condition="sliding_window",
        context=context,
        context_tokens=estimate_tokens(context),
        unbounded=False,
    )


def raw_replay_condition(turns: list[EvalTurn]) -> ConditionResult:
    """Full unbounded transcript — reference condition, exempt from the budget."""

    context = "\n".join(turn.content for turn in turns)
    return ConditionResult(
        condition="raw_replay",
        context=context,
        context_tokens=estimate_tokens(context),
        unbounded=True,
    )


def matched_budget_conditions(
    turns: list[EvalTurn],
    *,
    budget: int,
    query_text: str,
    pipeline_id: str,
    store_path: str | Path | None = None,
    **ncp_kwargs: object,
) -> dict[str, ConditionResult]:
    """Build all three matched-budget conditions: ``ncp``, ``sliding_window``, ``raw_replay``.

    All three see the same ``turns`` and the same nominal ``budget`` B,
    except ``raw_replay`` which is reported but exempt from the budget
    (``unbounded=True``) — it is the "nothing was dropped" reference point.
    Comparing ``ncp`` against ``sliding_window`` at the same B isolates how
    the budget is spent (relevance-based retrieval vs. fixed recency) from
    how much budget is available. Extra keyword arguments are forwarded to
    ``ncp_condition`` (e.g. ``agent_id``, ``owns``, ``must_not``, ``k``).
    """

    return {
        "ncp": ncp_condition(
            turns,
            budget=budget,
            query_text=query_text,
            pipeline_id=pipeline_id,
            store_path=store_path,
            **ncp_kwargs,  # type: ignore[arg-type]
        ),
        "sliding_window": sliding_window_condition(turns, budget=budget),
        "raw_replay": raw_replay_condition(turns),
    }
