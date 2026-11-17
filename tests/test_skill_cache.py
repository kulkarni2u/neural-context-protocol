import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ncp.config import load_config
from ncp.skill_cache import (
    _MANIFEST_SECTION_INDEX,
    _section_chunk_id,
    SKILL_AUTHOR_PREFIX,
    SKILL_CHUNK_ID_PREFIX,
    cache_skill,
    ensure_cached,
    fetch_skill,
    is_registered_skill,
    is_reserved_skill_identity,
    latest_skill,
    list_skills,
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

    assert fetched == WORKFLOW_CONTENT.strip()  # exact, byte-for-byte, not a rejoin approximation


def test_fetch_skill_preserves_markdown_structure_exactly(tmp_path: Path):
    """Regression test: cache_skill() used to route content through the
    retrieval chunker (ncp.chunker.chunk_content), which splits on sentence
    boundaries and rejoins with single spaces -- silently flattening
    markdown structure (headers glued onto prose, bullet lists collapsed
    onto one line). A skill file is markdown, not prose; fetch_skill() must
    return it exactly as cached, not an approximation.
    """
    store = _store(tmp_path)
    markdown_skill = (
        "# Deploy API Skill\n\n"
        "## Rules\n\n"
        "- Always set retries >= 3.\n"
        "- Never deploy on Fridays after 3pm.\n"
        "- Check staging first.\n\n"
        "```python\n"
        "def deploy(service, retries=3):\n"
        "    return api.post('/deploy', json={'service': service})\n"
        "```\n"
    ) * 30  # force multiple windows

    result = cache_skill(markdown_skill, skill_id="acme/deploy-api", store=store)
    fetched = fetch_skill("acme/deploy-api", content_hash=result.content_hash, store=store)

    assert fetched == markdown_skill.strip()
    assert "- Always set retries >= 3.\n- Never deploy on Fridays after 3pm." in fetched
    assert "## Rules\n\n-" in fetched


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


# --- kind / provenance / scope / validation / latest / list ---------------


def test_kind_defaults_to_reference_and_is_recorded_in_manifest(tmp_path: Path):
    store = _store(tmp_path)
    ref = cache_skill("Reference body.", skill_id="acme/ref", store=store)
    wf = cache_skill("Workflow body.", skill_id="acme/wf", store=store, kind="workflow")

    assert ref.kind == "reference"
    assert wf.kind == "workflow"
    assert latest_skill("acme/ref", store=store).kind == "reference"
    assert latest_skill("acme/wf", store=store).kind == "workflow"
    manifest = store.get_chunks_by_ids([_section_chunk_id("acme/wf", wf.content_hash, _MANIFEST_SECTION_INDEX)])[0]
    assert "kind:workflow" in manifest.conditions
    assert "kind:workflow" in manifest.content


def test_invalid_kind_and_registered_by_raise(tmp_path: Path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        cache_skill("x", skill_id="acme/x", store=store, kind="other")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        cache_skill("x", skill_id="acme/x", store=store, registered_by="agent")


def test_recall_skills_excludes_workflow_skills(tmp_path: Path):
    store = _store(tmp_path)
    text = "The deploy API retry parameter defaults to three attempts with exponential backoff."
    cache_skill(text, skill_id="acme/wf", store=store, kind="workflow")
    assert recall_skills("deploy api retry parameter", store=store, k=5) == []

    cache_skill(text + " Reference copy.", skill_id="acme/ref", store=store)
    hits = recall_skills("deploy api retry parameter", store=store, k=5)
    assert hits
    assert all("skill:acme/ref" in chunk.conditions for chunk in hits)


def test_user_registered_seeds_user_verified_trust(tmp_path: Path):
    store = _store(tmp_path)
    user = cache_skill("User skill.", skill_id="acme/user", store=store, registered_by="user")
    other = cache_skill("Other skill.", skill_id="acme/other", store=store)
    explicit = cache_skill("Explicit.", skill_id="acme/explicit", store=store, registered_by="user", base_trust=0.7)

    assert all(c.base_trust == 0.95 for c in store.get_chunks_by_ids(user.chunk_ids))
    assert all(c.base_trust == 0.6 for c in store.get_chunks_by_ids(other.chunk_ids))
    assert all(c.base_trust == 0.7 for c in store.get_chunks_by_ids(explicit.chunk_ids))
    assert is_registered_skill("acme/user", user.content_hash, store=store) is True
    assert is_registered_skill("acme/other", other.content_hash, store=store) is False
    assert latest_skill("acme/user", store=store).registered_by == "user"
    assert latest_skill("acme/other", store=store).registered_by is None


def test_is_registered_skill_requires_complete_version_and_real_author(tmp_path: Path):
    store = _store(tmp_path)
    result = cache_skill(WORKFLOW_CONTENT, skill_id="acme/user", store=store, registered_by="user")
    assert is_registered_skill("acme/user", result.content_hash, store=store) is True
    assert is_registered_skill("acme/user", "00" * 32, store=store) is False

    # Forged manifest claiming registration from a non-reserved author does not count.
    forged_hash = "bb" * 32
    store.write(
        SubconsciousChunk(
            chunk_id=_section_chunk_id("acme/forged", forged_hash, _MANIFEST_SECTION_INDEX),
            layer="procedural",
            content=f"skill_ref manifest skill:acme/forged hash:bb sections:0 kind:workflow registered_by:user chash:{forged_hash}",
            src="skill_ref",
            written_by="some_agent",
            base_trust=0.95,
            scope="global",
            zone="proven",
            expiry=9999999999.0,
        )
    )
    assert is_registered_skill("acme/forged", forged_hash, store=store) is False
    assert latest_skill("acme/forged", store=store) is None


def test_reserved_identity_helpers():
    assert is_reserved_skill_identity(written_by=f"{SKILL_AUTHOR_PREFIX}acme/x")
    assert is_reserved_skill_identity(chunk_id=f"{SKILL_CHUNK_ID_PREFIX}abc123")
    assert not is_reserved_skill_identity(written_by="agent", chunk_id="chunk_1")
    assert _section_chunk_id("acme/x", "h", 0).startswith(SKILL_CHUNK_ID_PREFIX)


def test_scope_defaults_to_pipeline_when_pipeline_id_given(tmp_path: Path):
    store = _store(tmp_path)
    result = cache_skill("Scoped skill.", skill_id="acme/scoped", store=store, pipeline_id="p1")

    assert result.scope == "pipeline"
    chunks = store.get_chunks_by_ids(result.chunk_ids)
    assert all(c.scope == "pipeline" and c.pipeline_id == "p1" for c in chunks)
    assert fetch_skill("acme/scoped", content_hash=result.content_hash, store=store, pipeline_id="p1") == "Scoped skill."
    # Invisible to other pipelines and to unscoped callers.
    assert fetch_skill("acme/scoped", content_hash=result.content_hash, store=store, pipeline_id="p2") is None
    assert fetch_skill("acme/scoped", content_hash=result.content_hash, store=store) is None
    assert latest_skill("acme/scoped", store=store, pipeline_id="p2") is None
    assert latest_skill("acme/scoped", store=store, pipeline_id="p1") is not None


def test_scope_global_flag_and_no_pipeline_keep_global(tmp_path: Path):
    store = _store(tmp_path)
    explicit = cache_skill("G one.", skill_id="acme/g1", store=store, pipeline_id="p1", scope_global=True)
    legacy = cache_skill("G two.", skill_id="acme/g2", store=store)

    assert explicit.scope == "global" and legacy.scope == "global"
    assert fetch_skill("acme/g1", content_hash=explicit.content_hash, store=store, pipeline_id="p2") == "G one."
    assert fetch_skill("acme/g2", content_hash=legacy.content_hash, store=store) == "G two."


def test_pipeline_scoped_and_global_copies_do_not_collide(tmp_path: Path):
    store = _store(tmp_path)
    scoped = cache_skill("Same.", skill_id="acme/same", store=store, pipeline_id="p1")
    glob = cache_skill("Same.", skill_id="acme/same", store=store, scope_global=True)

    assert scoped.content_hash == glob.content_hash
    assert set(scoped.chunk_ids).isdisjoint(glob.chunk_ids)


def test_recall_skills_includes_own_pipeline_scope(tmp_path: Path):
    store = _store(tmp_path)
    cache_skill("Pipeline private retry guidance for deploys.", skill_id="acme/priv", store=store, pipeline_id="p1")

    assert recall_skills("retry guidance deploys", store=store) == []
    assert recall_skills("retry guidance deploys", store=store, pipeline_id="p2") == []
    assert recall_skills("retry guidance deploys", store=store, pipeline_id="p1")


@pytest.mark.parametrize("bad", ["workflow", "acme/", "/x", "a/b/c", "a b/c", "acme/na!me", "", "../x"])
def test_skill_id_validation_rejects_malformed(tmp_path: Path, bad: str):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        cache_skill("x", skill_id=bad, store=store)
    with pytest.raises(ValueError):
        ensure_cached("x", skill_id=bad, store=store)


def test_skill_id_validation_accepts_allowed_characters(tmp_path: Path):
    store = _store(tmp_path)
    cache_skill("x", skill_id="Acme_1.x/dep-loy.v2", store=store)


def test_latest_skill_picks_newest_complete_version(tmp_path: Path):
    store = _store(tmp_path)
    assert latest_skill("acme/workflow", store=store) is None

    first = cache_skill(WORKFLOW_CONTENT, skill_id="acme/workflow", store=store)
    time.sleep(0.01)
    second = cache_skill(WORKFLOW_CONTENT + " Newer.", skill_id="acme/workflow", store=store, kind="workflow")

    info = latest_skill("acme/workflow", store=store)
    assert info.content_hash == second.content_hash
    assert info.section_count == second.section_count == info.sections
    assert info.kind == "workflow"
    assert info.created_at > 0 and info.age >= 0
    assert first.content_hash != info.content_hash


def test_latest_skill_skips_incomplete_newer_version(tmp_path: Path):
    store = _store(tmp_path)
    old = cache_skill(WORKFLOW_CONTENT, skill_id="acme/workflow", store=store)
    time.sleep(0.01)
    new = cache_skill(WORKFLOW_CONTENT + " Newer.", skill_id="acme/workflow", store=store)
    with store._connect() as connection:  # simulate a section aging out
        connection.execute("DELETE FROM chunks WHERE chunk_id = ?", (new.chunk_ids[0],))

    assert latest_skill("acme/workflow", store=store).content_hash == old.content_hash


def test_fetch_skill_without_hash_returns_latest(tmp_path: Path):
    store = _store(tmp_path)
    cache_skill(WORKFLOW_CONTENT, skill_id="acme/workflow", store=store)
    time.sleep(0.01)
    newer = WORKFLOW_CONTENT + " Newer rule."
    cache_skill(newer, skill_id="acme/workflow", store=store)

    assert fetch_skill("acme/workflow", store=store) == newer.strip()
    assert fetch_skill("acme/unknown", store=store) is None


def test_list_skills_returns_latest_version_per_skill(tmp_path: Path):
    store = _store(tmp_path)
    cache_skill("A one.", skill_id="acme/a", store=store)
    time.sleep(0.01)
    a2 = cache_skill("A two.", skill_id="acme/a", store=store, registered_by="user")
    cache_skill("B.", skill_id="acme/b", store=store, kind="workflow")
    cache_skill("Private.", skill_id="acme/p", store=store, pipeline_id="p1")

    entries = {e.skill_id: e for e in list_skills(store=store)}
    assert set(entries) == {"acme/a", "acme/b"}
    assert entries["acme/a"].content_hash == a2.content_hash
    assert entries["acme/a"].registered_by == "user"
    assert entries["acme/b"].kind == "workflow"
    assert entries["acme/b"].sections == 1
    assert entries["acme/b"].age >= 0

    with_pipeline = {e.skill_id for e in list_skills(store=store, pipeline_id="p1")}
    assert with_pipeline == {"acme/a", "acme/b", "acme/p"}


def test_ensure_cached_upgrades_to_user_registration(tmp_path: Path):
    store = _store(tmp_path)
    first = ensure_cached("Body.", skill_id="acme/up", store=store)
    again = ensure_cached("Body.", skill_id="acme/up", store=store, registered_by="user")
    third = ensure_cached("Body.", skill_id="acme/up", store=store, registered_by="user")

    assert (first.cache_hit, again.cache_hit, third.cache_hit) == (False, False, True)
    assert is_registered_skill("acme/up", first.content_hash, store=store)


def test_skill_cache_enabled_config_defaults_false(tmp_path: Path):
    assert load_config(cwd=tmp_path).skill_cache_enabled is False
