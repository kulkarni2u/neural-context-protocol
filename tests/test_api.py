from pathlib import Path

import ncp
from ncp.adapters.base import BaseAdapter, TokenUsage
from ncp.adapters.local import LocalAdapter
from ncp.config import NCPConfig
from ncp.costs import calculate_cost
from ncp.stores.sqlite import SQLiteStore
from ncp.types import AlertPayload, BudgetContext, SubconsciousChunk, Whisper


class _MeteredMockAdapter(BaseAdapter):
    """Mock provider adapter that reports real token usage like a live SDK."""

    def __init__(self, *, input_tokens: int, output_tokens: int, model: str = "gpt-4o") -> None:
        self._model = model
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    def call(self, ncp_context: str, user_turn: str) -> str:
        self.last_usage = TokenUsage(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )
        return "mock_provider_response"


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
    assert "[NCP:BUDGET]" in context
    assert "t:sensor" not in context
    assert "drift_score_sample" not in context


def _cape1_agent() -> object:
    return ncp.agent(
        id="executor",
        role="build",
        owns=["implementation"],
        must_not=["planning"],
        task="cost_accounting",
        slot="meter_tokens",
        intent="record_real_cost",
    )


def test_run_records_real_provider_tokens_and_priced_cost(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    ncp.configure(cwd=project)
    store = SQLiteStore(project / ".ncp" / "store.db")
    adapter = _MeteredMockAdapter(input_tokens=1234, output_tokens=567, model="gpt-4o")

    response = ncp.run(agent=_cape1_agent(), turn="do the work", adapter=adapter, store=store)

    expected = calculate_cost(
        model="gpt-4o", input_tokens=1234, output_tokens=567
    ).total_cost_usd
    assert response.input_tokens == 1234
    assert response.output_tokens == 567
    assert response.model == "gpt-4o"
    assert response.cost_source == "measured"
    assert response.cost_usd == expected
    assert expected > 0.0
    # The real priced cost is persisted, not the fabricated 0.0.
    assert store.status()["cost_usd_total"] == expected


def test_local_adapter_turn_is_marked_estimated_not_authoritative(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    ncp.configure(cwd=project)
    store = SQLiteStore(project / ".ncp" / "store.db")

    response = ncp.run(
        agent=_cape1_agent(), turn="finish the slice", adapter=LocalAdapter(), store=store
    )

    assert response.cost_source == "estimated"
    assert response.cost_usd == 0.0


def test_rapid_turns_do_not_overwrite_cost_log_rows(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    ncp.configure(cwd=project)
    store = SQLiteStore(project / ".ncp" / "store.db")

    # Force both turns onto the same millisecond so only the uuid suffix differs.
    monkeypatch.setattr("ncp.api.time.time", lambda: 1_700_000_000.0)

    first = ncp.run(
        agent=_cape1_agent(),
        turn="turn one",
        adapter=_MeteredMockAdapter(input_tokens=100, output_tokens=50),
        store=store,
    )
    second = ncp.run(
        agent=_cape1_agent(),
        turn="turn two",
        adapter=_MeteredMockAdapter(input_tokens=200, output_tokens=80),
        store=store,
    )

    assert first.turn_id != second.turn_id
    summary = store.cost_summary(limit=10)
    turn_ids = {entry["turn_id"] for entry in summary["recent_entries"]}
    assert first.turn_id in turn_ids
    assert second.turn_id in turn_ids
    # Both rows survive and the rollup sums them (no INSERT OR REPLACE clobber).
    expected_total = first.cost_usd + second.cost_usd
    assert summary["summary"]["cost_usd_total"] == expected_total
    assert store.status()["cost_usd_total"] == expected_total
