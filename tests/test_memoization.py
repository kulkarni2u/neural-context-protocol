"""Tests for CAP-C3 semantic work memoization."""

from __future__ import annotations

import json
import time
from pathlib import Path

from ncp.config import NCPConfig
from ncp.mcp.server import make_handlers, _handle_request
from ncp.stores.memo import compute_memo_signature, validate_code_memo_ast
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


# ---------------------------------------------------------------------------
# Fix 1: hit_count must only be counted once a memo is actually usable
# (not merely found), i.e. after outcome/verified gating in the handler.
# ---------------------------------------------------------------------------


def test_peek_memo_does_not_increment_hit_count(tmp_path: Path) -> None:
    """The telemetry-neutral peek must never mutate hit_count."""
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("peek_task")
    store.record_memo(sig, "peek_task", [])

    for _ in range(3):
        memo = store.peek_memo(sig)
        assert memo is not None
        assert memo["hit_count"] == 0

    stats = store.memo_stats()
    assert stats["hits"] == 0
    assert stats["misses"] == 0


def test_gated_lookup_does_not_count_as_hit(tmp_path: Path) -> None:
    """A memo rejected by outcome/verified gating must not inflate hit telemetry."""
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("gated_task")
    # Recorded with default outcome=0.0, verified=False.
    store.record_memo(sig, "gated_task", ["c1"], "result")

    config = NCPConfig(
        values={"memoization": {"enabled": True, "min_outcome": 0.5}},
        project_root=tmp_path,
    )
    handlers = make_handlers(store, config=config)

    result = _content(_handle_request(
        _call("ncp_lookup_memo", {"task": "gated_task"}),
        handlers,
    ))
    assert result["found"] is False, "min_outcome gate must reject a low-outcome memo"

    # The gate rejected the memo before it was returned as usable, so the
    # underlying row's hit_count must remain untouched, and telemetry must
    # record this as a miss rather than a hit.
    raw = store.peek_memo(sig)
    assert raw is not None
    assert raw["hit_count"] == 0

    stats = store.memo_stats()
    assert stats["hits"] == 0
    assert stats["misses"] == 1
    assert result["stats"]["hits"] == 0
    assert result["stats"]["misses"] == 1


def test_unverified_gated_lookup_does_not_count_as_hit(tmp_path: Path) -> None:
    """allow_unverified=False must gate out an unverified memo without counting a hit."""
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("unverified_task")
    store.record_memo(sig, "unverified_task", [])

    config = NCPConfig(
        values={"memoization": {"enabled": True, "allow_unverified": False}},
        project_root=tmp_path,
    )
    handlers = make_handlers(store, config=config)

    result = _content(_handle_request(
        _call("ncp_lookup_memo", {"task": "unverified_task"}),
        handlers,
    ))
    assert result["found"] is False

    raw = store.peek_memo(sig)
    assert raw is not None
    assert raw["hit_count"] == 0
    stats = store.memo_stats()
    assert stats["hits"] == 0
    assert stats["misses"] == 1


def test_usable_lookup_counts_exactly_one_hit(tmp_path: Path) -> None:
    """A memo that passes gating must count exactly one hit, not more."""
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("usable_task")
    store.record_memo(sig, "usable_task", ["c1"], "great result")
    store.update_memo_outcome(sig, 0.9, verified=True)

    config = NCPConfig(
        values={"memoization": {"enabled": True, "min_outcome": 0.5, "allow_unverified": False}},
        project_root=tmp_path,
    )
    handlers = make_handlers(store, config=config)

    result = _content(_handle_request(
        _call("ncp_lookup_memo", {"task": "usable_task"}),
        handlers,
    ))
    assert result["found"] is True
    stats = store.memo_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 0


# ---------------------------------------------------------------------------
# Fix 2: re-recording a memo must preserve hit_count/created_at/outcome/
# verified, not reset them (SQLite previously used INSERT OR REPLACE).
# ---------------------------------------------------------------------------


def test_rerecord_memo_preserves_hit_count_and_created_at(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("rerecord_task")
    store.record_memo(sig, "rerecord_task", ["c1"], "first result")

    # Accrue some hits and mark verified/outcome before re-recording.
    store.lookup_memo(sig)
    store.lookup_memo(sig)
    store.update_memo_outcome(sig, 0.75, verified=True)

    before = store.peek_memo(sig)
    assert before is not None
    assert before["hit_count"] == 2
    original_created_at = before["created_at"]

    # Re-record the same signature with updated task content.
    recorded = store.record_memo(sig, "rerecord_task", ["c1", "c2"], "second result")
    assert recorded is True

    after = store.peek_memo(sig)
    assert after is not None
    assert after["hit_count"] == 2, "re-record must not reset hit_count"
    assert after["created_at"] == original_created_at, "re-record must not reset created_at"
    assert after["outcome"] == 0.75, "re-record must not reset outcome"
    assert after["verified"] == 1, "re-record must not reset verified"
    # Content fields are still updated by the upsert.
    assert after["result_summary"] == "second result"
    assert json.loads(after["chunk_ids"]) == ["c1", "c2"]


def test_rerecord_memo_updates_content_fields(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    sig = compute_memo_signature("rerecord_content_task")
    store.record_memo(sig, "rerecord_content_task", [], "v1", output_tokens_est=10)
    store.record_memo(sig, "rerecord_content_task", ["cX"], "v2", output_tokens_est=20)

    memo = store.peek_memo(sig)
    assert memo is not None
    assert memo["result_summary"] == "v2"
    assert memo["output_tokens_est"] == 20
    assert json.loads(memo["chunk_ids"]) == ["cX"]


# ---------------------------------------------------------------------------
# Fix 4: AST guard for code memos must check declared structural kind, not
# just parseability.
# ---------------------------------------------------------------------------


def test_validate_code_memo_ast_rejects_syntax_errors() -> None:
    assert validate_code_memo_ast("def broken(:", "function") is False
    assert validate_code_memo_ast("", "function") is False


def test_validate_code_memo_ast_function_kind_requires_function() -> None:
    assert validate_code_memo_ast("x = 1\ny = 2", "function") is False, (
        "presence-only would wrongly accept a module with no function"
    )
    assert validate_code_memo_ast("def f():\n    return 1", "function") is True
    assert validate_code_memo_ast("async def f():\n    return 1", "function") is True


def test_validate_code_memo_ast_class_kind_requires_class() -> None:
    assert validate_code_memo_ast("def f():\n    return 1", "class") is False
    assert validate_code_memo_ast("class Foo:\n    pass", "class") is True


def test_validate_code_memo_ast_module_kind_is_presence_only() -> None:
    assert validate_code_memo_ast("x = 1", "module") is True
    assert validate_code_memo_ast("def f():\n    pass", "module") is True


def test_ncp_record_memo_rejects_mismatched_kind(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    handlers = make_handlers(store)

    result = _content(_handle_request(
        _call("ncp_record_memo", {
            "task": "code_task",
            "chunk_ids": ["c1"],
            "result_summary": "x = 1\ny = 2",  # no function def present
            "kind": "function",
        }),
        handlers,
    ))
    assert result["recorded"] is False
    assert "error" in result
    assert store.lookup_memo(result["signature"]) is None


def test_ncp_record_memo_accepts_matching_kind(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    handlers = make_handlers(store)

    result = _content(_handle_request(
        _call("ncp_record_memo", {
            "task": "code_task_2",
            "chunk_ids": ["c1"],
            "result_summary": "def helper():\n    return 42",
            "kind": "function",
        }),
        handlers,
    ))
    assert result["recorded"] is True
    memo = store.lookup_memo(result["signature"])
    assert memo is not None
    assert memo["result_summary"] == "def helper():\n    return 42"


def test_ncp_record_memo_without_kind_skips_ast_guard(tmp_path: Path) -> None:
    """Omitting `kind` must be fully backward compatible: no validation applied."""
    store = SQLiteStore(tmp_path / "store.db")
    handlers = make_handlers(store)

    result = _content(_handle_request(
        _call("ncp_record_memo", {
            "task": "no_kind_task",
            "chunk_ids": [],
            "result_summary": "not python at all !!!",
        }),
        handlers,
    ))
    assert result["recorded"] is True
