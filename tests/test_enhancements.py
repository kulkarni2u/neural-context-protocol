import json
import pytest
import anyio
from unittest.mock import MagicMock, patch

from ncp.config import NCPConfig
from ncp.types import (
    Whisper,
    HandoffPayload,
    DissentPayload,
    AlertPayload,
    WorldCheckPayload,
    SubconsciousChunk,
    TurnRecord,
    NCPResponse,
)
from ncp.stores.rerank import Reranker
from ncp.stores.sqlite import SQLiteStore


# ==============================================================================
# 1. Structured Whisper Payloads Tests
# ==============================================================================

def test_whisper_nudge_plain_text():
    # Plain text remains unaltered
    wsp = Whisper(
        from_agent="planner",
        target="executor",
        whisper_type="nudge",
        payload="Hello there!",
        confidence=0.85,
    )
    assert wsp.payload == "Hello there!"


def test_whisper_share_valid():
    # Dict gets validated and parsed into JSON string
    payload_dict = {"slice": "pgvector", "files": ["db.py"], "ask": "verify schema"}
    wsp = Whisper(
        from_agent="planner",
        target="executor",
        whisper_type="share",
        payload=payload_dict,
        confidence=0.90,
    )
    # Check that it serialized correctly
    parsed = json.loads(wsp.payload)
    assert parsed["slice"] == "pgvector"
    assert parsed["files"] == ["db.py"]
    assert parsed["ask"] == "verify schema"


def test_whisper_share_invalid_missing_ask():
    # HandoffPayload requires 'ask'
    payload_dict = {"slice": "pgvector", "files": ["db.py"]}
    with pytest.raises(ValueError, match="payload validation failed"):
        Whisper(
            from_agent="planner",
            target="executor",
            whisper_type="share",
            payload=payload_dict,
            confidence=0.90,
        )


def test_whisper_dissent_valid():
    wsp = Whisper(
        from_agent="critic",
        target="planner",
        whisper_type="dissent",
        payload=DissentPayload(issue="ambiguous instruction", alternatives=["add details"]),
        confidence=0.95,
    )
    parsed = json.loads(wsp.payload)
    assert parsed["issue"] == "ambiguous instruction"
    assert parsed["alternatives"] == ["add details"]


def test_whisper_alert_valid():
    payload_dict = {"alert_code": "drift_high", "description": "goal drifted"}
    wsp = Whisper(
        from_agent="system",
        target="planner",
        whisper_type="alert",
        payload=payload_dict,
        confidence=1.0,
    )
    parsed = json.loads(wsp.payload)
    assert parsed["alert_code"] == "drift_high"


def test_whisper_world_check_valid():
    payload_dict = {"anchor_intent": "verify", "detected_drift": 0.45}
    wsp = Whisper(
        from_agent="system",
        target="planner",
        whisper_type="world_check",
        payload=payload_dict,
        confidence=1.0,
    )
    parsed = json.loads(wsp.payload)
    assert parsed["detected_drift"] == 0.45


def test_whisper_payload_length_violation():
    long_payload = "a" * 601
    with pytest.raises(ValueError, match="payload must be <= 600 characters"):
        Whisper(
            from_agent="planner",
            target="executor",
            whisper_type="nudge",
            payload=long_payload,
            confidence=0.85,
        )


# ==============================================================================
# 2. Cross-Encoder Reranker Tests
# ==============================================================================

def test_reranker_disabled():
    class MockConfig:
        rerank_enabled = False
        rerank_provider = "local"
        rerank_model = "mock"

    cfg = MockConfig()
    reranker = Reranker(cfg)  # type: ignore[arg-type]
    chunks = [
        SubconsciousChunk(layer="semantic", content="chunk 1", src="tool_result", relevance=0.1),
        SubconsciousChunk(layer="semantic", content="chunk 2", src="tool_result", relevance=0.9),
    ]
    results = reranker.rerank("query", chunks)
    # Reranking is disabled, so order/relevance remains unchanged
    assert results[0].relevance == 0.1
    assert results[1].relevance == 0.9


def test_reranker_local_fallback_jaccard():
    class MockConfig:
        rerank_enabled = True
        rerank_provider = "local"
        rerank_model = "mock"

    cfg = MockConfig()
    reranker = Reranker(cfg)  # type: ignore[arg-type]
    chunks = [
        SubconsciousChunk(layer="semantic", content="verify database migrations", src="tool_result", base_trust=0.7),
        SubconsciousChunk(layer="semantic", content="completely unrelated words here", src="tool_result", base_trust=0.5),
    ]
    # sentence-transformers is not mock-installed, so it will fall back to Jaccard
    with pytest.warns(ImportWarning, match="sentence-transformers not installed"):
        results = reranker.rerank("verify database", chunks)

    # First chunk should have much higher relevance than second due to lexical overlap Jaccard fallback
    assert results[0].content == "verify database migrations"
    assert results[0].relevance > results[1].relevance


@patch("cohere.Client")
def test_reranker_cohere_mocked(mock_client_class):
    # Mock Cohere Client
    mock_client = MagicMock()
    mock_client_class.return_value = mock_client

    class MockRerankResult:
        def __init__(self, index, relevance_score):
            self.index = index
            self.relevance_score = relevance_score

    class MockCohereResponse:
        results = [
            MockRerankResult(0, 0.95),
            MockRerankResult(1, 0.20),
        ]

    mock_client.rerank.return_value = MockCohereResponse()

    class MockConfig:
        rerank_enabled = True
        rerank_provider = "cohere"
        rerank_model = "rerank-english-v3.0"

    cfg = MockConfig()
    reranker = Reranker(cfg)  # type: ignore[arg-type]
    
    chunks = [
        SubconsciousChunk(layer="semantic", content="chunk 1", src="tool_result"),
        SubconsciousChunk(layer="semantic", content="chunk 2", src="tool_result"),
    ]

    with patch.dict("os.environ", {"COHERE_API_KEY": "fake_key"}):
        results = reranker.rerank("test query", chunks)

    mock_client.rerank.assert_called_once_with(
        model="rerank-english-v3.0",
        query="test query",
        documents=["chunk 1", "chunk 2"],
    )
    # Check that scores were mapped correctly and sorted descending
    assert results[0].content == "chunk 1"
    assert results[0].relevance == 0.95
    assert results[1].content == "chunk 2"
    assert results[1].relevance == 0.20


# ==============================================================================
# 3. Async Database counterpart methods Tests
# ==============================================================================

@pytest.mark.anyio
async def test_async_sqlite_store_operations(tmp_path):
    db_file = tmp_path / "test_async.db"
    store = SQLiteStore(db_file)

    chunk = SubconsciousChunk(
        chunk_id="sub_test_async",
        layer="semantic",
        content="async persistence tests",
        src="tool_result",
    )

    # 1. Async write
    write_ok = await store.async_write(chunk)
    assert write_ok is True

    # 2. Async query
    results = await store.async_query("persistence tests", k=2)
    assert len(results) == 1
    assert results[0].chunk_id == "sub_test_async"

    # 3. Async cost logging
    resp = NCPResponse(
        content="success",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.0001,
        model="gpt-4o",
        turn_id="turn_async_test",
        latency_ms=100,
    )
    await store.async_log_cost(agent_id="planner", response=resp)

    # Verify log entry in SQLite
    costs = store.cost_summary()
    assert costs["summary"]["cost_usd_total"] == pytest.approx(0.0001)

    # 4. Async turn record logging
    record = TurnRecord(
        turn_id="turn_async_test_2",
        agent_id="executor",
        task="run",
        slot="slot_a",
        result="done",
        result_full="all done",
    )
    await store.async_log_turn_record(record)
    resolved = await store.async_resolve_recent_ref("r:sub/turn_async_test_2")
    assert resolved is not None
    assert resolved.result == "done"
