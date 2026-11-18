"""CLI: refine prune/apply gate/reject and `ncp improvements` (v1.9.7)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.config import load_config
from ncp.stores.factory import create_store
from ncp.types import OutcomeRecord

LONG = (
    "Deploy checklist. Run the unit tests before every deploy. Verify the migration plan with the "
    "database owner. Confirm the rollback command works in staging. Announce the deploy window in "
    "the team channel. Watch the dashboards for ten minutes after the deploy finishes."
)
SHORT = "Deploy checklist. Run tests, verify migrations, confirm rollback, announce, then watch dashboards."


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("NCP_AUTH_TOKEN", "NCP_HOST", "NCP_PORT"):
        monkeypatch.delenv(name, raising=False)
    result = CliRunner().invoke(main, ["init", "--cwd", str(tmp_path), "--store", "sqlite"])
    assert result.exit_code == 0, result.output
    return tmp_path


def _run(project: Path, *args: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(main, [*args, "--cwd", str(project)])


def _outcomes(project: Path, chunk_id: str, prefix: str, ok: int, bad: int, tokens: int) -> None:
    store = create_store(load_config(cwd=project))
    for i in range(ok + bad):
        store.record_outcome(
            OutcomeRecord(outcome_id=f"{prefix}{i}", chunk_ids=[chunk_id], success=i < ok, output_tokens=tokens, model="m1")
        )


def _setup_candidate(project: Path) -> str:
    assert _run(project, "refine", "ingest", "deploy", "--content", LONG).exit_code == 0
    head = create_store(load_config(cwd=project))
    from ncp.refine import find_procedure_head

    head_id = find_procedure_head(head, key="deploy").chunk_id  # type: ignore[union-attr]
    _outcomes(project, head_id, "a", 5, 0, 100)
    (project / "short.txt").write_text(SHORT)
    result = _run(
        project, "refine", "prune", "deploy", "--file", str(project / "short.txt"),
        "--problem", "token cost", "--rationale", "trim", "--predict", "t1", "--model", "m1",
    )
    assert result.exit_code == 0, result.output
    store = create_store(load_config(cwd=project))
    attempts = store.list_improvement_attempts()
    assert len(attempts) == 1 and attempts[0].model == "m1" and attempts[0].predicted == ["t1"]
    cand = attempts[0].candidate_chunk_id
    assert cand
    return cand


def test_prune_refused_without_anchor(project: Path) -> None:
    _run(project, "refine", "ingest", "deploy", "--content", LONG)
    (project / "short.txt").write_text(SHORT)
    result = _run(project, "refine", "prune", "deploy", "--file", str(project / "short.txt"))
    assert result.exit_code != 0 and "no anchor" in result.output


def test_apply_gate_failure_exits_2_with_details_and_force_bypasses(project: Path) -> None:
    cand = _setup_candidate(project)
    _outcomes(project, cand, "c", 2, 3, 60)
    failed = _run(project, "refine", "apply", cand)
    assert failed.exit_code == 2
    assert "Gate failed" in failed.output and "anchor_rate" in failed.output
    forced = _run(project, "refine", "apply", cand, "--force")
    assert forced.exit_code == 0, forced.output
    assert "Adopted" in forced.output


def test_apply_gate_flags_and_pass(project: Path) -> None:
    cand = _setup_candidate(project)
    _outcomes(project, cand, "c", 3, 0, 60)
    assert _run(project, "refine", "apply", cand).exit_code == 2  # 3 < 5 trial outcomes
    ok = _run(project, "refine", "apply", cand, "--min-trial-outcomes", "3", "--gate-ratio", "0.9")
    assert ok.exit_code == 0, ok.output


def test_reject_and_improvements_show_merge(project: Path) -> None:
    cand = _setup_candidate(project)
    rejected = _run(project, "refine", "reject", cand, "--reason", "no good")
    assert rejected.exit_code == 0, rejected.output
    again = _run(project, "refine", "reject", cand)
    assert again.exit_code != 0 and "already rejected" in again.output

    shown = _run(project, "improvements", "show", "--scope", "deploy", "--model", "m2")
    assert shown.exit_code == 0, shown.output
    assert "[NCP:IMPROVEMENT] scope:deploy problems:1 attempts:1" in shown.output
    assert "rejected prune" in shown.output and "!model" in shown.output

    store = create_store(load_config(cwd=project))
    prob = store.list_improvement_problems()[0]
    other = _run(project, "improvements", "merge", prob.problem_id, "prob_missing")
    assert other.exit_code != 0 and "unknown improvement problem" in other.output
    same = _run(project, "improvements", "merge", prob.problem_id, prob.problem_id)
    assert same.exit_code == 0, same.output


def test_propose_accepts_problem_flags(project: Path) -> None:
    _run(project, "refine", "ingest", "rule", "--content", "Always validate input.")
    store = create_store(load_config(cwd=project))
    from ncp.refine import find_procedure_head

    head = find_procedure_head(store, key="rule")
    assert head is not None
    for i in range(3):
        store.record_outcome(
            OutcomeRecord(outcome_id=f"o{i}", chunk_ids=[head.chunk_id], success=False, note="skipped validation")
        )
    result = _run(
        project, "refine", "propose", "rule", "--problem", "validation skipped",
        "--rationale", "r", "--predict", "t1", "--predict", "t2", "--model", "m1",
    )
    assert result.exit_code == 0, result.output
    attempt = create_store(load_config(cwd=project)).list_improvement_attempts()[0]
    assert attempt.kind == "repair" and attempt.predicted == ["t1", "t2"] and attempt.rationale == "r"
