"""CliRunner tests for the operator retention commands (`ncp vacuum`, `ncp forget`)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

import ncp.cli as cli_module
from ncp.cli import main
from ncp.config import load_config
from ncp.stores.base import BaseStore
from ncp.stores.factory import create_store

OLD = time.time() - 40 * 86400


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NCP_AUTH_TOKEN", "NCP_HOST", "NCP_PORT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    result = CliRunner().invoke(main, ["init", "--cwd", str(tmp_path), "--store", "sqlite"])
    assert result.exit_code == 0, result.output
    return tmp_path


def _store(project: Path):
    return create_store(load_config(cwd=project))


def _seed_old_cost_rows(project: Path) -> None:
    store = _store(project)
    now = time.time()
    with sqlite3.connect(store.path) as conn:
        conn.executemany(
            "INSERT INTO cost_log (turn_id, pipeline_id, agent_id, model, input_tokens, output_tokens, cost_usd, logged_at)"
            " VALUES (?, 'pipe', 'a', 'm', 1, 1, 0.1, ?)",
            [("t_old", OLD), ("t_new", now)],
        )


def _cost_rows(project: Path) -> int:
    with sqlite3.connect(_store(project).path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM cost_log").fetchone()[0])


def test_vacuum_requires_older_than(project: Path) -> None:
    result = CliRunner().invoke(main, ["vacuum", "--cwd", str(project)])
    assert result.exit_code != 0
    assert "--older-than" in result.output


def test_vacuum_rejects_bad_duration(project: Path) -> None:
    result = CliRunner().invoke(main, ["vacuum", "--cwd", str(project), "--older-than", "soon"])
    assert result.exit_code != 0
    assert "expected seconds" in result.output


def test_vacuum_without_yes_is_a_dry_run_when_not_a_tty(project: Path) -> None:
    _seed_old_cost_rows(project)
    result = CliRunner().invoke(main, ["vacuum", "--cwd", str(project), "--older-than", "30d"])
    assert result.exit_code == 0, result.output
    assert "cost_log" in result.output
    assert "nothing deleted" in result.output
    assert _cost_rows(project) == 2


def test_vacuum_dry_run_flag_wins_over_yes(project: Path) -> None:
    _seed_old_cost_rows(project)
    result = CliRunner().invoke(
        main, ["vacuum", "--cwd", str(project), "--older-than", "30d", "--dry-run", "--yes", "--json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["mode"] == "dry-run" and data["counts"]["cost_log"] == 1
    assert _cost_rows(project) == 2


def test_vacuum_yes_deletes_and_reports_json(project: Path) -> None:
    _seed_old_cost_rows(project)
    result = CliRunner().invoke(main, ["vacuum", "--cwd", str(project), "--older-than", "30d", "--yes", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["mode"] == "applied"
    assert data["older_than_seconds"] == 30 * 86400
    assert data["total"] == sum(data["counts"].values())
    assert _cost_rows(project) == 1
    again = CliRunner().invoke(main, ["vacuum", "--cwd", str(project), "--older-than", "30d", "--yes", "--json-output"])
    assert json.loads(again.output)["counts"]["cost_log"] == 0


def test_vacuum_prompts_on_a_tty(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_old_cost_rows(project)
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    declined = CliRunner().invoke(main, ["vacuum", "--cwd", str(project), "--older-than", "30d"], input="n\n")
    assert declined.exit_code == 0, declined.output
    assert _cost_rows(project) == 2
    accepted = CliRunner().invoke(main, ["vacuum", "--cwd", str(project), "--older-than", "30d"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert "Deleted" in accepted.output
    assert _cost_rows(project) == 1


def test_vacuum_unsupported_backend_is_a_clean_error(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class NoVacuumStore(BaseStore):  # inherits the NotImplementedError default
        def __init__(self) -> None:  # pragma: no cover - abstract methods are never called
            pass

    NoVacuumStore.__abstractmethods__ = frozenset()
    monkeypatch.setattr(cli_module, "create_store", lambda config: NoVacuumStore())
    result = CliRunner().invoke(main, ["vacuum", "--cwd", str(project), "--older-than", "1d", "--yes"])
    assert result.exit_code != 0
    assert "vacuum is not implemented for NoVacuumStore" in result.output
    assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# ncp forget
# ---------------------------------------------------------------------------


def _write(project: Path, chunk_id: str, *, pipeline_id: str = "pipe", written_by: str = "alice", **kwargs) -> None:
    from ncp.types import SubconsciousChunk

    assert _store(project).write(
        SubconsciousChunk(
            chunk_id=chunk_id,
            layer="semantic",
            content=f"note {chunk_id} about topic {chunk_id}",
            src="tool_result",
            written_by=written_by,
            pipeline_id=pipeline_id,
            **kwargs,
        ),
        allow_duplicate=True,
    )


def _live_chunks(project: Path) -> set[str]:
    with sqlite3.connect(_store(project).path) as conn:
        return {r[0] for r in conn.execute("SELECT chunk_id FROM chunks")}


def test_forget_requires_a_selector(project: Path) -> None:
    result = CliRunner().invoke(main, ["forget", "--cwd", str(project), "--yes"])
    assert result.exit_code != 0
    assert "at least one of --chunk, --agent or --pipeline" in result.output


def test_forget_dry_run_lists_candidates_and_deletes_nothing(project: Path) -> None:
    _write(project, "c1")
    _write(project, "c2")
    result = CliRunner().invoke(main, ["forget", "--cwd", str(project), "--chunk", "c1", "--dry-run", "--yes"])
    assert result.exit_code == 0, result.output
    assert "c1" in result.output and "c2" not in result.output
    assert "nothing deleted" in result.output
    assert _live_chunks(project) == {"c1", "c2"}


def test_forget_without_yes_is_a_dry_run_when_not_a_tty(project: Path) -> None:
    _write(project, "c1")
    result = CliRunner().invoke(main, ["forget", "--cwd", str(project), "--chunk", "c1"])
    assert result.exit_code == 0, result.output
    assert _live_chunks(project) == {"c1"}


def test_forget_yes_deletes_and_reports_json(project: Path) -> None:
    _write(project, "c1")
    _write(project, "c2", pipeline_id="other")
    result = CliRunner().invoke(
        main, ["forget", "--cwd", str(project), "--chunk", "c1", "--chunk", "c2", "--pipeline", "pipe", "--yes", "--json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["mode"] == "applied"
    assert [c["chunk_id"] for c in data["candidates"]] == ["c1"]
    assert data["counts"]["chunks"] == 1 and data["counts"]["tombstones"] == 1
    assert data["selectors"]["pipeline_id"] == "pipe"
    assert _live_chunks(project) == {"c2"}


def test_forget_by_agent_and_pipeline_spares_global(project: Path) -> None:
    _write(project, "mine")
    _write(project, "shared", scope="global")
    _write(project, "bobs", written_by="bob")
    result = CliRunner().invoke(
        main, ["forget", "--cwd", str(project), "--agent", "alice", "--pipeline", "pipe", "--yes", "--json-output"]
    )
    assert result.exit_code == 0, result.output
    assert _live_chunks(project) == {"shared", "bobs"}


def test_forget_prompts_on_a_tty(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(project, "c1")
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    args = ["forget", "--cwd", str(project), "--chunk", "c1"]
    assert CliRunner().invoke(main, args, input="n\n").exit_code == 0
    assert _live_chunks(project) == {"c1"}
    accepted = CliRunner().invoke(main, args, input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert "Forgot 1 chunk" in accepted.output
    assert _live_chunks(project) == set()


def test_forget_no_match_is_not_an_error(project: Path) -> None:
    result = CliRunner().invoke(main, ["forget", "--cwd", str(project), "--chunk", "nope", "--yes"])
    assert result.exit_code == 0, result.output
    assert "No chunks match" in result.output


def test_forget_unsupported_backend_is_a_clean_error(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class NoForgetStore(BaseStore):
        def __init__(self) -> None:  # pragma: no cover
            pass

    NoForgetStore.__abstractmethods__ = frozenset()
    monkeypatch.setattr(cli_module, "create_store", lambda config: NoForgetStore())
    result = CliRunner().invoke(main, ["forget", "--cwd", str(project), "--chunk", "x", "--yes"])
    assert result.exit_code != 0
    assert "forget is not implemented for NoForgetStore" in result.output
    assert "Traceback" not in result.output
