"""Typed decision contract: record, hash, registry, migration (spec 4h, D01-D12)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ncp.decisions import (
    BUILTIN_SCHEMAS,
    SchemaRegistry,
    compute_state_hash,
    contradiction_mass,
    joint_confidence,
)
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ConsciousBlock, DecisionRecord, SubconsciousChunk


def _store(tmp_path: Path) -> SQLiteStore:
    return SQLiteStore(tmp_path / ".ncp" / "store.db")


def _conscious(**overrides: object) -> ConsciousBlock:
    base: dict = {
        "agent_id": "agent_a",
        "role": "implementer",
        "owns": ["auth"],
        "must_not": ["deploy"],
        "task": "fix-auth",
        "slot": "retry-policy",
        "intent": "unblock-login",
    }
    base.update(overrides)
    return ConsciousBlock(**base)


def _decision(**overrides: object) -> DecisionRecord:
    base: dict = {
        "schema_id": "ncp.slot.continue_or_escalate",
        "slot": "retry-policy",
        "choice": "continue",
        "confidence": 0.9,
    }
    base.update(overrides)
    return DecisionRecord(**base)


# ── D01-D03: type validation ──────────────────────────────────────────────────

def test_d01_valid_enum_decision_records_and_reads_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    decision = _decision(
        options=["continue", "escalate", "stop"],
        probs={"continue": 0.7, "escalate": 0.2, "stop": 0.1},
        backend="system_one",
        confidence_source="backend_claimed",
        state_hash="f" * 64,
        chunk_ids=["sub_a", "sub_b"],
    )
    assert store.record_decision_record(decision) is True
    fetched = store.get_decision(decision.decision_id)
    assert fetched is not None
    assert fetched.canonical_json() == decision.canonical_json()


def test_d02_confidence_outside_unit_range_rejected() -> None:
    with pytest.raises(ValueError, match="confidence"):
        _decision(confidence=1.2)
    with pytest.raises(ValueError, match="confidence"):
        _decision(confidence=-0.01)


def test_d03_probs_must_sum_when_options_enumerated() -> None:
    with pytest.raises(ValueError, match="sum to 1.0"):
        _decision(options=["continue", "stop"], probs={"continue": 0.3, "stop": 0.2})
    # Within tolerance is accepted.
    _decision(options=["continue", "stop"], probs={"continue": 0.51, "stop": 0.48})
    # Probs keys must come from options.
    with pytest.raises(ValueError, match="not in options"):
        _decision(options=["continue"], probs={"continue": 0.5, "ghost": 0.5})
    # Without options, free-form probs are allowed (open scalar decisions).
    _decision(probs={"anything": 0.42})


def test_schema_id_and_slot_reject_whitespace_and_empty() -> None:
    with pytest.raises(ValueError, match="whitespace"):
        _decision(schema_id="has space")
    with pytest.raises(ValueError, match="whitespace"):
        _decision(slot="has space")
    with pytest.raises(ValueError, match="empty"):
        _decision(schema_id="")


def test_rationale_is_bounded_and_never_the_match_key() -> None:
    _decision(rationale="x" * 600)
    with pytest.raises(ValueError, match="600"):
        _decision(rationale="x" * 601)


def test_choice_preserves_python_type_through_the_store(tmp_path: Path) -> None:
    """A bool choice must not come back as the string 'True'."""
    store = _store(tmp_path)
    for choice in (True, False, 3, 3.5, "continue", {"action": "retry", "after_s": 5}):
        decision = _decision(schema_id="open.scalar", choice=choice)
        store.record_decision_record(decision)
        fetched = store.get_decision(decision.decision_id)
        assert fetched is not None
        assert fetched.choice == choice
        assert type(fetched.choice) is type(choice)


# ── state hash ────────────────────────────────────────────────────────────────

def test_d05_state_hash_is_stable_across_instances_and_orderings() -> None:
    conscious = _conscious(tried=["a", "b"], failed=["x"])
    reordered = _conscious(tried=["b", "a"], failed=["x"])
    first = compute_state_hash(
        schema_id="s.id", slot="retry-policy", conscious=conscious, chunk_ids=["c2", "c1"]
    )
    second = compute_state_hash(
        schema_id="s.id", slot="retry-policy", conscious=reordered, chunk_ids=["c1", "c2"]
    )
    assert first == second
    assert len(first) == 64


def test_state_hash_excludes_continuously_moving_fields() -> None:
    """Scores, trust and drift move every turn; including them makes a hash that never matches."""
    stable = compute_state_hash(
        schema_id="s.id", slot="pick", conscious=_conscious(), chunk_ids=["c1"]
    )
    for moving in (
        {"drift_score": 0.9},
        {"slot_confidence": 0.1},
        {"slot_age": 99},
        {"ctx_used_ratio": 0.8},
        {"pressure": "critical"},
        {"agent_id": "someone_else"},
        {"pipeline_id": "other_pipe"},
    ):
        assert compute_state_hash(
            schema_id="s.id", slot="pick", conscious=_conscious(**moving), chunk_ids=["c1"]
        ) == stable, f"{moving} must not change the hash"


def test_state_hash_changes_on_real_state_change() -> None:
    stable = compute_state_hash(
        schema_id="s.id", slot="pick", conscious=_conscious(), chunk_ids=["c1"]
    )
    assert compute_state_hash(
        schema_id="s.id", slot="pick", conscious=_conscious(), chunk_ids=["c1", "c2"]
    ) != stable
    assert compute_state_hash(
        schema_id="other.id", slot="pick", conscious=_conscious(), chunk_ids=["c1"]
    ) != stable
    assert compute_state_hash(
        schema_id="s.id", slot="pick", conscious=_conscious(task="different"), chunk_ids=["c1"]
    ) != stable


# ── advisory scores ───────────────────────────────────────────────────────────

def test_joint_confidence_zero_without_evidence() -> None:
    assert joint_confidence(confidences=[], contradiction_mass=0.0, drift_score=0.0) == 0.0


def test_joint_confidence_penalties_are_monotonic() -> None:
    base = joint_confidence(confidences=[0.8, 0.8], contradiction_mass=0.0, drift_score=0.0)
    assert base == pytest.approx(0.8)
    assert joint_confidence(confidences=[0.8, 0.8], contradiction_mass=1.0, drift_score=0.0) < base
    assert joint_confidence(confidences=[0.8, 0.8], contradiction_mass=0.0, drift_score=0.5) < base
    assert joint_confidence(confidences=[0.8, 0.8], contradiction_mass=0.0, drift_score=1.0) == 0.0


def test_contradiction_mass_is_measured_over_chunks_not_pairs() -> None:
    """One contradiction among six chunks reads 0.33, not 1/15."""
    ids = ["a", "b", "c", "d", "e", "f"]
    assert contradiction_mass(chunk_ids=ids, contradiction_pairs=[("a", "b")]) == pytest.approx(1 / 3)
    assert contradiction_mass(chunk_ids=ids, contradiction_pairs=[]) == 0.0
    assert contradiction_mass(chunk_ids=[], contradiction_pairs=[("a", "b")]) == 0.0
    # Pairs naming chunks that were not injected are ignored.
    assert contradiction_mass(chunk_ids=ids, contradiction_pairs=[("a", "zz")]) == 0.0
    assert contradiction_mass(chunk_ids=ids, contradiction_pairs=[("a", "a")]) == 0.0


# ── registry (D09) ────────────────────────────────────────────────────────────

def test_builtin_schemas_are_registered() -> None:
    registry = SchemaRegistry.load()
    for schema in BUILTIN_SCHEMAS:
        assert registry.is_registered(schema.schema_id)
    assert registry.get("ncp.slot.continue_or_escalate").options == (
        "continue",
        "escalate",
        "stop",
    )


def test_d09_registered_enum_rejects_unknown_choice() -> None:
    registry = SchemaRegistry.load()
    assert registry.validate_choice("ncp.slot.continue_or_escalate", "teleport") is not None
    assert registry.validate_choice("ncp.slot.continue_or_escalate", "stop") is None


def test_registry_type_checks_are_strict_about_bools() -> None:
    registry = SchemaRegistry.load()
    assert registry.validate_choice("ncp.slot.binary", True) is None
    assert registry.validate_choice("ncp.slot.binary", "true") is not None
    # A bool is not a number, despite Python's int subclassing.
    inline = {"m.num": {"choice_type": "number"}}
    numeric = SchemaRegistry.load(inline=inline)
    assert numeric.validate_choice("m.num", 3.5) is None
    assert numeric.validate_choice("m.num", True) is not None


def test_unregistered_schema_is_not_a_mismatch() -> None:
    assert SchemaRegistry.load().validate_choice("nobody.registered.this", "whatever") is None


def test_registry_overlay_order_file_then_inline(tmp_path: Path) -> None:
    (tmp_path / ".ncp").mkdir(parents=True)
    (tmp_path / ".ncp" / "decision_schemas.json").write_text(
        json.dumps({"team.route": {"choice_type": "enum", "options": ["a", "b"]}}),
        encoding="utf-8",
    )
    from_file = SchemaRegistry.load(project_root=tmp_path)
    assert from_file.get("team.route").options == ("a", "b")

    overridden = SchemaRegistry.load(
        project_root=tmp_path,
        inline={"team.route": {"choice_type": "enum", "options": ["a", "b", "c"]}},
    )
    assert overridden.get("team.route").options == ("a", "b", "c")


def test_malformed_registry_entries_are_skipped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / ".ncp").mkdir(parents=True)
    (tmp_path / ".ncp" / "decision_schemas.json").write_text("not json at all", encoding="utf-8")
    registry = SchemaRegistry.load(
        project_root=tmp_path,
        inline={
            "bad.enum_without_options": {"choice_type": "enum"},
            "bad.unknown_type": {"choice_type": "hologram"},
            "bad.not_a_dict": "nope",
            "good.one": {"choice_type": "string"},
        },
    )
    assert registry.is_registered("good.one")
    for bad in ("bad.enum_without_options", "bad.unknown_type", "bad.not_a_dict"):
        assert not registry.is_registered(bad)
    # Built-ins survive a broken overlay.
    assert registry.is_registered("ncp.slot.binary")


# ── store behavior ────────────────────────────────────────────────────────────

def test_query_decisions_ranks_exact_state_hash_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    older = _decision(state_hash="a" * 64, confidence=0.9, created_at=1000.0)
    newer = _decision(state_hash="b" * 64, confidence=0.95, created_at=2000.0)
    store.record_decision_record(older)
    store.record_decision_record(newer)

    ranked = store.query_decisions(
        schema_id=older.schema_id, slot=older.slot, state_hash="a" * 64, k=5
    )
    assert ranked[0].decision_id == older.decision_id
    # Without a hash, recency leads.
    assert store.query_decisions(schema_id=older.schema_id, slot=older.slot, k=5)[0].decision_id == (
        newer.decision_id
    )


def test_query_decisions_filters(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_decision_record(_decision(confidence=0.4, backend="rule", pipeline_id="p1"))
    store.record_decision_record(_decision(confidence=0.95, backend="human", pipeline_id="p2"))

    assert len(store.query_decisions(min_confidence=0.5)) == 1
    assert len(store.query_decisions(backend="rule")) == 1
    assert len(store.query_decisions(pipeline_id="p2")) == 1
    assert len(store.query_decisions(slot="nonexistent-slot")) == 0


def test_link_decision_outcome(tmp_path: Path) -> None:
    store = _store(tmp_path)
    decision = _decision()
    store.record_decision_record(decision)
    assert store.link_decision_outcome(decision.decision_id, "out_1") is True
    assert store.get_decision(decision.decision_id).outcome_id == "out_1"
    assert store.link_decision_outcome("dec_does_not_exist", "out_2") is False


def test_get_decision_returns_none_for_unknown_id(tmp_path: Path) -> None:
    assert _store(tmp_path).get_decision("dec_nope") is None


# ── D11: migration ────────────────────────────────────────────────────────────

def test_d11_existing_store_migrates_without_data_loss(tmp_path: Path) -> None:
    """A database created before the decisions table opens and keeps its chunks."""
    path = tmp_path / ".ncp" / "store.db"
    store = _store(tmp_path)
    store.write(
        SubconsciousChunk(
            chunk_id="sub_pre_migration",
            layer="semantic",
            content="a fact written before the decisions table existed",
            src="tool_result",
        )
    )

    # Simulate the pre-migration shape by dropping the new table entirely.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE IF EXISTS decisions")
        connection.commit()

    reopened = SQLiteStore(path)
    assert [c.chunk_id for c in reopened.get_chunks_by_ids(["sub_pre_migration"])] == [
        "sub_pre_migration"
    ]
    decision = _decision()
    assert reopened.record_decision_record(decision) is True
    assert reopened.get_decision(decision.decision_id) is not None


def test_empty_store_applies_decisions_schema(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.query_decisions() == []


# ── pgvector parity guard (runs without a live database) ──────────────────────

def test_pgvector_decision_sql_arity_matches_params() -> None:
    """Catches a column/placeholder mismatch without needing a live Postgres."""
    from ncp.stores.pgvector import PgvectorStore

    columns = PgvectorStore._DECISION_COLUMNS.count(",") + 1
    params = PgvectorStore._decision_params(_decision())
    assert columns == len(params) == 18

    where, filter_params = PgvectorStore._decision_filters(
        schema_id="a", slot="b", pipeline_id="c", backend="rule", min_confidence=0.5
    )
    assert where.count("%s") == len(filter_params)

    # The positional row reader must consume exactly what _DECISION_COLUMNS selects.
    original = _decision(choice=True, state_hash="c" * 64, chunk_ids=["x"])
    rebuilt = PgvectorStore._row_to_decision(list(PgvectorStore._decision_params(original)))
    assert rebuilt.canonical_json() == original.canonical_json()


def test_pgvector_schema_template_and_migration_agree() -> None:
    """A fresh install and a migrated install must converge on the same table."""
    from ncp.stores.pgvector import PGVECTOR_SCHEMA_TEMPLATE

    rendered = PGVECTOR_SCHEMA_TEMPLATE.format(schema="ncp", prefix="ncp_")
    migration = Path("ncp/migrations/014_add_decisions_table.sql").read_text(encoding="utf-8")
    up = migration.split("-- DOWN")[0].replace("{schema}", "ncp").replace("{prefix}", "ncp_")

    assert "ncp.ncp_decisions" in rendered
    assert "ncp.ncp_decisions" in up
    for column in ("schema_id", "state_hash", "confidence_source", "backend", "outcome_id"):
        assert column in rendered and column in up


# ── CLI ───────────────────────────────────────────────────────────────────────

def test_precedents_cli_typed_filters(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from ncp.cli import main

    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    store.record_decision_record(
        _decision(choice="continue", confidence=0.92, backend="rule", state_hash="a" * 64)
    )
    store.record_decision_record(
        DecisionRecord(
            schema_id="team.route", slot="routing", choice="fast",
            confidence=0.40, backend="human",
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["precedents", "--cwd", str(tmp_path), "--schema-id", "ncp.slot.continue_or_escalate",
         "--json-output"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["count"] == 1
    assert payload["decisions"][0]["choice"] == "continue"

    # min_confidence alone also selects the typed path.
    filtered = runner.invoke(
        main, ["precedents", "--cwd", str(tmp_path), "--min-confidence", "0.9", "--json-output"]
    )
    assert json.loads(filtered.output)["count"] == 1

    # Backend filter.
    human = runner.invoke(
        main, ["precedents", "--cwd", str(tmp_path), "--backend", "human", "--json-output"]
    )
    assert json.loads(human.output)["decisions"][0]["slot"] == "routing"


def test_precedents_cli_requires_a_query_or_a_typed_filter(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from ncp.cli import main

    result = CliRunner().invoke(main, ["precedents", "--cwd", str(tmp_path)])
    assert result.exit_code != 0
    assert "typed filter" in result.output


def test_precedents_cli_text_query_still_works(tmp_path: Path) -> None:
    """The legacy text path must keep working untouched."""
    from click.testing import CliRunner

    from ncp.cli import main

    store = SQLiteStore(tmp_path / ".ncp" / "store.db")
    store.write(
        SubconsciousChunk(
            layer="reasoning_trace",
            content="decision: apply null guard\nrationale: retryCount is null for ACH\noutcome: succeeded",
            src="agent_inferred",
        )
    )
    result = CliRunner().invoke(
        main, ["precedents", "null guard", "--cwd", str(tmp_path), "--json-output"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["count"] >= 1
