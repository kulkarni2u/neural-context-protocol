"""Tests for decision trace capture and precedent query."""

from pathlib import Path
import json

from click.testing import CliRunner

from ncp.cli import main
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _make_store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / ".ncp" / "store.db")


def _seed_decisions(store: SQLiteStore, pipeline_id: str = "pipe_dt") -> list[str]:
    chunk_ids = []
    d1 = SubconsciousChunk(
        chunk_id="dec_null_guard",
        layer="reasoning_trace",
        content=(
            "decision: apply null guard at PaymentProcessor.java:142\n"
            "rationale: retryCount is null when payment_method=ACH and customer.tier=trial\n"
            "alternatives: optional wrapper | default value at constructor\n"
            "outcome: succeeded\n"
            "evidence: sub_stack_trace sub_test_result\n"
            "tags: null-guard java bugfix"
        ),
        src="agent_inferred",
        written_by="fixer",
        pipeline_id=pipeline_id,
        base_trust=0.8,
        caused_by="sub_stack_trace",
        source_refs=["sub_stack_trace", "sub_test_result"],
    )
    store.write(d1)
    chunk_ids.append(d1.chunk_id)

    d2 = SubconsciousChunk(
        chunk_id="dec_retry_logic",
        layer="reasoning_trace",
        content=(
            "decision: add exponential backoff with max 3 retries\n"
            "rationale: network timeouts cause intermittent failures in payment processing\n"
            "alternatives: fixed delay | circuit breaker | no retry\n"
            "outcome: succeeded\n"
            "tags: retry-logic resilience java"
        ),
        src="agent_inferred",
        written_by="fixer",
        pipeline_id=pipeline_id,
        base_trust=0.9,
    )
    store.write(d2)
    chunk_ids.append(d2.chunk_id)

    d3 = SubconsciousChunk(
        chunk_id="dec_cache_strategy",
        layer="reasoning_trace",
        content=(
            "decision: use LRU cache with 5min TTL for user profiles\n"
            "rationale: reduce database load during peak traffic\n"
            "alternatives: redis cache | no cache | pre-warming\n"
            "outcome: failed\n"
            "tags: caching performance"
        ),
        src="agent_inferred",
        written_by="architect",
        pipeline_id=pipeline_id,
        base_trust=0.5,
    )
    store.write(d3)
    chunk_ids.append(d3.chunk_id)

    return chunk_ids


# ── Store method tests ──────────────────────────────────────────────────


def test_query_precedents_finds_relevant_decisions(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_decisions(store)

    results = store.query_precedents("null guard payment", pipeline_id="pipe_dt")

    assert len(results) >= 1
    decisions = [r["decision"] for r in results]
    assert any("null guard" in d for d in decisions)


def test_query_precedents_parses_decision_fields(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_decisions(store)

    results = store.query_precedents("null guard payment", pipeline_id="pipe_dt")

    null_guard = next((r for r in results if "null guard" in r["decision"]), None)
    assert null_guard is not None
    assert null_guard["outcome"] == "succeeded"
    assert "java" in null_guard["tags"]
    assert null_guard["caused_by"] == "sub_stack_trace"


def test_query_precedents_filter_by_tag(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_decisions(store)

    results = store.query_precedents(
        "java", pipeline_id="pipe_dt", tags=["caching"]
    )

    for r in results:
        assert "caching" in r["tags"]


def test_query_precedents_filter_by_outcome(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_decisions(store)

    results = store.query_precedents(
        "cache strategy", pipeline_id="pipe_dt", outcome="failed"
    )

    for r in results:
        assert r["outcome"] == "failed"


def test_query_precedents_empty_store(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    results = store.query_precedents("anything")

    assert results == []


def test_query_precedents_respects_k_limit(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    _seed_decisions(store)

    results = store.query_precedents("java", pipeline_id="pipe_dt", k=1)

    assert len(results) <= 1


def test_parse_decision_content_roundtrip() -> None:
    content = (
        "decision: apply null guard\n"
        "rationale: field can be null\n"
        "alternatives: wrapper | default\n"
        "outcome: succeeded\n"
        "evidence: ref1 ref2\n"
        "tags: java bugfix"
    )
    parsed = SQLiteStore._parse_decision_content(content)

    assert parsed["decision"] == "apply null guard"
    assert parsed["rationale"] == "field can be null"
    assert parsed["alternatives"] == ["wrapper", "default"]
    assert parsed["outcome"] == "succeeded"
    assert parsed["tags"] == ["java", "bugfix"]


# ── MCP tool tests ──────────────────────────────────────────────────────


def test_mcp_record_decision_writes_reasoning_trace(tmp_path: Path) -> None:
    from ncp.mcp.server import make_handlers
    store = _make_store(tmp_path)
    handlers = make_handlers(store)

    result = handlers["ncp_record_decision"]({
        "decision": "use connection pooling",
        "rationale": "reduce database connection overhead under concurrent load",
        "alternatives": ["individual connections", "connection per request"],
        "agent_id": "architect",
        "pipeline_id": "pipe_mcp",
        "confidence": 0.85,
        "tags": ["database", "performance"],
        "evidence_refs": ["sub_load_test"],
    })

    assert result["recorded"] is True
    assert result["tag_count"] == 2
    assert result["evidence_count"] == 1

    chunks = store.query(
        "connection pooling",
        k=4,
        min_score=0.0,
        layer="reasoning_trace",
        pipeline_id="pipe_mcp",
    )
    assert len(chunks) >= 1
    matching = [c for c in chunks if "connection pooling" in c.content]
    assert len(matching) >= 1
    assert matching[0].src == "agent_inferred"
    assert matching[0].written_by == "architect"


def test_mcp_record_decision_with_caused_by(tmp_path: Path) -> None:
    from ncp.mcp.server import make_handlers
    store = _make_store(tmp_path)
    handlers = make_handlers(store)

    store.write(SubconsciousChunk(
        chunk_id="parent_analysis",
        layer="episodic",
        content="performance analysis shows DB bottleneck",
        src="tool_result",
        pipeline_id="pipe_mcp",
    ))

    result = handlers["ncp_record_decision"]({
        "decision": "add read replicas",
        "rationale": "DB bottleneck identified in performance analysis",
        "agent_id": "architect",
        "pipeline_id": "pipe_mcp",
        "caused_by": "parent_analysis",
    })

    assert result["recorded"] is True
    chunk_id = result["chunk_id"]
    chunks = store.get_chunks_by_ids([chunk_id])
    assert len(chunks) == 1
    assert chunks[0].caused_by == "parent_analysis"


def test_mcp_record_decision_defaults_outcome_pending(tmp_path: Path) -> None:
    from ncp.mcp.server import make_handlers
    store = _make_store(tmp_path)
    handlers = make_handlers(store)

    result = handlers["ncp_record_decision"]({
        "decision": "minimal change",
        "rationale": "low risk",
        "agent_id": "fixer",
    })

    assert result["outcome"] == "pending"


# ── CLI tests ───────────────────────────────────────────────────────────


def test_cli_precedents_renders_table(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = _make_store(tmp_path)
    _seed_decisions(store)

    result = runner.invoke(
        main,
        ["precedents", "null guard", "--cwd", str(tmp_path), "--pipeline-id", "pipe_dt"],
    )

    assert result.exit_code == 0
    assert "NCP Precedents" in result.output
    assert "Precedent" in result.output


def test_cli_precedents_json_output(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = _make_store(tmp_path)
    _seed_decisions(store)

    result = runner.invoke(
        main,
        ["precedents", "retry logic", "--cwd", str(tmp_path),
         "--pipeline-id", "pipe_dt", "--json-output"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "precedents" in payload
    assert payload["query"] == "retry logic"


def test_cli_precedents_empty_result(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])

    result = runner.invoke(
        main,
        ["precedents", "nonexistent topic", "--cwd", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert "No matching" in result.output


def test_cli_precedents_tag_filter(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = _make_store(tmp_path)
    _seed_decisions(store)

    result = runner.invoke(
        main,
        ["precedents", "java", "--cwd", str(tmp_path),
         "--pipeline-id", "pipe_dt", "--tag", "caching", "--json-output"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    for p in payload["precedents"]:
        assert "caching" in p["tags"]


def test_cli_precedents_outcome_filter(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    store = _make_store(tmp_path)
    _seed_decisions(store)

    result = runner.invoke(
        main,
        ["precedents", "cache", "--cwd", str(tmp_path),
         "--pipeline-id", "pipe_dt", "--outcome", "failed", "--json-output"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    for p in payload["precedents"]:
        assert p["outcome"] == "failed"
