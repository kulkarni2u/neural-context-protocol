"""Tests for Finding 3 of docs/NCP_SILENT_DISCONNECT_AUDIT.md.

Production embedding spend used to be invisible to NCP's own cost
accounting: SQLiteStore.write()/query() call a configured
`embedding_adapter.embed(...)` -- a real, potentially billable call -- with
no record of it anywhere. These tests confirm the new `embedding_cost_log`
table / `log_embedding_cost()` / `embedding_cost_summary()` surface it, and
that a store with no embedding adapter configured logs nothing and behaves
exactly as before.
"""

from pathlib import Path

from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


class _FixedVectorAdapter:
    """Fake embedding adapter: fixed small vector, no real API call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.1, 0.2, 0.3]


def test_embedding_cost_log_table_created(tmp_path: Path) -> None:
    store_path = tmp_path / "store.db"
    SQLiteStore(store_path)

    import sqlite3

    connection = sqlite3.connect(store_path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    connection.close()

    assert "embedding_cost_log" in tables


def test_write_and_query_log_embedding_cost(tmp_path: Path) -> None:
    adapter = _FixedVectorAdapter()
    store = SQLiteStore(tmp_path / "store.db", embedding_adapter=adapter)

    for i in range(3):
        store.write(
            SubconsciousChunk(
                chunk_id=f"sub_{i}",
                layer="semantic",
                content=f"embedding cost tracking content number {i} " * 3,
                src="tool_result",
                pipeline_id="pipe_embed",
            )
        )

    for _ in range(2):
        store.query("embedding cost tracking", k=4, pipeline_id="pipe_embed")

    summary = store.embedding_cost_summary()
    assert summary["summary"]["entry_count"] == 5  # 3 writes + 2 queries
    assert summary["summary"]["estimated_tokens_total"] > 0
    assert summary["summary"]["char_count_total"] > 0

    by_op = {row["op"]: row for row in summary["by_op"]}
    assert by_op["write"]["calls"] == 3
    assert by_op["query"]["calls"] == 2


def test_embedding_cost_summary_filters_by_pipeline_id(tmp_path: Path) -> None:
    adapter = _FixedVectorAdapter()
    store = SQLiteStore(tmp_path / "store.db", embedding_adapter=adapter)

    store.write(
        SubconsciousChunk(
            chunk_id="sub_a",
            layer="semantic",
            content="pipeline alpha content for embedding cost test",
            src="tool_result",
            pipeline_id="pipe_alpha",
        )
    )
    store.write(
        SubconsciousChunk(
            chunk_id="sub_b",
            layer="semantic",
            content="pipeline beta content for embedding cost test",
            src="tool_result",
            pipeline_id="pipe_beta",
        )
    )

    alpha_summary = store.embedding_cost_summary(pipeline_id="pipe_alpha")
    assert alpha_summary["summary"]["entry_count"] == 1

    all_summary = store.embedding_cost_summary()
    assert all_summary["summary"]["entry_count"] == 2


def test_no_embedding_adapter_logs_nothing_and_behaves_as_before(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")

    store.write(
        SubconsciousChunk(
            chunk_id="sub_no_adapter",
            layer="semantic",
            content="standard retrieval content with no embedding adapter configured",
            src="tool_result",
        )
    )
    results = store.query("standard retrieval", k=4, min_score=0.0)

    assert len(results) == 1
    assert results[0].chunk_id == "sub_no_adapter"

    summary = store.embedding_cost_summary()
    assert summary["summary"]["entry_count"] == 0
    assert summary["summary"]["estimated_tokens_total"] == 0
    assert summary["by_op"] == []


def test_log_embedding_cost_never_raises_on_bad_input(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")

    # Even a pathological call must be swallowed silently (defensive
    # try/except around the write/query hot path).
    store.log_embedding_cost(pipeline_id=None, op="write", text="")
    store.log_embedding_cost(pipeline_id="pipe_x", op="query", text="some text")

    summary = store.embedding_cost_summary()
    assert summary["summary"]["entry_count"] == 2
