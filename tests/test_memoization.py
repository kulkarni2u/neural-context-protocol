"""Tests for CAP-C3 semantic work memoization."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ncp.mcp.server import make_handlers, _handle_request
from ncp.stores.memo import compute_memo_signature
from ncp.stores.sqlite import SQLiteStore


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
