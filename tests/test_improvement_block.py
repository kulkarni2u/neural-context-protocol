"""[NCP:IMPROVEMENT] block rendering and view (v1.9.7)."""

from __future__ import annotations

from pathlib import Path

from ncp.improvement import build_improvement_view, render_improvement_block
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ImprovementAttempt, ImprovementProblem


def _problem(pid: str, name: str, opened: float, scope: str = "deploy") -> ImprovementProblem:
    return ImprovementProblem(problem_id=pid, scope=scope, name=name, opened_at=opened)


def _attempt(aid: str, pid: str, created: float, **kw: object) -> ImprovementAttempt:
    base: dict[str, object] = {"attempt_id": aid, "problem_id": pid, "kind": "repair", "what": "w", "created_at": created}
    base.update(kw)
    return ImprovementAttempt(**base)  # type: ignore[arg-type]


def test_block_format_exact() -> None:
    p = _problem("prob_a", "retries ignore backoff", 1.0)
    full = _attempt(
        "a1", "prob_a", 1.0, status="applied", what="added backoff", rationale="429s",
        predicted=["t1", "t2"], fixed=["t1"], missed=["t2"], passed=4, total=5,
        mean_output_tokens=80.4, model="m1", benchmark="b1",
    )
    bare = _attempt("a2", "prob_a", 2.0, kind="prune", what="shorter")
    text, tel = render_improvement_block([(p, [bare, full])], scope="deploy", current_model="m1")
    assert text.split("\n") == [
        "[NCP:IMPROVEMENT] scope:deploy problems:1 attempts:2",
        "prob_a retries ignore backoff",
        "  - applied repair added backoff | why:429s | predicted:t1,t2 fixed:t1 missed:t2 "
        "| pass:4/5 out_tok:80 | ctx:model=m1,bench=b1",
        "  - proposed prune shorter",
    ]
    assert tel == {"chars": len(text), "oversize": False, "problems": 1, "attempts": 2}


def test_model_mismatch_flag() -> None:
    p = _problem("prob_a", "n", 1.0)
    same = _attempt("a1", "prob_a", 1.0, model="m1")
    other = _attempt("a2", "prob_a", 2.0, model="m2", benchmark="b")
    unset = _attempt("a3", "prob_a", 3.0)
    text, _ = render_improvement_block([(p, [same, other, unset])], current_model="m1")
    lines = text.split("\n")
    assert lines[2].endswith("ctx:model=m1")
    assert lines[3].endswith("ctx:model=m2,bench=b !model")
    assert "!model" not in lines[4]
    no_current, _ = render_improvement_block([(p, [other])])
    assert "!model" not in no_current
    assert text.startswith("[NCP:IMPROVEMENT] scope:* ")


def test_oversize_is_reported_never_truncated() -> None:
    p = _problem("prob_a", "n", 1.0)
    attempts = [_attempt(f"a{i}", "prob_a", float(i), what="x" * 200) for i in range(10)]
    text, tel = render_improvement_block([(p, attempts)], warn_chars=500)
    assert tel["oversize"] is True and tel["chars"] == len(text) > 500
    assert text.count("\n  - ") == 10 and text.count("x" * 200) == 10
    _, small = render_improvement_block([(p, attempts)], warn_chars=100000)
    assert small["oversize"] is False


def test_empty_block() -> None:
    text, tel = render_improvement_block([])
    assert text == "[NCP:IMPROVEMENT] scope:* problems:0 attempts:0"
    assert tel["problems"] == 0 and tel["attempts"] == 0


def test_ordering_by_opened_and_created() -> None:
    late = _problem("prob_late", "late", 9.0)
    early = _problem("prob_early", "early", 1.0)
    a_late = _attempt("a2", "prob_early", 5.0, what="second")
    a_early = _attempt("a1", "prob_early", 2.0, what="first")
    text, _ = render_improvement_block([(late, []), (early, [a_late, a_early])])
    lines = text.split("\n")
    assert lines[1].startswith("prob_early") and lines[4].startswith("prob_late")
    assert "first" in lines[2] and "second" in lines[3]


def test_build_view_from_store(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    store.upsert_improvement_problem(_problem("prob_b", "second", 2.0))
    store.upsert_improvement_problem(_problem("prob_a", "first", 1.0))
    store.upsert_improvement_problem(_problem("prob_c", "other scope", 3.0, scope="elsewhere"))
    store.record_improvement_attempt(_attempt("a2", "prob_a", 5.0))
    store.record_improvement_attempt(_attempt("a1", "prob_a", 2.0, status="rejected"))
    view = build_improvement_view(store, scope="deploy")
    assert [p.problem_id for p, _ in view] == ["prob_a", "prob_b"]
    assert [a.attempt_id for a in view[0][1]] == ["a1", "a2"]
    assert len(build_improvement_view(store)) == 3
    store.merge_improvement_problems(src_problem_id="prob_b", dst_problem_id="prob_a")
    assert [p.problem_id for p, _ in build_improvement_view(store, scope="deploy")] == ["prob_a"]
