from pathlib import Path
from unittest.mock import patch

from ncp.config import load_config
from ncp.skill_cache import (
    _MANIFEST_SECTION_INDEX,
    _section_chunk_id,
    cache_skill,
    ensure_cached,
    fetch_skill,
    recall_skills,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / "ncp.db")


WORKFLOW_CONTENT = (
    "Every subagent must call ncp_get_context before starting work. "
    "Every subagent must call ncp_post_turn before finishing. "
) * 40  # long enough to force multiple windows


def test_cache_skill_writes_sections_and_manifest(tmp_path: Path):
    store = _store(tmp_path)
    result = cache_skill(WORKFLOW_CONTENT, skill_id="acme/workflow", store=store)

    assert result.section_count >= 2
    assert len(result.chunk_ids) == result.section_count
    assert result.written == result.section_count + 1  # + manifest

    sections = store.get_chunks_by_ids(result.chunk_ids)
    assert len(sections) == result.section_count
    assert all(chunk.src == "skill_ref" for chunk in sections)
    assert all(chunk.scope == "global" for chunk in sections)
    assert all(chunk.zone == "proven" for chunk in sections)
    assert all("skill:acme/workflow" in chunk.conditions for chunk in sections)
    assert all(chunk.base_trust == 0.6 for chunk in sections)  # default skill_cache trust

    manifest_id = _section_chunk_id("acme/workflow", result.content_hash, _MANIFEST_SECTION_INDEX)
    manifest = store.get_chunks_by_ids([manifest_id])
    assert len(manifest) == 1
    assert "manifest" in manifest[0].conditions


def test_recaching_identical_content_is_idempotent(tmp_path: Path):
    store = _store(tmp_path)
    first = cache_skill(WORKFLOW_CONTENT, skill_id="acme/workflow", store=store)
    second = cache_skill(WORKFLOW_CONTENT, skill_id="acme/workflow", store=store)

    assert first.content_hash == second.content_hash
    assert first.chunk_ids == second.chunk_ids
    # Regression guard: a second cache_skill() call for the same skill (as a
    # different subagent process would make) must not raise. write()'s
    # _assert_src_immutable rejects a written_by mismatch on re-writing an
    # existing chunk_id -- cache_skill() must therefore use a caller-
    # independent written_by for a given skill_id, not the calling agent's
    # own identity.
    assert second.written == second.section_count + 1


def test_changed_content_gets_a_different_hash_and_ids(tmp_path: Path):
    store = _store(tmp_path)
    first = cache_skill(WORKFLOW_CONTENT, skill_id="acme/workflow", store=store)
    second = cache_skill(WORKFLOW_CONTENT + " New rule added.", skill_id="acme/workflow", store=store)

    assert first.content_hash != second.content_hash
    assert set(first.chunk_ids).isdisjoint(second.chunk_ids)


def test_fetch_skill_returns_full_content_verbatim_in_order(tmp_path: Path):
    store = _store(tmp_path)
    result = cache_skill(WORKFLOW_CONTENT, skill_id="acme/workflow", store=store)

    fetched = fetch_skill("acme/workflow", content_hash=result.content_hash, store=store)

    assert fetched is not None
    sections = store.get_chunks_by_ids(result.chunk_ids)
    ordered_by_index = sorted(
        sections, key=lambda c: int(next(cond for cond in c.conditions if cond.startswith("section:")).split(":")[1])
    )
    assert fetched == "\n\n".join(chunk.content for chunk in ordered_by_index)


def test_fetch_skill_misses_on_unknown_hash(tmp_path: Path):
    store = _store(tmp_path)
    cache_skill(WORKFLOW_CONTENT, skill_id="acme/workflow", store=store)

    assert fetch_skill("acme/workflow", content_hash="deadbeef" * 8, store=store) is None


def test_fetch_skill_misses_when_a_section_is_missing(tmp_path: Path):
    store = _store(tmp_path)
    content_hash = "aa" * 32
    manifest = SubconsciousChunk(
        chunk_id=_section_chunk_id("acme/broken", content_hash, _MANIFEST_SECTION_INDEX),
        layer="procedural",
        content="skill_ref manifest skill:acme/broken hash:aabbcc sections:2",
        src="skill_ref",
        written_by="ncp:skill_cache:acme/broken",
        base_trust=0.6,
        conditions=["skill_ref", "skill:acme/broken", "manifest"],
        scope="global",
        zone="proven",
        expiry=9999999999.0,
    )
    store.write(manifest)
    only_section = SubconsciousChunk(
        chunk_id=_section_chunk_id("acme/broken", content_hash, 0),
        layer="procedural",
        content="Only the first section was ever written.",
        src="skill_ref",
        written_by="ncp:skill_cache:acme/broken",
        base_trust=0.6,
        conditions=["skill_ref", "skill:acme/broken", "section:0"],
        scope="global",
        zone="proven",
        expiry=9999999999.0,
    )
    store.write(only_section)

    assert fetch_skill("acme/broken", content_hash=content_hash, store=store) is None


def test_recall_skills_finds_cached_skill_content_and_excludes_other_sources(tmp_path: Path):
    store = _store(tmp_path)
    cache_skill(
        "The deploy API retry parameter defaults to three attempts with exponential backoff.",
        skill_id="acme/deploy-api",
        store=store,
    )
    store.write(
        SubconsciousChunk(
            layer="semantic",
            content="The deploy API retry parameter was mentioned by a tool result too.",
            src="tool_result",
            written_by="tester",
            base_trust=0.8,
            scope="global",
            zone="proven",
            expiry=9999999999.0,
        )
    )

    hits = recall_skills("deploy api retry parameter", store=store, k=5)

    assert hits
    assert all(chunk.src == "skill_ref" for chunk in hits)


def test_ordinary_working_zone_query_does_not_surface_cached_skill_content(tmp_path: Path):
    """Documents why recall_skills() exists as a separate call (see module docstring):
    the assembler's per-turn retrieval only ever queries zone="working", so
    zone="proven" skill_ref content is invisible to it without this helper.
    """
    store = _store(tmp_path)
    cache_skill(
        "The deploy API retry parameter defaults to three attempts.",
        skill_id="acme/deploy-api",
        store=store,
    )

    hits = store.query("deploy api retry parameter", k=5, scope="global")  # zone defaults to "working"

    assert hits == []


def test_oversized_unsplittable_content_falls_back_to_hard_split(tmp_path: Path):
    store = _store(tmp_path)
    # One giant "sentence" (no punctuation) well past the 2000-char chunk
    # ceiling -- chunk_prose's word-window fallback should still keep it
    # under budget, but exercise the defensive hard-split path too by using
    # a config with a large window so a single window can still land above
    # the hard content ceiling in principle.
    huge_word_blob = "x" * 5000
    result = cache_skill(huge_word_blob, skill_id="acme/huge", store=store)

    sections = store.get_chunks_by_ids(result.chunk_ids)
    assert all(len(chunk.content) <= 2000 for chunk in sections)


def test_base_trust_override(tmp_path: Path):
    store = _store(tmp_path)
    result = cache_skill("Short skill body.", skill_id="acme/short", store=store, base_trust=0.9)

    sections = store.get_chunks_by_ids(result.chunk_ids)
    assert all(chunk.base_trust == 0.9 for chunk in sections)


def test_skill_cache_config_defaults(tmp_path: Path):
    config = load_config(cwd=tmp_path)

    assert config.skill_cache_default_trust == 0.60
    assert config.skill_cache_default_expiry_days == 30
    assert config.skill_cache_window_chars == 1800


def test_ensure_cached_misses_on_first_call_then_hits_on_repeat(tmp_path: Path):
    store = _store(tmp_path)

    first = ensure_cached(WORKFLOW_CONTENT, skill_id="harness/workflow", store=store)
    assert first.cache_hit is False

    second = ensure_cached(WORKFLOW_CONTENT, skill_id="harness/workflow", store=store)
    assert second.cache_hit is True
    assert second.content_hash == first.content_hash


def test_ensure_cached_detects_changed_content_as_a_miss(tmp_path: Path):
    store = _store(tmp_path)
    first = ensure_cached(WORKFLOW_CONTENT, skill_id="harness/workflow", store=store)

    second = ensure_cached(WORKFLOW_CONTENT + " New step added.", skill_id="harness/workflow", store=store)

    assert second.cache_hit is False
    assert second.content_hash != first.content_hash


def test_ensure_cached_skips_the_write_path_on_a_hit(tmp_path: Path):
    """The optimization ensure_cached() exists for: a hit must not re-chunk/re-write."""
    store = _store(tmp_path)
    ensure_cached(WORKFLOW_CONTENT, skill_id="harness/workflow", store=store)

    with patch("ncp.skill_cache.cache_skill") as mock_cache_skill:
        result = ensure_cached(WORKFLOW_CONTENT, skill_id="harness/workflow", store=store)

    assert result.cache_hit is True
    mock_cache_skill.assert_not_called()


def test_ensure_cached_calls_cache_skill_on_a_miss(tmp_path: Path):
    store = _store(tmp_path)

    with patch("ncp.skill_cache.cache_skill", wraps=cache_skill) as mock_cache_skill:
        result = ensure_cached(WORKFLOW_CONTENT, skill_id="harness/workflow", store=store)

    assert result.cache_hit is False
    mock_cache_skill.assert_called_once()


def test_ensure_cached_hash_feeds_a_downstream_subagent_fetch(tmp_path: Path):
    """The DAG-payload pattern: orchestrator caches once, subagent fetches by the pinned hash."""
    store = _store(tmp_path)

    result = ensure_cached(WORKFLOW_CONTENT, skill_id="harness/workflow", store=store)
    fetched_by_subagent = fetch_skill("harness/workflow", content_hash=result.content_hash, store=store)

    assert fetched_by_subagent is not None
