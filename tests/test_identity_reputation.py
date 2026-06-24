"""Tests for agent identity and reputation rollup."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.identity import identity_id_for_public_key, store_secret_key
from ncp.stores.calibration import ReputationUpdate
from ncp.stores.calibration import FeedbackRow, rollup_reputation
from ncp.stores.pgvector import PgvectorStore
from ncp.stores.sqlite import SQLiteStore
from ncp.types import SubconsciousChunk


def _chunk(
    chunk_id: str,
    content: str,
    *,
    written_by: str,
    pipeline_id: str | None = "pipe_rep",
    base_trust: float = 0.6,
) -> SubconsciousChunk:
    return SubconsciousChunk(
        chunk_id=chunk_id,
        content=content,
        layer="semantic",
        src="agent_inferred",
        written_by=written_by,
        pipeline_id=pipeline_id,
        base_trust=base_trust,
    )


def _fetch_reputation(db_path: Path, identity_id: str) -> sqlite3.Row | None:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT identity_id, alpha, beta, obs_count, last_updated FROM reputation WHERE identity_id = ?",
            (identity_id,),
        ).fetchone()
    finally:
        connection.close()


def test_identity_id_for_public_key_is_cli_safe() -> None:
    identity_id = identity_id_for_public_key(b"\xfb" * 32)

    assert len(identity_id) == 16
    assert identity_id[0].isalnum()
    assert all(ch.isalnum() for ch in identity_id)


def test_rollup_reputation_positive_chunk_raises_alpha() -> None:
    updates = rollup_reputation(
        [{"chunk_id": "c1", "old_trust": 0.5, "new_trust": 0.7, "reason": "retrieval_feedback"}],
        {"c1": "agent_a"},
        {},
        gain=4.0,
        forget=0.99,
    )

    assert len(updates) == 1
    assert updates[0].identity_id == "agent_a"
    assert updates[0].new_alpha == pytest.approx(1.8)
    assert updates[0].new_beta == pytest.approx(1.0)
    assert updates[0].obs_delta == 1


def test_rollup_reputation_negative_chunk_raises_beta() -> None:
    updates = rollup_reputation(
        [{"chunk_id": "c1", "old_trust": 0.8, "new_trust": 0.5, "reason": "dissent"}],
        {"c1": "agent_a"},
        {},
        gain=4.0,
        forget=0.99,
    )

    assert updates[0].new_alpha == pytest.approx(1.0)
    assert updates[0].new_beta == pytest.approx(2.2)


def test_rollup_reputation_propagation_credits_parent_author() -> None:
    updates = rollup_reputation(
        [
            {
                "chunk_id": "parent",
                "old_trust": 0.4,
                "new_trust": 0.6,
                "reason": "trust_propagation",
            }
        ],
        {"parent": "parent_author", "child": "child_author"},
        {},
        gain=4.0,
        forget=0.99,
    )

    assert [update.identity_id for update in updates] == ["parent_author"]


def test_rollup_reputation_forgets_toward_prior_without_crossing_floor() -> None:
    updates = rollup_reputation(
        [{"chunk_id": "c1", "old_trust": 0.5, "new_trust": 0.6, "reason": "retrieval_feedback"}],
        {"c1": "agent_a"},
        {"agent_a": (11.0, 0.2)},
        gain=4.0,
        forget=0.5,
    )

    assert updates[0].new_alpha == pytest.approx(6.4)
    assert updates[0].new_beta == pytest.approx(1.0)


def test_rollup_reputation_mixed_feedback_contributes_positive_and_negative_mass() -> None:
    updates = rollup_reputation(
        [
            {"chunk_id": "c1", "old_trust": 0.5, "new_trust": 0.7, "reason": "retrieval_feedback"},
            {"chunk_id": "c1", "old_trust": 0.7, "new_trust": 0.6, "reason": "dissent"},
        ],
        {"c1": "agent_a"},
        {},
        gain=4.0,
        forget=0.99,
    )

    assert updates[0].new_alpha == pytest.approx(1.8)
    assert updates[0].new_beta == pytest.approx(1.4)
    assert updates[0].obs_delta == 2


def test_feedback_compute_and_rollup_are_backend_neutral() -> None:
    rows = [
        FeedbackRow(
            chunk_id="c1",
            base_trust=0.6,
            retrieval_count=5,
        )
    ]
    from ncp.stores.calibration import compute_feedback_updates

    sqlite_result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5)
    pgvector_result = compute_feedback_updates(rows, feedback_weight=0.15, propagation_factor=0.5)

    assert sqlite_result == pgvector_result
    assert rollup_reputation(sqlite_result.change_log, {"c1": "agent_a"}, {}, gain=4.0, forget=0.99) == (
        rollup_reputation(pgvector_result.change_log, {"c1": "agent_a"}, {}, gain=4.0, forget=0.99)
    )


def test_sqlite_feedback_rolls_up_author_reputation(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("rep1", "retrieved reputation evidence", written_by="agent_a"))
    for _ in range(5):
        store.query("retrieved reputation", k=4, min_score=0.0, pipeline_id="pipe_rep")

    report = store.calibrate(pipeline_id="pipe_rep", feedback_mode=True, feedback_weight=0.15)

    row = _fetch_reputation(store.path, "agent_a")
    assert row is not None
    assert row["alpha"] > 1.0
    assert row["beta"] == pytest.approx(1.0)
    assert row["obs_count"] == 1
    assert any(entry.get("reason") == "reputation_rollup" for entry in report.change_log)


def test_sqlite_feedback_reputation_dry_run_does_not_write(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("rep_dry", "dry reputation evidence", written_by="agent_a"))
    for _ in range(3):
        store.query("dry reputation", k=4, min_score=0.0, pipeline_id="pipe_rep")

    report = store.calibrate(pipeline_id="pipe_rep", feedback_mode=True, dry_run=True)

    assert any(entry.get("reason") == "reputation_rollup" for entry in report.change_log)
    assert _fetch_reputation(store.path, "agent_a") is None


def test_sqlite_feedback_reputation_is_atomic_with_trust_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "store.db")
    store.write(_chunk("rep_atomic", "atomic reputation evidence", written_by="agent_a"))
    for _ in range(5):
        store.query("atomic reputation", k=4, min_score=0.0, pipeline_id="pipe_rep")

    def fail_upsert(*_: object, **__: object) -> None:
        raise sqlite3.Error("forced reputation failure")

    monkeypatch.setattr(SQLiteStore, "_upsert_reputation_updates", fail_upsert)

    with pytest.raises(Exception, match="forced reputation failure"):
        store.calibrate(pipeline_id="pipe_rep", feedback_mode=True)

    connection = sqlite3.connect(store.path)
    try:
        trust = connection.execute(
            "SELECT base_trust FROM chunks WHERE chunk_id = 'rep_atomic'"
        ).fetchone()[0]
        rep_count = connection.execute("SELECT COUNT(*) FROM reputation").fetchone()[0]
    finally:
        connection.close()
    assert trust == pytest.approx(0.6)
    assert rep_count == 0


def test_identity_create_list_revoke_and_reputation_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    env = {"NCP_KEYSTORE_DIR": str(tmp_path / "keys")}

    create_result = runner.invoke(
        main,
        ["identity", "create", "--cwd", str(tmp_path), "--label", "claude"],
        env=env,
    )
    assert create_result.exit_code == 0, create_result.output
    identity_id = create_result.output.strip().splitlines()[-1].strip()
    assert len(identity_id) == 16
    assert (tmp_path / "keys" / f"{identity_id}.key").exists()

    list_result = runner.invoke(main, ["identity", "list", "--cwd", str(tmp_path)], env=env)
    assert list_result.exit_code == 0
    assert identity_id in list_result.output
    assert "claude" in list_result.output

    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    store.write(_chunk("cli_rep", "cli reputation evidence", written_by=identity_id))
    for _ in range(5):
        store.query("cli reputation", k=4, min_score=0.0, pipeline_id="pipe_rep")
    store.calibrate(pipeline_id="pipe_rep", feedback_mode=True)

    rep_result = runner.invoke(
        main,
        ["reputation", "--cwd", str(tmp_path), "--json-output"],
        env=env,
    )
    assert rep_result.exit_code == 0, rep_result.output
    payload = json.loads(rep_result.output)
    assert payload[0]["identity_id"] == identity_id
    assert payload[0]["label"] == "claude"
    assert payload[0]["score"] > 0.5
    assert payload[0]["confidence"] > 0.0

    revoke_result = runner.invoke(
        main,
        ["identity", "revoke", identity_id, "--cwd", str(tmp_path)],
        env=env,
    )
    assert revoke_result.exit_code == 0, revoke_result.output


def test_store_secret_key_restricts_keystore_dir_permissions(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"

    path = store_secret_key("identity123", b"secret", keystore_dir=key_dir)

    assert path.exists()
    assert os.stat(key_dir).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_identity_create_removes_secret_when_db_registration_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingStore:
        def register_identity(self, **_: object) -> None:
            raise RuntimeError("db unavailable")

    monkeypatch.setattr("ncp.cli._resolve_runtime_store", lambda _config: FailingStore())
    monkeypatch.setattr(
        "ncp.identity.generate_ed25519_identity",
        lambda: ("identity123456789", "public", b"secret"),
    )
    runner = CliRunner()
    runner.invoke(main, ["init", "--cwd", str(tmp_path)])
    key_dir = tmp_path / "keys"

    result = runner.invoke(
        main,
        ["identity", "create", "--cwd", str(tmp_path), "--label", "claude"],
        env={"NCP_KEYSTORE_DIR": str(key_dir)},
    )

    assert result.exit_code != 0
    assert not list(key_dir.glob("*.key"))


def test_pgvector_reputation_upsert_applies_evidence_to_current_row() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            self.calls.append((sql, params))

        def close(self) -> None:
            pass

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_obj = FakeCursor()

        def cursor(self) -> FakeCursor:
            return self.cursor_obj

    store = object.__new__(PgvectorStore)
    store.schema = "ncp"
    store.table_prefix = "ncp_"
    store.reputation_forget = 0.99
    connection = FakeConnection()
    update = ReputationUpdate(
        identity_id="agent_a",
        new_alpha=6.4,
        new_beta=1.0,
        obs_delta=1,
        positive_evidence=0.4,
        negative_evidence=0.0,
    )

    store._upsert_reputation_updates(connection, (update,), now=123.0)

    sql, params = connection.cursor_obj.calls[0]
    assert "alpha = 1.0 + (rep.alpha - 1.0) * %s + (EXCLUDED.alpha - 1.0)" in sql
    assert "beta = 1.0 + (rep.beta - 1.0) * %s + (EXCLUDED.beta - 1.0)" in sql
    assert params == ("agent_a", 1.4, 1.0, 1, 123.0, 0.99, 0.99)
