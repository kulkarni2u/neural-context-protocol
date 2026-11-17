"""R9: batch gen-0 decay skips user-registered skills (sqlite)."""

from __future__ import annotations

from pathlib import Path

from ncp.skill_cache import cache_skill
from ncp.stores.calibration import registered_skill_chunk_ids
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _age_all(store: SQLiteStore) -> None:
    with store._connect() as c:
        c.execute("UPDATE chunks SET created_at = 1.0")


def _trust(store: SQLiteStore, cid: str) -> float:
    with store._connect() as c:
        return float(c.execute("SELECT base_trust FROM chunks WHERE chunk_id=?", (cid,)).fetchone()[0])


def test_user_registered_skill_not_decayed_but_cached_skill_and_plain_chunks_are(tmp_path: Path) -> None:
    from ncp.config import load_config

    config = load_config(cwd=tmp_path, env={})
    store = SQLiteStore(tmp_path / "ncp.db")
    text = "# Deploy\n\n" + "step one do the thing\n" * 200  # several sections
    user = cache_skill(text, skill_id="acme/user-skill", store=store, config=config, registered_by="user", pipeline_id="p")
    plain = cache_skill(text + "x", skill_id="acme/plain-skill", store=store, config=config, pipeline_id="p")
    assert user.section_count > 1
    store.write(SubconsciousChunk(
        chunk_id="ordinary", layer="semantic", content="ordinary aged chunk", src="tool_result",
        pipeline_id="p", written_by="w", base_trust=0.9,
    ))
    _age_all(store)

    report = store.calibrate(pipeline_id="p")  # batch decay

    decayed = {c["chunk_id"] for c in report.change_log if c["reason"] == "batch_decay"}
    assert "ordinary" in decayed
    assert decayed.isdisjoint(user.chunk_ids)
    assert set(plain.chunk_ids) <= decayed
    for cid in user.chunk_ids:
        assert _trust(store, cid) == 0.95
    assert report.protected >= len(user.chunk_ids) + 1  # sections + manifest


def test_helper_pairs_sections_with_marked_manifest() -> None:
    rows = [
        ("m", ["skill_ref", "skill:a/b", "hash:h1", "manifest", "registered_by:user"]),
        ("s0", ["skill_ref", "skill:a/b", "hash:h1", "section:0"]),
        ("old", ["skill_ref", "skill:a/b", "hash:h0", "section:0"]),  # other version, unregistered
        ("other", '["skill_ref", "skill:c/d", "hash:h1", "section:0"]'),
    ]
    assert registered_skill_chunk_ids(rows) == {"m", "s0"}
    assert registered_skill_chunk_ids([("x", None), ("y", "not json")]) == set()
