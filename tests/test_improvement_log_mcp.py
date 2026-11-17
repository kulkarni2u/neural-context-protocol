"""ncp_improvement_log MCP tool (v1.9.7)."""

from __future__ import annotations

from pathlib import Path

import pytest

import copy

from ncp.config import NCPConfig, load_config
from ncp.mcp.server import MCP_TOOLS, STANDARD_TOOL_NAMES, make_handlers, tools_for_config
from ncp.stores.base import BaseStore
from ncp.stores.sqlite import SQLiteStore


@pytest.fixture()
def handlers(tmp_path: Path):  # type: ignore[no-untyped-def]
    store = SQLiteStore(tmp_path / "s.db")
    return make_handlers(store)["ncp_improvement_log"], store


def _cfg(tmp_path: Path, **sections: object) -> NCPConfig:
    cfg = load_config(cwd=tmp_path)
    values = copy.deepcopy(cfg.values)
    for name, section in sections.items():
        values.setdefault(name, {}).update(section)  # type: ignore[arg-type]
    return NCPConfig(values=values, project_root=cfg.project_root)


def _record(call, **extra):  # type: ignore[no-untyped-def]
    args = {"action": "record_attempt", "scope": "AGENTS.md", "what": "added rule", **extra}
    return call(args)["attempt"]


def test_tool_in_profiles_and_schema_terse(tmp_path: Path) -> None:
    assert "ncp_improvement_log" in STANDARD_TOOL_NAMES
    tool = next(t for t in MCP_TOOLS if t["name"] == "ncp_improvement_log")
    assert tool["inputSchema"]["required"] == ["action"]  # type: ignore[index]
    for profile in ("standard", "full"):
        cfg = _cfg(tmp_path, tools={"profile": profile})
        assert "ncp_improvement_log" in {t["name"] for t in tools_for_config(cfg)}
    core = _cfg(tmp_path, tools={"profile": "core"})
    assert "ncp_improvement_log" not in {t["name"] for t in tools_for_config(core)}


def test_record_attempt_requires_citation(handlers) -> None:  # type: ignore[no-untyped-def]
    call, _ = handlers
    with pytest.raises(ValueError, match="problem_id.*problem_name"):
        call({"action": "record_attempt", "scope": "s", "what": "w"})


def test_record_attempt_validation(handlers) -> None:  # type: ignore[no-untyped-def]
    call, _ = handlers
    with pytest.raises(ValueError, match="what"):
        call({"action": "record_attempt", "problem_name": "p", "scope": "s"})
    with pytest.raises(ValueError, match="scope"):
        call({"action": "record_attempt", "problem_name": "p", "what": "w"})
    with pytest.raises(ValueError, match="unknown improvement problem"):
        call({"action": "record_attempt", "problem_id": "prob_nope", "what": "w"})
    with pytest.raises(ValueError, match="manual"):
        call({"action": "record_attempt", "problem_name": "p", "scope": "s", "what": "w", "kind": "prune"})
    with pytest.raises(ValueError, match="array of strings"):
        call({"action": "record_attempt", "problem_name": "p", "scope": "s", "what": "w", "predicted": "t1"})
    with pytest.raises(ValueError, match="unknown ncp_improvement_log action"):
        call({"action": "nope"})


def test_record_get_list_resolve_merge_roundtrip(handlers) -> None:  # type: ignore[no-untyped-def]
    call, store = handlers
    att = _record(
        call, problem_name="Retries  ignore backoff", pipeline_id="p1", predicted=["t1"],
        files=["AGENTS.md"], model="m1", benchmark="b1", rationale="429s",
    )
    assert att["kind"] == "manual" and att["status"] == "proposed" and att["files"] == ["AGENTS.md"]
    # same normalized name -> same problem
    att2 = _record(call, problem_name="retries ignore BACKOFF", pipeline_id="p1")
    assert att2["problem_id"] == att["problem_id"]
    # cite by id
    att3 = _record(call, problem_id=att["problem_id"])
    assert att3["problem_id"] == att["problem_id"]

    listed = call({"action": "list_problems", "pipeline_id": "p1"})["problems"]
    assert [p["problem_id"] for p in listed] == [att["problem_id"]]
    assert call({"action": "list_problems", "pipeline_id": "other"})["problems"] == []

    resolved = call({
        "action": "resolve_attempt", "attempt_id": att["attempt_id"], "status": "applied",
        "fixed": ["t1"], "missed": [], "passed": 3, "total": 4, "mean_output_tokens": 90, "note": "ok",
    })["attempt"]
    assert resolved["status"] == "applied" and resolved["accepted"] is True
    assert resolved["fixed"] == ["t1"] and resolved["passed"] == 3 and resolved["resolved_at"] is not None

    got = call({"action": "get", "pipeline_id": "p1", "model": "m2"})
    assert got["block"].startswith("[NCP:IMPROVEMENT] scope:* problems:1 attempts:3")
    assert "!model" in got["block"]
    assert got["telemetry"]["attempts"] == 3 and got["telemetry"]["oversize"] is False

    other = _record(call, problem_name="other problem", pipeline_id="p1")
    call({"action": "merge", "src_problem_id": other["problem_id"], "dst_problem_id": att["problem_id"]})
    assert len(call({"action": "list_problems", "pipeline_id": "p1"})["problems"]) == 1
    assert call({"action": "get", "pipeline_id": "p1"})["telemetry"]["attempts"] == 4
    assert isinstance(store, BaseStore)


def test_resolve_and_merge_validation(handlers) -> None:  # type: ignore[no-untyped-def]
    call, _ = handlers
    with pytest.raises(ValueError, match="attempt_id"):
        call({"action": "resolve_attempt", "status": "applied"})
    with pytest.raises(ValueError, match="status"):
        call({"action": "resolve_attempt", "attempt_id": "x", "status": "weird"})
    with pytest.raises(ValueError, match="unknown improvement attempt"):
        call({"action": "resolve_attempt", "attempt_id": "att_missing", "status": "rejected"})
    with pytest.raises(ValueError, match="number"):
        call({"action": "resolve_attempt", "attempt_id": "x", "status": "applied", "passed": -1})
    with pytest.raises(ValueError, match="src_problem_id"):
        call({"action": "merge", "src_problem_id": "a"})


def test_oversize_telemetry_uses_config(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "s.db")
    cfg = _cfg(tmp_path, improvement={"warn_chars": 50})
    call = make_handlers(store, config=cfg)["ncp_improvement_log"]
    _record(call, problem_name="p", what="x" * 200)
    tel = call({"action": "get"})["telemetry"]
    assert tel["oversize"] is True and tel["chars"] > 200
