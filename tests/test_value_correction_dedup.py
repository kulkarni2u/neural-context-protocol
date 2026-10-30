"""Write-time dedup and merging never swallow a corrected value or flipped claim.

Regression: dedup was pure text similarity (> 0.92), so "fact version v2"
after "fact version v1" (or "retries 5 times" after "retries 3 times") was
dropped as a duplicate. A following supersede() then failed and the stale
value kept being served. Found by benchmarks/memo_reuse.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from ncp.mcp.server import _handle_request, make_handlers
from ncp.stores.consolidation import find_merge_candidates, is_near_duplicate
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk

_LIVE = os.getenv("NCP_RUN_PGVECTOR_INTEGRATION") == "1"
_DSN = os.getenv("NCP_PGVECTOR_DSN", "postgresql://postgres:postgres@127.0.0.1:5432/ncp")


@pytest.mark.parametrize(
    ("a", "b", "duplicate"),
    [
        ("payment service retries 3 times on timeout", "payment service retries 5 times on timeout", False),
        ("topic0 fact version v1 for the benchmark", "topic0 fact version v2 for the benchmark", False),
        ("the auth handler validates tokens", "the auth handler does not validate tokens", False),
        ("payment service retries 3 times on timeout", "payment service retries 3 times on timeout.", True),
    ],
)
def test_is_near_duplicate(a: str, b: str, duplicate: bool) -> None:
    assert is_near_duplicate(a, b) is duplicate


def test_value_change_is_not_merged() -> None:
    def chunk(cid: str, content: str) -> SubconsciousChunk:
        return SubconsciousChunk(chunk_id=cid, layer="semantic", content=content, src="agent_inferred", pipeline_id="p")

    cluster = [chunk("a", "service timeout is 30 seconds"), chunk("b", "service timeout is 45 seconds")]
    assert find_merge_candidates(cluster, similarity_threshold=0.6) == []


def _chunk(cid: str, version: int) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=cid, layer="semantic", content=f"payment fact version v{version} for retries",
        src="tool_result", pipeline_id="p", written_by="oracle",
    )


def _assert_correction_lands(write, supersede) -> None:
    assert write(_chunk("v1", 1)) is True
    assert write(_chunk("v1_again", 1)) is False  # a true duplicate is still suppressed
    assert write(_chunk("v2", 2)) is True
    assert supersede("v1", "v2") is True


def test_sqlite_correction_is_written(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    _assert_correction_lands(store.write, store.supersede)


def test_pgvector_correction_is_written() -> None:
    if not _LIVE:
        pytest.skip("set NCP_RUN_PGVECTOR_INTEGRATION=1 to run live pgvector tests")
    pytest.importorskip("psycopg")
    from ncp.stores.pgvector import PgvectorStore

    store = PgvectorStore(_DSN, schema=f"ncp_vc_{uuid4().hex[:8]}", table_prefix="it_")
    _assert_correction_lands(store.write, store.supersede)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_async_pgvector_correction_is_written() -> None:
    if not _LIVE:
        pytest.skip("set NCP_RUN_PGVECTOR_INTEGRATION=1 to run live pgvector tests")
    pytest.importorskip("psycopg_pool")
    from ncp.stores.pgvector_async import AsyncPgvectorStore

    store = AsyncPgvectorStore(_DSN, schema=f"ncp_avc_{uuid4().hex[:8]}", table_prefix="it_")
    try:
        assert await store.async_write(_chunk("v1", 1)) is True
        assert await store.async_write(_chunk("v1_again", 1)) is False
        assert await store.async_write(_chunk("v2", 2)) is True
        assert await store.async_supersede("v1", "v2") is True
    finally:
        await store.close()


def test_write_memory_supersedes_with_corrected_value(tmp_path: Path) -> None:
    handlers = make_handlers(SQLiteStore(tmp_path / "ncp.db"))

    def write(args: dict) -> dict:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "ncp_write_memory", "arguments": args}}
        return json.loads(json.loads(_handle_request(request, handlers))["result"]["content"][0]["text"])

    base = {"layer": "semantic", "src": "tool_result", "written_by": "ops", "pipeline_id": "p"}
    write({**base, "chunk_id": "timeout_v1", "content": "gateway timeout is 30 seconds"})
    result = write({**base, "chunk_id": "timeout_v2", "content": "gateway timeout is 45 seconds",
                    "supersedes": "timeout_v1"})

    assert result["written"] is True
    assert result["superseded"] is True
