"""Improvement block: render problem -> attempt history for an agent (v1.9.7).

The block is never truncated: an oversize block is reported in telemetry so
the caller can warn, because silently dropping history defeats its purpose.
"""

from __future__ import annotations

from typing import Sequence

from ncp.stores.base import BaseStore
from ncp.types import ImprovementAttempt, ImprovementProblem

ProblemView = tuple[ImprovementProblem, list[ImprovementAttempt]]


def build_improvement_view(
    store: BaseStore, *, pipeline_id: str | None = None, scope: str | None = None
) -> list[ProblemView]:
    """Problems (by ``opened_at``) with their attempts (by ``created_at``)."""
    problems = sorted(
        store.list_improvement_problems(pipeline_id=pipeline_id, scope=scope),
        key=lambda p: (p.opened_at, p.problem_id),
    )
    view: list[ProblemView] = []
    for problem in problems:
        attempts = sorted(
            store.list_improvement_attempts(problem_id=problem.problem_id),
            key=lambda a: (a.created_at, a.attempt_id),
        )
        view.append((problem, attempts))
    return view


def _num(value: float) -> str:
    return f"{value:g}"


def _attempt_line(a: ImprovementAttempt, current_model: str | None) -> str:
    parts = [f"  - {a.status} {a.kind} {a.what}"]
    if a.rationale:
        parts.append(f"why:{a.rationale}")
    obs: list[str] = []
    if a.predicted:
        obs.append("predicted:" + ",".join(a.predicted))
    if a.fixed:
        obs.append("fixed:" + ",".join(a.fixed))
    if a.missed:
        obs.append("missed:" + ",".join(a.missed))
    if obs:
        parts.append(" ".join(obs))
    stats: list[str] = []
    if a.passed is not None and a.total is not None:
        stats.append(f"pass:{_num(a.passed)}/{_num(a.total)}")
    if a.mean_output_tokens is not None:
        stats.append(f"out_tok:{int(round(a.mean_output_tokens))}")
    if stats:
        parts.append(" ".join(stats))
    ctx: list[str] = []
    if a.model:
        ctx.append(f"model={a.model}")
    if a.benchmark:
        ctx.append(f"bench={a.benchmark}")
    if ctx:
        text = "ctx:" + ",".join(ctx)
        if a.model and current_model and a.model != current_model:
            text += " !model"
        parts.append(text)
    return " | ".join(parts)


def render_improvement_block(
    problems_with_attempts: Sequence[ProblemView],
    *,
    scope: str | None = None,
    current_model: str | None = None,
    warn_chars: int = 8000,
) -> tuple[str, dict]:
    """Render the ``[NCP:IMPROVEMENT]`` block and its telemetry. Never truncates."""
    ordered = sorted(problems_with_attempts, key=lambda pa: (pa[0].opened_at, pa[0].problem_id))
    n_attempts = sum(len(attempts) for _, attempts in ordered)
    lines = [f"[NCP:IMPROVEMENT] scope:{scope or '*'} problems:{len(ordered)} attempts:{n_attempts}"]
    for problem, attempts in ordered:
        lines.append(f"{problem.problem_id} {problem.name}")
        for a in sorted(attempts, key=lambda x: (x.created_at, x.attempt_id)):
            lines.append(_attempt_line(a, current_model))
    text = "\n".join(lines)
    telemetry = {
        "chars": len(text),
        "oversize": len(text) > warn_chars,
        "problems": len(ordered),
        "attempts": n_attempts,
    }
    return text, telemetry
