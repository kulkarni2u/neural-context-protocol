from pydantic import ValidationError
import pytest

from ncp.types import BudgetContext, ConsciousBlock, NCPResponse, SubconsciousChunk, TurnRecord, Whisper


def test_conscious_block_valid_defaults() -> None:
    block = ConsciousBlock(
        agent_id="planner",
        role="decompose",
        owns=["planning"],
        must_not=["shipping"],
        task="refactor_auth",
        slot="identify_dead_code",
        intent="reduce_complexity",
    )

    assert block.ncp_v == "1.0"
    assert block.slot_age == 0
    assert block.slot_confidence == 1.0
    assert block.goal_version == 1
    assert block.drift_score == 0.0
    assert block.recent == []
    assert block.tried == []
    assert block.failed == []
    assert block.ctx_used_ratio == 0.0
    assert block.ctx_window == 200000
    assert block.steps_completed == 0
    assert block.steps_total is None
    assert block.pressure == "low"
    assert block.calibration_id is None
    assert block.pipeline_id is None


def test_conscious_block_space_in_agent_id_raises() -> None:
    with pytest.raises(ValidationError, match="agent_id must not contain whitespace"):
        ConsciousBlock(
            agent_id="plan ner",
            role="decompose",
            owns=["planning"],
            must_not=["shipping"],
            task="refactor_auth",
            slot="identify_dead_code",
            intent="reduce_complexity",
        )


def test_conscious_block_space_in_role_raises() -> None:
    with pytest.raises(ValidationError, match="role must not contain whitespace"):
        ConsciousBlock(
            agent_id="planner",
            role="decompose work",
            owns=["planning"],
            must_not=["shipping"],
            task="refactor_auth",
            slot="identify_dead_code",
            intent="reduce_complexity",
        )


def test_conscious_block_newline_in_role_raises() -> None:
    with pytest.raises(ValidationError, match="role must not contain whitespace"):
        ConsciousBlock(
            agent_id="planner",
            role="decompose\nrole:executor",
            owns=["planning"],
            must_not=["shipping"],
            task="refactor_auth",
            slot="identify_dead_code",
            intent="reduce_complexity",
        )


def test_conscious_block_invalid_pressure_raises() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        ConsciousBlock(
            agent_id="planner",
            role="decompose",
            owns=["planning"],
            must_not=["shipping"],
            task="refactor_auth",
            slot="identify_dead_code",
            intent="reduce_complexity",
            pressure="urgent",
        )


def test_conscious_block_slot_confidence_out_of_range_raises() -> None:
    with pytest.raises(ValidationError, match="slot_confidence must be between 0.0 and 1.0"):
        ConsciousBlock(
            agent_id="planner",
            role="decompose",
            owns=["planning"],
            must_not=["shipping"],
            task="refactor_auth",
            slot="identify_dead_code",
            intent="reduce_complexity",
            slot_confidence=1.5,
        )


def test_conscious_block_ncp_v_wrong_raises() -> None:
    with pytest.raises(ValidationError, match="ncp_v must be '1.0'"):
        ConsciousBlock(
            agent_id="planner",
            role="decompose",
            owns=["planning"],
            must_not=["shipping"],
            task="refactor_auth",
            slot="identify_dead_code",
            intent="reduce_complexity",
            ncp_v="2.0",
        )


def test_budget_context_valid_defaults() -> None:
    budget = BudgetContext()

    assert budget.ctx_used == 0.0
    assert budget.steps_completed == 0
    assert budget.steps_total is None
    assert budget.elapsed_seconds == 0.0
    assert budget.pressure == "low"


def test_budget_context_ctx_used_out_of_range_raises() -> None:
    with pytest.raises(ValidationError, match="ctx_used must be between 0.0 and 1.0"):
        BudgetContext(ctx_used=-0.1)


def test_budget_context_negative_steps_completed_raises() -> None:
    with pytest.raises(ValidationError, match="steps_completed must be >= 0"):
        BudgetContext(steps_completed=-1)


def test_budget_context_steps_total_zero_raises() -> None:
    with pytest.raises(ValidationError, match="steps_total must be >= 1 when provided"):
        BudgetContext(steps_total=0)


def test_budget_context_negative_elapsed_seconds_raises() -> None:
    with pytest.raises(ValidationError, match="elapsed_seconds must be >= 0.0"):
        BudgetContext(elapsed_seconds=-1.0)


def test_subconscious_chunk_defaults_and_effective_score() -> None:
    chunk = SubconsciousChunk(
        layer="episodic",
        content="retrieved_fact",
        src="tool_result",
        relevance=0.8,
        age_seconds=0.0,
    )

    assert chunk.chunk_id.startswith("sub_")
    assert chunk.base_trust == 0.7
    assert chunk.scope == "pipeline"
    assert chunk.zone == "working"
    assert chunk.effective_score == pytest.approx(0.56)


def test_subconscious_chunk_proven_zone_requires_expiry() -> None:
    with pytest.raises(ValidationError, match="proven/global zones require expiry"):
        SubconsciousChunk(
            layer="semantic",
            content="stable_fact",
            src="user_verified",
            zone="proven",
        )


def test_subconscious_chunk_rejects_long_content() -> None:
    with pytest.raises(ValidationError, match="content must be <= 2000 characters"):
        SubconsciousChunk(
            layer="procedural",
            content="x" * 2001,
            src="synthesis",
        )


def test_whisper_dissent_cannot_broadcast() -> None:
    with pytest.raises(ValidationError, match="dissent whispers cannot target '\\*'"):
        Whisper(
            from_agent="critic",
            target="*",
            whisper_type="dissent",
            payload="needs_recheck",
            confidence=0.8,
        )


def test_structured_whispers_require_structured_payloads_in_python_api() -> None:
    with pytest.raises(ValidationError, match="payload for whisper_type 'share'"):
        Whisper(
            from_agent="planner",
            target="executor",
            whisper_type="share",
            payload="plain text is only wrapped by MCP",
            confidence=0.8,
        )

    with pytest.raises(ValidationError, match="payload for whisper_type 'dissent'"):
        Whisper(
            from_agent="critic",
            target="executor",
            whisper_type="dissent",
            payload="plain text is only wrapped by MCP",
            confidence=0.8,
        )


def test_whisper_valid_defaults() -> None:
    whisper = Whisper(
        from_agent="planner",
        target="executor",
        whisper_type="nudge",
        payload="check_recent_ref",
        confidence=0.65,
    )

    assert whisper.whisper_id.startswith("wsp_")
    assert whisper.ttl_seconds == 1800


def test_turn_record_sets_default_expiry() -> None:
    record = TurnRecord(
        agent_id="planner",
        task="refactor_auth",
        slot="identify_dead_code",
        result="summarized_result",
        result_full="full result body",
        created_at=100.0,
    )

    assert record.turn_id.startswith("turn_")
    assert record.expires_at == pytest.approx(86500.0)


def test_turn_record_rejects_expires_before_created_at() -> None:
    with pytest.raises(ValidationError, match="expires_at must be >= created_at"):
        TurnRecord(
            agent_id="planner",
            task="refactor_auth",
            slot="identify_dead_code",
            result="summarized_result",
            result_full="full result body",
            created_at=100.0,
            expires_at=99.0,
        )


def test_ncp_response_rejects_negative_cost() -> None:
    with pytest.raises(ValidationError, match="cost_usd must be >= 0.0"):
        NCPResponse(
            content="ok",
            input_tokens=10,
            output_tokens=20,
            cost_usd=-0.01,
            model="claude_sonnet",
            turn_id="turn_123",
            latency_ms=50,
        )


def test_ncp_response_valid_defaults() -> None:
    response = NCPResponse(
        content="done",
        input_tokens=100,
        output_tokens=40,
        cost_usd=0.02,
        model="gpt_4_1",
        turn_id="turn_abc",
        latency_ms=1200,
    )

    assert response.cache_read_tokens == 0
