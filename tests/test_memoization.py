"""Tests for CAP-C3 semantic work memoization."""

from __future__ import annotations

import json
import time
from pathlib import Path

from ncp.mcp.server import make_handlers, _handle_request
from ncp.stores.memo import compute_memo_signature
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_tokens


def _call(name: str, arguments: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def _content(response_str: str) -> dict:
    r = json.loads(response_str)["result"]
    return json.loads(r["content"][0]["text"])


# ---------------------------------------------------------------------------
# Signature computation
# ---------------------------------------------------------------------------


def test_signature_is_deterministic() -> None:
    sig1 = compute_memo_signature("translate text", "context_en_fr")
    sig2 = compute_memo_signature("translate text", "context_en_fr")
    assert sig1 == sig2
    assert len(sig1) == 64
    # Verify it's a hex SHA-256
    int(sig1, 16)


def test_signature_differs_for_different_inputs() -> None:
    sig_a = compute_memo_signature("task_a", "ctx")
    sig_b = compute_memo_signature("task_b", "ctx")
    assert sig_a != sig_b


def test_signature_normalizes_whitespace() -> None:
    sig1 = compute_memo_signature("  hello   world  ", "ctx")
    sig2 = compute_memo_signature("hello world", "ctx")
    assert sig1 == sig2


def test_signature_without_context() -> None:
    sig = compute_memo_signature("hello")
    assert len(sig) == 64


# ---------------------------------------------------------------------------
# SQLiteStore memo operations
# ---------------------------------------------------------------------------


def test_record_and_lookup_memo(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("test_task", "ctx")
    recorded = store.record_memo(sig, "test_task", ["chunk_a", "chunk_b"], "did the thing")
    assert recorded is True

    memo = store.lookup_memo(sig)
    assert memo is not None
    assert memo["signature"] == sig
    assert memo["task"] == "test_task"
    assert json.loads(memo["chunk_ids"]) == ["chunk_a", "chunk_b"]
    assert memo["result_summary"] == "did the thing"
    assert memo["outcome"] == 0.0
    assert memo["hit_count"] == 0


def test_lookup_unknown_signature_returns_none(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    memo = store.lookup_memo("nonexistent_sig")
    assert memo is None


def test_lookup_stale_memo_returns_none(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("stale_task")
    store.record_memo(sig, "stale_task", [])
    # Manually push created_at into the past so it looks stale
    with store._connect() as conn:
        conn.execute(
            "UPDATE memo_entries SET created_at = ? WHERE signature = ?",
            (time.time() - 25 * 3600, sig),
        )
    memo = store.lookup_memo(sig)
    assert memo is None


def test_update_memo_outcome(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("outcome_task")
    store.record_memo(sig, "outcome_task", [])

    updated = store.update_memo_outcome(sig, 0.85, verified=True)
    assert updated is True

    memo = store.lookup_memo(sig)
    assert memo is not None
    assert memo["outcome"] == 0.85
    assert memo["verified"] == 1


def test_update_memo_outcome_unknown(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    updated = store.update_memo_outcome("missing_sig", 0.5)
    assert updated is False


# ---------------------------------------------------------------------------
# MCP tool end-to-end
# ---------------------------------------------------------------------------


def test_ncp_lookup_memo_end_to_end(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("mcp_task", "mcp_ctx")
    store.record_memo(sig, "mcp_task", ["c1"], "mcp result")
    handlers = make_handlers(store)

    result = _content(_handle_request(
        _call("ncp_lookup_memo", {"task": "mcp_task", "context": "mcp_ctx"}),
        handlers,
    ))
    assert result["found"] is True
    assert result["memo"] is not None
    assert result["memo"]["signature"] == sig
    assert result["memo"]["result_summary"] == "mcp result"


def test_ncp_lookup_memo_not_found(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    handlers = make_handlers(store)
    result = _content(_handle_request(
        _call("ncp_lookup_memo", {"task": "unknown_task"}),
        handlers,
    ))
    assert result["found"] is False
    assert result["memo"] is None


def test_ncp_lookup_memo_by_signature(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("direct_task")
    store.record_memo(sig, "direct_task", [])
    handlers = make_handlers(store)

    result = _content(_handle_request(
        _call("ncp_lookup_memo", {"signature": sig}),
        handlers,
    ))
    assert result["found"] is True
    assert result["memo"]["signature"] == sig


def test_ncp_record_memo_end_to_end(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    handlers = make_handlers(store)

    result = _content(_handle_request(
        _call("ncp_record_memo", {
            "task": "record_test",
            "chunk_ids": ["c1", "c2"],
            "result_summary": "recorded result",
        }),
        handlers,
    ))
    assert result["recorded"] is True
    assert len(result["signature"]) == 64

    sig = result["signature"]
    memo = store.lookup_memo(sig)
    assert memo is not None
    assert memo["task"] == "record_test"
    assert json.loads(memo["chunk_ids"]) == ["c1", "c2"]


# ---------------------------------------------------------------------------
# S4.1: memoization telemetry — hits, misses, estimated tokens saved
# ---------------------------------------------------------------------------


def test_memo_telemetry_hits_misses_tokens_saved(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("telemetry_task", "ctx")
    result_summary = "a memoized result summary long enough to have several tokens"
    store.record_memo(sig, "telemetry_task", ["c1"], result_summary)
    expected_tokens = estimate_tokens(result_summary)

    first_hit = store.lookup_memo(sig)
    assert first_hit is not None
    assert first_hit["output_tokens_est"] == expected_tokens
    second_hit = store.lookup_memo(sig)
    assert second_hit is not None
    assert store.lookup_memo("no_such_signature") is None

    stats = store.memo_stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    assert stats["entry_count"] == 1
    # Estimated savings: SUM(hit_count * output_tokens_est).
    assert stats["estimated_tokens_saved"] == 2 * expected_tokens


def test_memo_stats_empty_store_is_all_zero(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    stats = store.memo_stats()
    assert stats == {
        "hits": 0,
        "misses": 0,
        "entry_count": 0,
        "estimated_tokens_saved": 0,
    }


def test_stale_memo_lookup_counts_as_miss(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("stale_telemetry_task")
    store.record_memo(sig, "stale_telemetry_task", [])
    with store._connect() as conn:
        conn.execute(
            "UPDATE memo_entries SET created_at = ? WHERE signature = ?",
            (time.time() - 25 * 3600, sig),
        )
    assert store.lookup_memo(sig) is None
    stats = store.memo_stats()
    assert stats["hits"] == 0
    assert stats["misses"] == 1


def test_record_memo_explicit_output_tokens_est(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("real_count_task")
    store.record_memo(sig, "real_count_task", [], "summary", output_tokens_est=1234)
    memo = store.lookup_memo(sig)
    assert memo is not None
    assert memo["output_tokens_est"] == 1234
    stats = store.memo_stats()
    assert stats["estimated_tokens_saved"] == 1234


def test_ncp_lookup_memo_response_includes_counters(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("stats_task", "stats_ctx")
    store.record_memo(sig, "stats_task", ["c1"], "stats result")
    handlers = make_handlers(store)

    hit = _content(_handle_request(
        _call("ncp_lookup_memo", {"task": "stats_task", "context": "stats_ctx"}),
        handlers,
    ))
    assert hit["found"] is True
    assert hit["stats"]["hits"] == 1
    assert hit["stats"]["misses"] == 0

    miss = _content(_handle_request(
        _call("ncp_lookup_memo", {"task": "unknown_stats_task"}),
        handlers,
    ))
    assert miss["found"] is False
    assert miss["stats"]["hits"] == 1
    assert miss["stats"]["misses"] == 1


def test_ncp_record_memo_with_explicit_signature(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    handlers = make_handlers(store)
    explicit_sig = "a" * 64

    result = _content(_handle_request(
        _call("ncp_record_memo", {
            "task": "explicit_sig_test",
            "chunk_ids": ["c1"],
            "signature": explicit_sig,
        }),
        handlers,
    ))
    assert result["recorded"] is True
    assert result["signature"] == explicit_sig
