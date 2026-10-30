"""Fan-in and consolidation never merge opposite or time-disjoint claims.

Regression: near-identical wording with opposite meaning ("still reproduces"
vs "no longer reproduces") scored above the merge threshold, so one side was
merged away before the contradiction check ever saw the pair.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ncp.stores.consolidation import claim_polarity, find_merge_candidates, reduce_candidates
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _chunk(cid: str, content: str, trust: float = 0.7, **kw: object) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=cid, content=content, layer="semantic", zone="working", pipeline_id="p",
        src="agent_inferred", written_by="w", base_trust=trust, chunk_type="prose", scope="pipeline", **kw,
    )


def _reduce(chunks: list[SubconsciousChunk]):
    return reduce_candidates(chunks, similarity_threshold=0.6, contradict_floor=0.35, min_cluster=3)


def _filler() -> list[SubconsciousChunk]:
    return [_chunk("f1", "billing exporter writes csv nightly"), _chunk("f2", "cache warmup runs at boot")]


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("login bug still fails on safari after the patch", "login bug no longer fails on safari after the patch"),
        ("the retry handler retries on 429 responses", "the retry handler does not retry on 429 responses"),
        ("token refresh bug was fixed in the auth service", "token refresh bug still throws in the auth service"),
    ],
    ids=["reversal", "negation", "fixed_vs_persists"],
)
def test_opposite_claims_are_kept_and_flagged(a: str, b: str) -> None:
    assert claim_polarity(a) != claim_polarity(b)
    result = _reduce([_chunk("a", a, 0.9), _chunk("b", b, 0.5), *_filler()])

    kept = {c.chunk_id for c in result.kept}
    assert {"a", "b"} <= kept
    assert result.merged_away == []
    assert ("a", "b") in result.contradictions or ("b", "a") in result.contradictions


def test_same_polarity_paraphrases_still_merge() -> None:
    result = _reduce([
        _chunk("a", "login bug no longer fails on safari after the patch", 0.9),
        _chunk("b", "login bug no longer fails on safari after the patch landed", 0.5),
        *_filler(),
    ])
    assert result.merged_away == [("b", "a")]
    assert result.contradictions == []


def test_time_disjoint_claims_are_neither_merged_nor_flagged() -> None:
    result = _reduce([
        _chunk("old", "deploy freeze is in effect for the release branch", 0.9, valid_from=100.0, valid_to=200.0),
        _chunk("new", "deploy freeze is in effect for the release branch", 0.5, valid_from=200.0),
        *_filler(),
    ])
    assert {"old", "new"} <= {c.chunk_id for c in result.kept}
    assert result.merged_away == []
    assert result.contradictions == []


def test_find_merge_candidates_respects_polarity() -> None:
    cluster = [
        _chunk("a", "login bug still fails on safari after the patch", 0.9),
        _chunk("b", "login bug no longer fails on safari after the patch", 0.5),
    ]
    assert find_merge_candidates(cluster, similarity_threshold=0.6) == []


def test_consolidate_does_not_merge_opposite_claims(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "ncp.db")
    store.write(_chunk("a", "login bug still fails on safari after the patch", 0.9))
    store.write(_chunk("b", "login bug no longer fails on safari after the patch", 0.5))

    report = store.consolidate(pipeline_id="p", similarity_threshold=0.6)

    assert report.merged == 0
    assert {c.chunk_id for c in store.get_chunks_by_ids(["a", "b"])} == {"a", "b"}
