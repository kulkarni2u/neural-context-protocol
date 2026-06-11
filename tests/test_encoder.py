from ncp.encoder import PidginEncoder
from ncp.types import BudgetContext, ConsciousBlock, SubconsciousChunk, Whisper


def test_pidgin_encoder_omits_empty_optional_blocks() -> None:
    encoder = PidginEncoder()
    conscious = ConsciousBlock(
        agent_id="planner",
        role="decompose",
        owns=["planning"],
        must_not=["shipping"],
        task="refactor_auth",
        slot="identify_dead_code",
        intent="reduce_complexity",
    )
    budget = BudgetContext(ctx_used=0.25, steps_completed=1, steps_total=4, elapsed_seconds=12.4)

    rendered = encoder.assemble(conscious=conscious, chunks=[], whispers=[], budget=budget)

    assert rendered == (
        "[NCP:CONSCIOUS]\n"
        "id:planner role:decompose ncp_v:1.0\n"
        "task:refactor_auth\n"
        "slot:identify_dead_code\n"
        "intent:reduce_complexity\n"
        "owns:[planning]\n"
        "must-not:[shipping]\n\n"
        "[NCP:BUDGET] ctx_used:0.2 steps:1/4 elapsed:12s pressure:low"
    )
    assert "[NCP:SUBCONSCIOUS]" not in rendered
    assert "[NCP:WHISPERS]" not in rendered


def test_pidgin_encoder_renders_all_blocks_in_order() -> None:
    encoder = PidginEncoder()
    conscious = ConsciousBlock(
        agent_id="executor",
        role="build",
        owns=["implementation", "tests"],
        must_not=["planning"],
        task="implement_encoder",
        slot="wire_pidgin_blocks",
        intent="assemble_context",
        slot_age=2,
        slot_confidence=0.85,
        goal_version=3,
        recent=["r:sub/turn_a", "r:sub/turn_b"],
        tried=["draft_encoder"],
        failed=["inline_payload"],
        drift_score=0.10,
    )
    chunk = SubconsciousChunk(
        chunk_id="sub_encoder",
        layer="procedural",
        content="line_one\nline_two",
        src="tool_result",
        base_trust=0.9,
        relevance=0.8,
        age_seconds=0.0,
    )
    whisper = Whisper(
        from_agent="critic",
        target="executor",
        whisper_type="nudge",
        payload="verify_golden_fixture",
        confidence=0.75,
        created_at=100.0,
    )
    budget = BudgetContext(
        ctx_used=0.67,
        steps_completed=3,
        steps_total=None,
        elapsed_seconds=18.0,
        pressure="medium",
    )

    rendered = encoder.assemble(
        conscious=conscious,
        chunks=[chunk],
        whispers=[whisper],
        budget=budget,
        now=130.0,
    )

    assert rendered == (
        "[NCP:CONSCIOUS]\n"
        "id:executor role:build ncp_v:1.0\n"
        "task:implement_encoder\n"
        "slot:wire_pidgin_blocks\n"
        "intent:assemble_context\n"
        "owns:[implementation,tests]\n"
        "must-not:[planning]\n"
        "slot_age:2 slot_conf:0.8\n"
        "goal_version:3\n"
        "recent:[r:sub/turn_a | r:sub/turn_b]\n"
        "tried:[draft_encoder]\n"
        "failed:[inline_payload]\n"
        "drift_score:0.1\n\n"
        "[NCP:SUBCONSCIOUS]\n"
        "chunk:sub_encoder layer:procedural score:0.7 src:tool_result trust:0.9\n"
        "  line_one\n"
        "  line_two\n\n"
        "[NCP:WHISPERS]\n"
        "wsp from:critic to:executor t:nudge c:0.8 age:<1m\n"
        "  verify_golden_fixture\n\n"
        "[NCP:BUDGET] ctx_used:0.7 steps:3/? elapsed:18s pressure:medium"
    )
