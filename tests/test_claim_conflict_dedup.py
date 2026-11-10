"""Write-time dedup must not discard a contradicting claim or an explicit correction.

Regression: ``is_near_duplicate`` compared numeric values and polarity but not
word values, so "...is on Friday morning" followed by "...is on Tuesday
morning" was dropped as a duplicate -- even when the second chunk declared
``supersedes`` pointing at the first. Found by benchmarks/conflict_trust.

The rule under test (ncp/stores/consolidation.py): two texts are different
claims when, over the same number of word tokens and not as a pure reordering,
one or two aligned positions differ by a content word -- not case,
punctuation, a stopword, a digit-bearing token (numbers are compared on their
own) or an inflection of the same word.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from benchmarks.conflict_trust.run import FACTS
from ncp.stores.consolidation import (
    claim_value_substitutions,
    find_merge_candidates,
    is_near_duplicate,
    same_claim_shape,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk

BASE = "The deploy window for the billing service is on Friday morning."


def _benchmark_pairs() -> list[tuple[str, str]]:
    pairs = []
    for fact in FACTS:
        true_text = fact.statement.format(value=fact.true_value)
        pairs += [(true_text, fact.statement.format(value=wrong)) for wrong in fact.wrong_values]
    return pairs


@pytest.mark.parametrize(("a", "b"), _benchmark_pairs())
def test_benchmark_sentence_shapes_with_a_different_value_are_distinct(a: str, b: str) -> None:
    assert is_near_duplicate(a, b) is False
    assert is_near_duplicate(b, a) is False


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (BASE, "The deploy window for the billing service is on Tuesday morning."),
        (BASE, "The deploy window for the billing service is on Friday evening."),
        (BASE, "The deploy window for the billing service is on Tuesday evening."),  # two words swapped
        ("The invoice export job is owned by the finance team.", "The invoice export job is owned by the growth team."),
        ("Refund events go to the ledger queue.", "Refund events go to the audit queue."),
        ("payment service retries 3 times on timeout", "payment service retries 5 times on timeout"),
        ("the cache ttl is 900 seconds", "the cache ttl is 9000 seconds"),
        ("topic0 fact version v1 for the benchmark", "topic0 fact version v2 for the benchmark"),
        ("the edge proxy accepts tls 1.3 as its minimum", "the edge proxy accepts tls 1.2 as its minimum"),
        ("the auth handler validates tokens", "the auth handler does not validate tokens"),
        ("the bug is fixed in the parser module today", "the bug still throws in the parser module today"),
        ("Alerts page the data rota for webhook failures", "Alerts page the payments rota for webhook failures"),
        ("the primary orders database is in oregon region", "the primary orders database is in ohio region"),
    ],
)
def test_value_conflicts_are_distinct(a: str, b: str) -> None:
    assert is_near_duplicate(a, b) is False


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (BASE, BASE),
        (BASE, BASE.lower()),  # case
        (BASE, BASE.rstrip(".")),  # punctuation
        (BASE, BASE + "  "),  # whitespace
        (BASE, "The deploy window for the billing service is on Friday morning!"),
        (BASE, "The deploy window for a billing service is on Friday morning."),  # stopword swap
        ("Webhook delivery failures page the integrations rota.", "Webhook delivery failures paged the integrations rota."),
        ("The payment service retries on timeout.", "The payment service retried on timeout."),  # retry/retried
        ("The payment service retries on timeout.", "The payment service retry on timeout."),
        ("Pagination controls gained keyboard focus styles", "Pagination controls gained keyboard focus style"),
        ("The cache ttl for session tokens is 900 seconds.", "The cache ttl for session tokens is 900 seconds"),
        ("the cache ttl for session tokens is 900 seconds right now", "The cache ttl for session tokens is 900 seconds right now."),
        ("the new checkout flow is gated by the aurora flag", "the new checkout flow is gated by the aurora flags"),
        ("the edge proxy accepts tls version 1.3 as its minimum value", "The edge proxy accepts TLS version 1.3 as its minimum value."),
    ],
)
def test_genuine_rewordings_still_dedupe(a: str, b: str) -> None:
    assert is_near_duplicate(a, b) is True
    assert is_near_duplicate(b, a) is True


def test_filler_and_reordering_are_not_value_substitutions() -> None:
    # Different token counts or the same words reordered carry no aligned
    # substitution, so only the similarity ratio decides (as before).
    assert claim_value_substitutions(BASE, BASE + " Confirmed again.") == 0
    assert claim_value_substitutions("retries on timeout payment", "payment retries on timeout") == 0
    long_text = "the payment gateway client retries failed requests with exponential backoff and jitter " * 3
    assert is_near_duplicate(long_text, long_text.replace("jitter", "jitter confirmed again", 1)) is True


def test_wide_rewrites_are_left_to_the_similarity_ratio() -> None:
    # More than two swapped content words is a rewrite, not a value correction.
    a = "the quick brown fox jumps over the lazy sleeping dog near the river bank today"
    b = a.replace("quick", "rapid", 1).replace("brown", "tan", 1).replace("lazy", "idle", 1)
    assert claim_value_substitutions(a, b) == 3
    assert is_near_duplicate(a, b, threshold=0.6) is True  # decided by the ratio alone
    assert is_near_duplicate(a, b) is False  # ratio below the default threshold
    one = a.replace("quick", "rapid", 1)
    assert is_near_duplicate(a, one, threshold=0.6) is False  # a single swap is a value conflict


def test_consolidation_keeps_the_looser_claim_shape_rule() -> None:
    # Consolidation/fan-in use same_claim_shape (numbers + polarity) at a lower
    # similarity threshold; only write-time dedup applies the word-value rule.
    other = "The deploy window for the billing service is on Tuesday morning."
    assert same_claim_shape(BASE, other) is True
    assert is_near_duplicate(BASE, other) is False

    def chunk(cid: str, content: str) -> SubconsciousChunk:
        return SubconsciousChunk(chunk_id=cid, layer="semantic", content=content, src="agent_inferred", pipeline_id="p")

    assert find_merge_candidates([chunk("a", BASE), chunk("b", other)], similarity_threshold=0.6) != []


# --------------------------------------------------------------------------
# SQLite store
# --------------------------------------------------------------------------


def _chunk(cid: str, content: str, **kwargs) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=cid, layer="semantic", content=content, src="tool_result",
        pipeline_id="p", written_by="oracle", **kwargs,
    )


def test_sqlite_non_numeric_contradiction_is_written(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    assert store.write(_chunk("friday", BASE)) is True
    assert store.write(_chunk("tuesday", BASE.replace("Friday", "Tuesday"))) is True
    # A true repeat and a trivial rewording are still suppressed.
    assert store.write(_chunk("friday_again", BASE)) is False
    assert store.write(_chunk("friday_lower", BASE.lower())) is False
    ids = {c.chunk_id for c in store.query("deploy window billing service", pipeline_id="p", k=5)}
    assert {"friday", "tuesday"} <= ids


def test_sqlite_supersedes_correction_is_written_and_hides_old_chunk(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    assert store.write(_chunk("old", BASE)) is True
    assert store.write(_chunk("new", BASE.replace("Friday", "Tuesday"), supersedes="old")) is True
    assert store.supersede("old", "new") is True
    ids = [c.chunk_id for c in store.query("deploy window billing service", pipeline_id="p", k=5)]
    assert ids == ["new"]


def test_sqlite_supersedes_is_never_a_duplicate_of_its_target(tmp_path: Path) -> None:
    # Even a byte-identical restatement that declares supersedes is stored.
    store = SQLiteStore(tmp_path / "ncp.db")
    assert store.write(_chunk("old", BASE)) is True
    assert store.write(_chunk("restated", BASE, supersedes="old")) is True


def test_supersedes_exempts_only_the_named_chunk(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    assert store.write(_chunk("a", BASE)) is True
    assert store.write(_chunk("b", "Refund events go to the ledger queue.")) is True
    # Supersedes "b" but duplicates "a": still suppressed against "a".
    assert store.write(_chunk("c", BASE, supersedes="b")) is False
    # A JSON list of superseded ids (consolidation shape) exempts each entry.
    assert store.write(_chunk("d", BASE, supersedes=json.dumps(["a", "b"]))) is True


def test_write_memory_supersedes_with_non_numeric_value(tmp_path: Path) -> None:
    from ncp.mcp.server import _handle_request, make_handlers

    handlers = make_handlers(SQLiteStore(tmp_path / "ncp.db"))

    def write(args: dict) -> dict:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "ncp_write_memory", "arguments": args}}
        return json.loads(json.loads(_handle_request(request, handlers))["result"]["content"][0]["text"])

    base = {"layer": "semantic", "src": "tool_result", "written_by": "ops", "pipeline_id": "p"}
    write({**base, "chunk_id": "w1", "content": BASE})
    result = write({**base, "chunk_id": "w2", "content": BASE.replace("Friday", "Tuesday"), "supersedes": "w1"})
    assert result["written"] is True
    assert result["superseded"] is True


def _mcp_write(handlers: dict, args: dict) -> dict:
    from ncp.mcp.server import _handle_request

    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "ncp_write_memory", "arguments": args}}
    return json.loads(json.loads(_handle_request(request, handlers))["result"]["content"][0]["text"])


def test_write_memory_supersedes_argument_is_never_deduped_against_its_target(tmp_path: Path) -> None:
    from ncp.mcp.server import make_handlers

    handlers = make_handlers(SQLiteStore(tmp_path / "ncp.db"))
    base = {"layer": "semantic", "src": "tool_result", "written_by": "ops", "pipeline_id": "p"}
    assert _mcp_write(handlers, {**base, "chunk_id": "w1", "content": BASE})["written"] is True
    # Without a declared correction the restatement is a duplicate...
    assert _mcp_write(handlers, {**base, "chunk_id": "w_dup", "content": BASE})["written"] is False
    # ...and with one it is stored and replaces the old chunk.
    result = _mcp_write(handlers, {**base, "chunk_id": "w2", "content": BASE, "supersedes": "w1"})
    assert result["written"] is True and result["superseded"] is True


def test_write_memory_supersedes_edge_is_never_deduped_against_its_target(tmp_path: Path) -> None:
    from ncp.mcp.server import make_handlers

    handlers = make_handlers(SQLiteStore(tmp_path / "ncp.db"))
    base = {"layer": "semantic", "src": "tool_result", "written_by": "ops", "pipeline_id": "p"}
    assert _mcp_write(handlers, {**base, "chunk_id": "w1", "content": BASE})["written"] is True
    result = _mcp_write(
        handlers,
        {**base, "chunk_id": "w2", "content": BASE, "edges": [{"dst": "w1", "type": "supersedes"}]},
    )
    assert result["written"] is True


# --------------------------------------------------------------------------
# pgvector (sync, fake connection) -- the dedup scan returns rows with chunk_id
# --------------------------------------------------------------------------


def _pg_store_returning(rows: list[dict]):
    from ncp.stores.pgvector import PgvectorStore

    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchall = MagicMock(return_value=rows)
    cursor.fetchone = MagicMock(return_value=None)
    cursor.execute = MagicMock()
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return PgvectorStore("postgresql://localhost/test", connect_factory=lambda _: conn)


def test_pgvector_dedup_scan_selects_chunk_id() -> None:
    store = _pg_store_returning([])
    store.write(_chunk("x", BASE))
    sql = [str(c.args[0]) for c in store._connect().__enter__().cursor().execute.call_args_list]
    assert any("SELECT content, chunk_id FROM" in s for s in sql)


def test_pgvector_non_numeric_contradiction_is_not_a_duplicate() -> None:
    store = _pg_store_returning([{"content": BASE, "chunk_id": "friday"}])
    with store._connect() as connection:
        assert store._is_duplicate(connection, _chunk("tuesday", BASE.replace("Friday", "Tuesday"))) is False
        assert store._is_duplicate(connection, _chunk("friday_lower", BASE.lower())) is True


def test_pgvector_supersedes_is_never_a_duplicate_of_its_target() -> None:
    store = _pg_store_returning([{"content": BASE, "chunk_id": "old"}])
    with store._connect() as connection:
        assert store._is_duplicate(connection, _chunk("new", BASE, supersedes="old")) is False
        assert store._is_duplicate(connection, _chunk("new", BASE, supersedes="other")) is True
        assert store._is_duplicate(connection, _chunk("new", BASE, supersedes=json.dumps(["x", "old"]))) is False


def test_pgvector_fake_db_write_stores_contradiction_and_correction() -> None:
    from tests.test_future_stores import _MemoryPgDB, _pg_connect_factory

    from ncp.stores.pgvector import PgvectorStore

    store = PgvectorStore("postgresql://localhost/test", connect_factory=_pg_connect_factory(_MemoryPgDB()))
    assert store.write(_chunk("friday", BASE)) is True
    assert store.write(_chunk("friday_again", BASE)) is False
    assert store.write(_chunk("tuesday", BASE.replace("Friday", "Tuesday"))) is True


# --------------------------------------------------------------------------
# pgvector (async, fake connection)
# --------------------------------------------------------------------------


def _async_conn_returning(rows: list[dict]):
    from unittest.mock import AsyncMock

    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=rows)
    cursor.description = [("content",), ("chunk_id",)]
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=False)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    return conn


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_async_pgvector_dedup_honours_value_conflict_and_supersedes() -> None:
    pytest.importorskip("psycopg_pool")
    from unittest.mock import patch

    from ncp.stores.pgvector_async import AsyncPgvectorStore

    with patch("psycopg_pool.AsyncConnectionPool", return_value=MagicMock()):
        store = AsyncPgvectorStore("postgresql://localhost/test")
    conn = _async_conn_returning([{"content": BASE, "chunk_id": "friday"}])

    assert await store._async_is_duplicate(conn, _chunk("tuesday", BASE.replace("Friday", "Tuesday"))) is False
    assert await store._async_is_duplicate(conn, _chunk("friday_lower", BASE.lower())) is True
    assert await store._async_is_duplicate(conn, _chunk("restated", BASE, supersedes="friday")) is False
    assert await store._async_is_duplicate(conn, _chunk("restated", BASE, supersedes="other")) is True
