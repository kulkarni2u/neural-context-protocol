"""Library API for the task ledger (ncp.api) and the owner check on api.emit."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from ncp import api
from ncp.config import NCPConfig, load_config
from ncp.mcp.server import _handle_request, make_handlers
from ncp.stores.sqlite import SQLiteStore
from ncp.types import Whisper

PIPE = "pipe_task_api"


def _config(tmp_path: Path, *, tasks: bool = True) -> NCPConfig:
    config = load_config(cwd=tmp_path)
    config.values["tasks"]["enabled"] = tasks
    return config


@pytest.fixture
def ctx(tmp_path: Path) -> dict[str, Any]:
    return {"store": SQLiteStore(tmp_path / "api.db"), "config": _config(tmp_path)}


def _forged(task_id: str, sender: str, kind: str = "result") -> Whisper:
    payload: object
    if kind == "result":
        payload = {"status": "succeeded", "summary": "forged"}
    else:
        payload = {"status": "working"}
    return Whisper(
        from_agent=sender,
        target="router",
        whisper_type=kind,  # type: ignore[arg-type]
        payload=payload,
        confidence=0.9,
        pipeline_id=PIPE,
        task_id=task_id,
    )


class TestLifecycle:
    def test_full_lifecycle_on_sqlite(self, ctx: dict[str, Any]) -> None:
        created = api.create_task(
            agent_id="router", pipeline_id=PIPE, slot="review", title="review diff",
            assign_to="worker", **ctx,
        )
        task_id = created["task"]["task_id"]
        origin = created["task"]["origin_whisper_id"]
        assert created["created"] is True and created["assigned"] is True
        assert origin == created["whisper_id"]
        [request] = ctx["store"].drain_whispers(agent_id="worker", pipeline_id=PIPE)
        assert request.whisper_type == "request" and request.task_id == task_id

        claimed = api.claim_task(agent_id="worker", task_id=task_id, **ctx)
        assert claimed["claimed"] is True and claimed["lease_token"]
        token = claimed["lease_token"]
        assert "lease_token" not in claimed["task"]

        beat = api.heartbeat_task(
            task_id=task_id, lease_token=token,
            progress={"status": "working", "pct": 50, "note": "halfway"}, **ctx,
        )
        assert beat["ok"] is True and "lease_token" not in json.dumps(beat)
        [progress] = ctx["store"].drain_whispers(agent_id="router", pipeline_id=PIPE)
        assert progress.whisper_type == "progress" and progress.whisper_id == beat["whisper_id"]

        done = api.complete_task(
            task_id=task_id, lease_token=token, summary="all green",
            result_chunk_ids=["chk_a"], turn_id="turn_1", **ctx,
        )
        assert done["ok"] is True and done["task"]["status"] == "done"
        assert done["result_turn_id"] == "turn_1"
        [result] = ctx["store"].drain_whispers(agent_id="router", pipeline_id=PIPE)
        assert result.whisper_type == "result" and result.whisper_id == done["whisper_id"]
        assert result.task_id == task_id and result.in_reply_to == origin
        assert result.from_agent == "worker"
        assert json.loads(result.payload)["summary"] == "all green"

        assert api.get_task(task_id=task_id, **ctx)["task"]["status"] == "done"
        listed = api.list_tasks(pipeline_id=PIPE, status="done", **ctx)
        assert listed["count"] == 1 and listed["tasks"][0]["task_id"] == task_id

    def test_release_and_cancel(self, ctx: dict[str, Any]) -> None:
        task_id = api.create_task(
            agent_id="router", pipeline_id=PIPE, slot="s", title="t", **ctx
        )["task"]["task_id"]
        token = api.claim_task(agent_id="worker", task_id=task_id, **ctx)["lease_token"]
        released = api.release_task(task_id=task_id, lease_token=token, **ctx)
        assert released["task"]["status"] == "open"
        assert api.cancel_task(agent_id="router", task_id=task_id, **ctx)["task"]["status"] == "cancelled"
        stale = api.heartbeat_task(task_id=task_id, lease_token=token, **ctx)
        assert stale["ok"] is False


class TestOwnerCheckOnEmit:
    def _claimed(self, ctx: dict[str, Any]) -> str:
        task_id = api.create_task(
            agent_id="router", pipeline_id=PIPE, slot="s", title="t", **ctx
        )["task"]["task_id"]
        api.claim_task(agent_id="worker", task_id=task_id, **ctx)
        return task_id

    @pytest.mark.parametrize("kind", ["result", "progress"])
    def test_forged_whisper_from_non_owner_rejected(self, ctx: dict[str, Any], kind: str) -> None:
        task_id = self._claimed(ctx)
        with pytest.raises(ValueError, match="only the current owner of a claimed task"):
            api.emit(_forged(task_id, "mallory", kind), **ctx)
        assert ctx["store"].drain_whispers(agent_id="router", pipeline_id=PIPE) == []

    def test_owner_whisper_accepted(self, ctx: dict[str, Any]) -> None:
        task_id = self._claimed(ctx)
        whisper = _forged(task_id, "worker")
        whisper.in_reply_to = None
        wid = api.emit(whisper, **ctx)
        [got] = ctx["store"].drain_whispers(agent_id="router", pipeline_id=PIPE)
        assert got.whisper_id == wid and got.task_id == task_id

    def test_unknown_task_and_cross_pipeline_rejected(self, ctx: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="unknown task_id"):
            api.emit(_forged("tsk_nope", "worker"), **ctx)
        task_id = self._claimed(ctx)
        other = _forged(task_id, "worker")
        other.pipeline_id = "other_pipe"
        with pytest.raises(ValueError, match="different pipeline"):
            api.emit(other, **ctx)

    def test_other_types_may_carry_task_id(self, ctx: dict[str, Any]) -> None:
        task_id = self._claimed(ctx)
        note = Whisper(
            from_agent="anyone", target="router", whisper_type="alert", payload={"alert_code": "x", "description": "fyi"},
            confidence=0.9, pipeline_id=PIPE, task_id=task_id,
        )
        assert api.emit(note, **ctx) == note.whisper_id

    def test_disabled_ledger_rejects_task_linked_result(self, tmp_path: Path) -> None:
        ctx = {"store": SQLiteStore(tmp_path / "off.db"), "config": _config(tmp_path, tasks=False)}
        with pytest.raises(ValueError, match="task ledger is disabled"):
            api.emit(_forged("tsk_x", "worker"), **ctx)


class TestDisabled:
    def test_every_task_function_raises_when_disabled(self, tmp_path: Path) -> None:
        ctx = {"store": SQLiteStore(tmp_path / "off.db"), "config": _config(tmp_path, tasks=False)}
        calls = [
            lambda: api.create_task(agent_id="a", pipeline_id=PIPE, slot="s", title="t", **ctx),
            lambda: api.claim_task(agent_id="a", task_id="tsk_x", **ctx),
            lambda: api.heartbeat_task(task_id="tsk_x", lease_token="x", **ctx),
            lambda: api.release_task(task_id="tsk_x", lease_token="x", **ctx),
            lambda: api.complete_task(task_id="tsk_x", lease_token="x", summary="s", **ctx),
            lambda: api.cancel_task(agent_id="a", task_id="tsk_x", **ctx),
            lambda: api.get_task(task_id="tsk_x", **ctx),
            lambda: api.list_tasks(pipeline_id=PIPE, **ctx),
        ]
        for call in calls:
            with pytest.raises(ValueError, match=r"\[tasks\]\.enabled = true"):
                call()


class TestDoorbell:
    def test_complete_rings_process_notifier(self, ctx: dict[str, Any]) -> None:
        task_id = api.create_task(
            agent_id="router", pipeline_id=PIPE, slot="s", title="t", assign_to="worker", **ctx
        )["task"]["task_id"]
        token = api.claim_task(agent_id="worker", task_id=task_id, **ctx)["lease_token"]
        with api.subscribe(agent_id="router", pipeline_id=PIPE, config=ctx["config"]) as sub:
            api.complete_task(task_id=task_id, lease_token=token, summary="done", **ctx)
            events = sub.poll(1.0)
        assert [(e.type, e.task_id) for e in events] == [("result", task_id)]

    def test_expiry_into_failure_rings_creator(self, ctx: dict[str, Any]) -> None:
        task_id = api.create_task(
            agent_id="router", pipeline_id=PIPE, slot="s", title="t", max_attempts=1, **ctx
        )["task"]["task_id"]
        api.claim_task(agent_id="worker", task_id=task_id, lease_seconds=1, **ctx)
        with api.subscribe(agent_id="router", pipeline_id=PIPE, config=ctx["config"]) as sub:
            time.sleep(1.2)
            got = api.get_task(task_id=task_id, **ctx)
            events = sub.poll(1.0)
        assert got["task"]["status"] == "failed"
        assert [(e.event, e.task_id, e.status) for e in events] == [("task", task_id, "failed")]


_VOLATILE = {"created_at", "updated_at", "lease_expires_at", "claimed_at", "heartbeat_at", "finished_at"}


def _normalize(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in _VOLATILE:
                out[key] = "<ts>" if item is not None else None
            elif key.endswith("_id") or key == "lease_token":
                out[key] = ids.setdefault(str(item), f"<{key}{len(ids)}>") if item is not None else None
            else:
                out[key] = _normalize(item, ids)
        return out
    if isinstance(value, list):
        return [_normalize(v, ids) for v in value]
    return value


class TestParityWithTool:
    def test_same_actions_yield_equal_results(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        tool_store = SQLiteStore(tmp_path / "tool.db")
        api_store = SQLiteStore(tmp_path / "api2.db")
        handlers = make_handlers(tool_store, config=config)
        kw = {"store": api_store, "config": config}

        def tool(**args: Any) -> dict[str, Any]:
            request = {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "ncp_task", "arguments": args},
            }
            response = json.loads(_handle_request(request, handlers))
            return json.loads(response["result"]["content"][0]["text"])

        t_ids: dict[str, str] = {}
        a_ids: dict[str, str] = {}

        def same(tool_result: dict[str, Any], api_result: dict[str, Any]) -> None:
            assert _normalize(tool_result, t_ids) == _normalize(api_result, a_ids)

        t_created = tool(action="create", agent_id="router", pipeline_id=PIPE, slot="s",
                         title="t", assign_to="worker", dedupe_key="k1")
        a_created = api.create_task(agent_id="router", pipeline_id=PIPE, slot="s", title="t",
                                    assign_to="worker", dedupe_key="k1", **kw)
        same(t_created, a_created)

        t_claim = tool(action="claim", agent_id="worker", task_id=t_created["task"]["task_id"])
        a_claim = api.claim_task(agent_id="worker", task_id=a_created["task"]["task_id"], **kw)
        assert "lease_token" in t_claim and "lease_token" in a_claim
        same(t_claim, a_claim)

        progress = {"status": "working", "pct": 10}
        same(
            tool(action="heartbeat", task_id=t_created["task"]["task_id"],
                 lease_token=t_claim["lease_token"], progress=progress),
            api.heartbeat_task(task_id=a_created["task"]["task_id"],
                               lease_token=a_claim["lease_token"], progress=progress, **kw),
        )
        same(tool(action="get", task_id=t_created["task"]["task_id"]),
             api.get_task(task_id=a_created["task"]["task_id"], **kw))
        same(tool(action="list", pipeline_id=PIPE), api.list_tasks(pipeline_id=PIPE, **kw))
        same(
            tool(action="complete", task_id=t_created["task"]["task_id"],
                 lease_token=t_claim["lease_token"], summary="ok", turn_id="turn_9"),
            api.complete_task(task_id=a_created["task"]["task_id"],
                              lease_token=a_claim["lease_token"], summary="ok", turn_id="turn_9", **kw),
        )
        # Stale token after completion: same failure shape.
        same(
            tool(action="release", task_id=t_created["task"]["task_id"], lease_token=t_claim["lease_token"]),
            api.release_task(task_id=a_created["task"]["task_id"], lease_token=a_claim["lease_token"], **kw),
        )
