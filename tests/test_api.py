from pathlib import Path

import ncp
from ncp.adapters.local import LocalAdapter
from ncp.config import NCPConfig
from ncp.stores.sqlite import SQLiteStore
from ncp.types import AlertPayload, BudgetContext, SubconsciousChunk, Whisper


def test_agent_creates_conscious_block_template() -> None:
    block = ncp.agent(
        id="planner",
        role="decompose",
        owns=["planning"],
        must_not=["shipping"],
        task="refactor_auth",
        slot="identify_dead_code",
        intent="reduce_complexity",
    )

    assert block.agent_id == "planner"
    assert block.role == "decompose"


def test_configure_and_get_context_use_store_runtime(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".ncp").mkdir()
    config = ncp.configure(cwd=project)
    store = SQLiteStore(config.store_path)
    store.write(
        SubconsciousChunk(
            chunk_id="sub_api",
            layer="semantic",
            content="api can assemble from persisted refactor_auth memory",
            src="tool_result",
        )
    )
    store.emit_whisper(
        Whisper(
            from_agent="critic",
            target="planner",
            whisper_type="nudge",
            payload="check_context",
            confidence=0.8,
        )
    )

    context = ncp.get_context(
        agent=ncp.agent(
            id="planner",
            role="decompose",
            owns=["planning"],
            must_not=["shipping"],
            task="refactor_auth",
            slot="identify_dead_code",
            intent="reduce_complexity",
        ),
        budget=BudgetContext(),
        store=store,
    )

    assert "[NCP:CONSCIOUS]" in context
    assert "api can assemble from persisted refactor_auth memory" in context
    assert "check_context" in context
    assert isinstance(config, NCPConfig)


def test_write_memory_and_emit_helpers_use_store(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")

    wrote = ncp.write_memory(
        SubconsciousChunk(
            chunk_id="sub_helper",
            layer="episodic",
            content="helper path",
            src="synthesis",
        ),
        store=store,
    )
    ncp.emit(
        Whisper(
            from_agent="planner",
            target="planner",
            whisper_type="alert",
            payload=AlertPayload(alert_code="self_check", description="self_check"),
            confidence=1.0,
        ),
        store=store,
    )

    assert wrote is True
    assert store.query("helper", k=1)[0].chunk_id == "sub_helper"
    assert "self_check" in store.drain_whispers(agent_id="planner")[0].payload


def test_run_and_stream_use_local_adapter(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    ncp.configure(cwd=project)
    store = SQLiteStore(project / ".ncp" / "store.db")
    agent = ncp.agent(
        id="executor",
        role="build",
        owns=["implementation"],
        must_not=["planning"],
        task="implement_cli",
        slot="wire_local_adapter",
        intent="exercise_runtime",
    )

    response = ncp.run(agent=agent, turn="finish the slice", adapter=LocalAdapter(), store=store)
    streamed = "".join(
        ncp.stream(agent=agent, turn="finish the slice", adapter=LocalAdapter(), store=store)
    )
    status = store.status()

    assert "local_adapter_response" in response.content
    assert "local_adapter_response" in streamed
    assert status["turn_record_count"] == 2
    assert status["cost_usd_total"] == 0.0


def test_get_context_degrades_gracefully_on_empty_store(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    config = ncp.configure(cwd=project)
    store = SQLiteStore(config.store_path)

    context = ncp.get_context(
        agent=ncp.agent(
            id="planner",
            role="decompose",
            owns=["planning"],
            must_not=["shipping"],
            task="empty_store_demo",
            slot="bootstrap",
            intent="verify_graceful_empty_state",
        ),
        store=store,
    )

    assert "[NCP:CONSCIOUS]" in context
    assert "[NCP:SUBCONSCIOUS]" in context
    assert "chunk:cold_init" in context
    assert "[NCP:WHISPERS]" not in context
