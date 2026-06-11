from pathlib import Path

from ncp.config import load_config
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _chunk(chunk_id: str, *, base_trust: float = 0.7, pipeline_id: str | None = "pipe_1", **kwargs: object) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        layer="episodic",
        content=f"zzz_{chunk_id}_zzz " + "_".join(chunk_id * 3 for _ in range(8)),
        src="tool_result",
        pipeline_id=pipeline_id,
        base_trust=base_trust,
        **kwargs,
    )


def test_retention_disabled_by_default_evicts_nothing(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")

    for i in range(20):
        assert store.write(_chunk(f"sub_{i}")) is True

    chunks = store.get_working_zone(pipeline_id="pipe_1")
    assert len(chunks) == 20
    assert store.retention_evictions == 0


def test_retention_evicts_lowest_scored_overflow(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db", max_working_chunks_per_pipeline=5)

    for i in range(8):
        # Higher index => higher trust => should survive eviction.
        assert store.write(_chunk(f"sub_{i}", base_trust=0.1 + i * 0.1)) is True

    chunks = store.get_working_zone(pipeline_id="pipe_1")
    surviving_ids = {chunk.chunk_id for chunk in chunks}

    assert len(chunks) == 5
    assert surviving_ids == {"sub_3", "sub_4", "sub_5", "sub_6", "sub_7"}
    assert store.retention_evictions == 3


def test_retention_never_evicts_proven_zone_chunks(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db", max_working_chunks_per_pipeline=2)

    assert store.write(
        _chunk("sub_proven", base_trust=0.01, zone="proven", expiry=9999999999.0)
    ) is True

    for i in range(4):
        assert store.write(_chunk(f"sub_working_{i}", base_trust=0.5 + i * 0.1)) is True

    working_chunks = store.get_working_zone(pipeline_id="pipe_1")
    assert len(working_chunks) == 2
    assert store.retention_evictions == 2

    with store._connect() as connection:
        row = connection.execute(
            "SELECT chunk_id FROM chunks WHERE chunk_id = ?", ("sub_proven",)
        ).fetchone()
    assert row is not None


def test_retention_is_scoped_per_pipeline(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db", max_working_chunks_per_pipeline=3)

    for i in range(5):
        assert store.write(_chunk(f"sub_a_{i}", base_trust=0.2 + i * 0.1, pipeline_id="pipe_a")) is True
    for i in range(2):
        assert store.write(_chunk(f"sub_b_{i}", base_trust=0.5, pipeline_id="pipe_b")) is True

    pipe_a_chunks = store.get_working_zone(pipeline_id="pipe_a")
    pipe_b_chunks = store.get_working_zone(pipeline_id="pipe_b")

    assert len(pipe_a_chunks) == 3
    assert len(pipe_b_chunks) == 2
    assert store.retention_evictions == 2


def test_retention_config_default_is_zero_and_reads_configured_value(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)

    default_config = load_config(cwd=project)
    assert default_config.retention_max_working_chunks_per_pipeline == 0

    (project / ".ncp").mkdir()
    (project / ".ncp" / "config.toml").write_text(
        "[retention]\nmax_working_chunks_per_pipeline = 5000\n"
    )

    configured = load_config(cwd=project)
    assert configured.retention_max_working_chunks_per_pipeline == 5000
