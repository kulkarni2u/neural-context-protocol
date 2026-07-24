from pathlib import Path

from ncp.memory import compile_memory, improve, recall, remember
from ncp.stores.sqlite import SQLiteStore


def test_compile_memory_creates_source_and_atoms_with_edges():
    result = compile_memory(
        "Alice likes fast feedback. Bob owns release verification.",
        written_by="tester",
        pipeline_id="pipe_memory",
    )

    assert result.source_chunk.content == "Alice likes fast feedback. Bob owns release verification."
    assert result.source_chunk.layer == "episodic"
    assert result.source_chunk.src == "tool_result"
    assert len(result.atoms) == 2
    assert all(atom.layer == "semantic" for atom in result.atoms)
    assert all(atom.src == "synthesis" for atom in result.atoms)
    assert all(atom.source_refs == [result.source_chunk.chunk_id] for atom in result.atoms)
    assert all(edge.edge_type == "derived_from" for edge in result.edges)
    assert {edge.src_chunk_id for edge in result.edges} == {atom.chunk_id for atom in result.atoms}
    assert {edge.dst_chunk_id for edge in result.edges} == {result.source_chunk.chunk_id}


def test_compile_memory_deduplicates_and_caps_atoms():
    result = compile_memory(
        "Same fact. Same fact. Another fact. Third fact.",
        max_atoms=2,
    )

    assert [atom.content for atom in result.atoms] == ["Same fact.", "Another fact."]


def test_remember_persists_atoms_and_recall_returns_semantic_memory(tmp_path: Path):
    store = SQLiteStore(tmp_path / "ncp.db")

    result = remember(
        "Trial users need ACH retry guards. Release checks must run before publish.",
        store=store,
        pipeline_id="pipe_memory",
        written_by="tester",
    )
    hits = recall("ACH retry guards", store=store, pipeline_id="pipe_memory")

    assert result.source_chunk.chunk_id
    assert [hit.content for hit in hits][:1] == ["Trial users need ACH retry guards."]


def test_improve_runs_consolidation_and_reports_counts(tmp_path: Path):
    store = SQLiteStore(tmp_path / "ncp.db")
    remember("Alpha fact. Beta fact.", store=store, pipeline_id="pipe_memory")

    result = improve(store=store, pipeline_id="pipe_memory")

    assert result["improved"] is True
    assert result["consolidated_groups"] >= 0
