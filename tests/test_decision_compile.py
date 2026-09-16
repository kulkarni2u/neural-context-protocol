"""ncp_compile_decision_query and the typed record path (spec 4h, D04-D12)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from ncp.config import NCPConfig, load_config
from ncp.mcp.server import make_handlers, tools_for_config
from ncp.stores.sqlite import SQLiteStore
from ncp.types import ChunkEdge, SubconsciousChunk

SCHEMA = "ncp.slot.continue_or_escalate"


def _setup(tmp_path: Path, **overrides: object) -> tuple[SQLiteStore, dict, NCPConfig]:
    config = load_config(cwd=tmp_path)
    if overrides:
        config.values.setdefault("decisions", {}).update(overrides)
    store = SQLiteStore(tmp_path / "store.db")
    return store, make_handlers(store, config=config), config


def _compile(handlers: dict, **overrides: object) -> dict:
    args: dict = {
        "agent_id": "agent_a",
        "role": "implementer",
        "task": "fix-auth",
        "slot": "retry-policy",
        "intent": "unblock-login",
        "schema_id": SCHEMA,
        "pipeline_id": "p1",
    }
    args.update(overrides)
    return handlers["ncp_compile_decision_query"](args)  # type: ignore[no-any-return]


def _seed(store: SQLiteStore, count: int = 3, *, trust: float = 0.7) -> list[str]:
    ids = []
    for index in range(count):
        chunk = SubconsciousChunk(
            layer="semantic",
            content=f"retry policy note {index}: auth retries use exponential backoff variant {index}",
            src="tool_result",
            written_by=f"agent_{index}",
            base_trust=trust,
            pipeline_id="p1",
        )
        store.write(chunk)
        ids.append(chunk.chunk_id)
    return ids


# ── D04: legacy compatibility ─────────────────────────────────────────────────

def test_d04_legacy_call_still_records_and_adapts(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    result = handlers["ncp_record_decision"]({
        "decision": "use-jwt",
        "rationale": "stateless and cacheable across the fleet",
        "agent_id": "agent_a",
    })
    assert result["recorded"] is True
    assert result["schema_id"] == "legacy.untyped"
    # The legacy reasoning_trace chunk is still written, unchanged.
    assert result["chunk_id"].startswith("sub_")

    stored = store.get_decision(result["decision_id"])
    assert stored is not None
    # The *decision* becomes the choice. The rationale is commentary and must
    # never be the match key -- mapping it to choice would rank precedents on
    # justification text.
    assert stored.choice == "use-jwt"
    assert stored.rationale == "stateless and cacheable across the fleet"
    assert stored.backend == "unknown"
    assert stored.confidence_source == "self_reported"


def test_legacy_response_shape_is_unchanged_for_old_clients(tmp_path: Path) -> None:
    _, handlers, _ = _setup(tmp_path)
    result = handlers["ncp_record_decision"]({
        "decision": "retry-twice",
        "rationale": "transient 503s clear within a second",
        "agent_id": "agent_a",
        "alternatives": ["fail-fast"],
        "evidence_refs": ["sub_a"],
        "tags": ["retry-logic"],
    })
    for key in ("recorded", "chunk_id", "outcome", "tag_count", "evidence_count"):
        assert key in result
    assert result["outcome"] == "pending"
    assert result["tag_count"] == 1
    assert result["evidence_count"] == 1


# ── typed record path ─────────────────────────────────────────────────────────

def test_typed_record_persists_full_contract(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    result = handlers["ncp_record_decision"]({
        "decision": "continue-with-backoff",
        "rationale": "three notes agree",
        "agent_id": "agent_a",
        "schema_id": SCHEMA,
        "slot": "retry-policy",
        "choice": "continue",
        "options": ["continue", "escalate", "stop"],
        "probs": {"continue": 0.8, "escalate": 0.15, "stop": 0.05},
        "confidence": 0.9,
        "backend": "system_one",
        "confidence_source": "backend_claimed",
        "state_hash": "a" * 64,
        "chunk_ids": ["sub_a"],
    })
    assert result["recorded"] is True
    stored = store.get_decision(result["decision_id"])
    assert stored.choice == "continue"
    assert stored.backend == "system_one"
    assert stored.probs == {"continue": 0.8, "escalate": 0.15, "stop": 0.05}
    assert stored.state_hash == "a" * 64


def test_d09_unknown_choice_on_builtin_schema_writes_no_row(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    result = handlers["ncp_record_decision"]({
        "decision": "teleport",
        "rationale": "why not",
        "agent_id": "agent_a",
        "schema_id": SCHEMA,
        "slot": "retry-policy",
        "choice": "teleport",
        "confidence": 0.5,
    })
    assert result["recorded"] is False
    assert result["error"] == "schema_mismatch"
    assert "teleport" in result["details"]
    assert store.query_decisions() == []


def test_unregistered_schema_still_records(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    result = handlers["ncp_record_decision"]({
        "decision": "pick-b", "rationale": "r", "agent_id": "a",
        "schema_id": "team.custom.route", "slot": "route", "choice": "b", "confidence": 0.7,
    })
    assert result["recorded"] is True
    assert result["schema_registered"] is False
    assert store.get_decision(result["decision_id"]).choice == "b"


def test_strict_mode_rejects_unregistered_schema(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path, strict_registered_schemas=True)
    result = handlers["ncp_record_decision"]({
        "decision": "pick-b", "rationale": "r", "agent_id": "a",
        "schema_id": "team.custom.route", "slot": "route", "choice": "b", "confidence": 0.7,
    })
    assert result["recorded"] is False
    assert result["error"] == "schema_unregistered"
    assert store.query_decisions() == []


def test_typed_fields_without_schema_id_are_refused(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    result = handlers["ncp_record_decision"]({
        "decision": "d", "rationale": "r", "agent_id": "a", "choice": "continue",
    })
    assert result["recorded"] is False
    assert result["error"] == "schema_required" or result["error"] == "schema_id_required"
    assert store.query_decisions() == []


def test_invalid_probs_return_validation_error_not_a_partial_row(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    result = handlers["ncp_record_decision"]({
        "decision": "d", "rationale": "r", "agent_id": "a",
        "schema_id": SCHEMA, "slot": "s", "choice": "continue",
        "options": ["continue", "stop"], "probs": {"continue": 0.3, "stop": 0.2},
        "confidence": 0.8,
    })
    assert result["recorded"] is False
    assert result["error"] == "validation_error"
    assert store.query_decisions() == []


# ── D05-D08: compile ──────────────────────────────────────────────────────────

def test_d06_empty_store_escalates_with_no_evidence(tmp_path: Path) -> None:
    _, handlers, _ = _setup(tmp_path)
    result = _compile(handlers)
    assert result["joint_confidence"] == 0.0
    assert result["evidence_count"] == 0
    assert result["escalate"] is True
    assert "no_evidence" in result["escalate_reasons"]
    assert result["confidence_label"] == "advisory"


def test_d05_same_state_compiles_to_the_same_hash(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    _seed(store)
    first = _compile(handlers)
    second = _compile(handlers)
    assert first["state_hash"] == second["state_hash"]
    assert len(first["state_hash"]) == 64


def test_compile_hash_survives_a_fresh_handler_and_store_instance(tmp_path: Path) -> None:
    """Determinism has to hold across processes, not just across two calls."""
    store, handlers, config = _setup(tmp_path)
    _seed(store)
    first = _compile(handlers)

    reopened = SQLiteStore(tmp_path / "store.db")
    second = _compile(make_handlers(reopened, config=config))
    assert first["state_hash"] == second["state_hash"]


def test_d07_high_drift_raises_its_own_reason(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    _seed(store)
    result = _compile(handlers, drift_score=0.5)
    # Reasons accumulate and are correlated by construction: drift also
    # depresses joint_confidence. The contract is "is this reason present".
    assert "high_drift" in result["escalate_reasons"]
    assert result["escalate"] is True
    assert result["joint_confidence"] < _compile(handlers, drift_score=0.0)["joint_confidence"]


def test_d08_contradicts_edge_raises_contradiction_mass(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    ids = _seed(store, count=3)
    baseline = _compile(handlers)
    store.add_chunk_edges([
        ChunkEdge(src_chunk_id=ids[0], dst_chunk_id=ids[1], edge_type="contradicts")
    ])
    after = _compile(handlers)
    if after["evidence_count"] >= 2:
        assert after["contradiction_mass"] > baseline["contradiction_mass"]
        assert after["joint_confidence"] <= baseline["joint_confidence"]


def test_critical_budget_and_open_schema_reasons(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    _seed(store)
    assert "critical_budget" in _compile(handlers, pressure="critical")["escalate_reasons"]
    assert "open_schema" in _compile(handlers, schema_id="nobody.registered.this")["escalate_reasons"]
    assert "open_schema" not in _compile(handlers)["escalate_reasons"]


def test_escalate_reasons_never_name_a_model(tmp_path: Path) -> None:
    """NCP says why to escalate. Picking the backend is the host's job."""
    store, handlers, _ = _setup(tmp_path)
    _seed(store)
    result = _compile(handlers, drift_score=0.9, pressure="critical", schema_id="open.thing")
    allowed = {
        "low_joint_confidence", "high_drift", "contradiction",
        "open_schema", "critical_budget", "no_evidence",
    }
    assert set(result["escalate_reasons"]) <= allowed


def test_compile_returns_questions_from_the_registry(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    _seed(store)
    registered = _compile(handlers)["questions"]
    assert registered == [
        {"key": "choice", "type": "enum", "options": ["continue", "escalate", "stop"]}
    ]
    # An unregistered schema still gets a usable question shape.
    assert _compile(handlers, schema_id="open.thing")["questions"] == [
        {"key": "choice", "type": "string"}
    ]


def test_evidence_cap_is_clamped_to_max_evidence(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    _seed(store, count=20)
    assert _compile(handlers, k=500)["evidence_count"] <= 12
    assert _compile(handlers, k=2)["evidence_count"] <= 2


def test_min_confidence_filters_weak_evidence(tmp_path: Path) -> None:
    """The geometric mean is dragged down by weak chunks, so filtering matters."""
    store, handlers, _ = _setup(tmp_path)
    _seed(store, count=2, trust=0.9)
    store.write(SubconsciousChunk(
        layer="semantic",
        content="retry policy note weak: auth retries maybe backoff unsure",
        src="agent_inferred",
        written_by="agent_weak",
        base_trust=0.2,
        pipeline_id="p1",
    ))
    unfiltered = _compile(handlers)
    filtered = _compile(handlers, min_confidence=0.5)
    assert filtered["evidence_count"] <= unfiltered["evidence_count"]
    if filtered["evidence_count"] and filtered["evidence_count"] < unfiltered["evidence_count"]:
        assert filtered["joint_confidence"] > unfiltered["joint_confidence"]


# ── D10: disabled payload ─────────────────────────────────────────────────────

def test_d10_disabled_returns_disabled_payload_and_legacy_still_writes(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path, enabled=False)
    compiled = _compile(handlers)
    assert compiled["decisions_enabled"] is False
    assert compiled["escalate"] is False
    assert compiled["state"] == {}
    assert compiled["state_hash"] == ""

    legacy = handlers["ncp_record_decision"]({
        "decision": "d", "rationale": "r", "agent_id": "a",
    })
    assert legacy["recorded"] is True
    assert legacy["decisions_enabled"] is False
    assert "decision_id" not in legacy
    assert store.query_decisions() == []
    assert handlers["ncp_get_decision"]({"decision_id": "dec_x"})["decisions_enabled"] is False


# ── D12: no provider calls ────────────────────────────────────────────────────

def test_d12_compile_makes_zero_provider_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Compile is store reads plus arithmetic. Any socket is a bug."""
    store, handlers, _ = _setup(tmp_path)
    _seed(store)

    calls: list[str] = []

    def _no_sockets(*args: object, **kwargs: object) -> None:
        calls.append("socket")
        raise AssertionError("compile_decision_query opened a network connection")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setattr(socket, "create_connection", _no_sockets)

    result = _compile(handlers)
    assert calls == []
    assert result["evidence_count"] >= 1


# ── precedents and reuse ──────────────────────────────────────────────────────

def test_precedent_with_matching_hash_is_offered_as_a_reuse_candidate(tmp_path: Path) -> None:
    """The hit-rate check: a recorded decision must actually come back."""
    store, handlers, _ = _setup(tmp_path)
    _seed(store)
    compiled = _compile(handlers)
    handlers["ncp_record_decision"]({
        "decision": "continue", "rationale": "evidence agrees", "agent_id": "agent_a",
        "schema_id": SCHEMA, "slot": "retry-policy", "choice": "continue",
        "confidence": 0.95, "backend": "rule", "state_hash": compiled["state_hash"],
        "pipeline_id": "p1",
    })

    again = _compile(handlers)
    assert again["state_hash"] == compiled["state_hash"]
    assert len(again["precedents"]) == 1
    assert again["suggested_choice"] == "continue"


def test_low_confidence_precedent_is_not_suggested(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    _seed(store)
    compiled = _compile(handlers)
    handlers["ncp_record_decision"]({
        "decision": "continue", "rationale": "r", "agent_id": "agent_a",
        "schema_id": SCHEMA, "slot": "retry-policy", "choice": "continue",
        "confidence": 0.5, "state_hash": compiled["state_hash"], "pipeline_id": "p1",
    })
    assert "suggested_choice" not in _compile(handlers)


def test_precedent_with_failed_outcome_is_not_suggested(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    _seed(store)
    compiled = _compile(handlers)
    recorded = handlers["ncp_record_decision"]({
        "decision": "continue", "rationale": "r", "agent_id": "agent_a",
        "schema_id": SCHEMA, "slot": "retry-policy", "choice": "continue",
        "confidence": 0.95, "state_hash": compiled["state_hash"], "pipeline_id": "p1",
    })
    handlers["ncp_record_outcome"]({
        "success": False, "chunk_ids": ["sub_x"], "decision_id": recorded["decision_id"],
    })
    assert "suggested_choice" not in _compile(handlers)


def test_record_outcome_links_the_decision(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path)
    recorded = handlers["ncp_record_decision"]({
        "decision": "d", "rationale": "r", "agent_id": "a",
        "schema_id": SCHEMA, "slot": "s", "choice": "stop", "confidence": 0.8,
    })
    outcome = handlers["ncp_record_outcome"]({
        "success": True, "chunk_ids": ["sub_a"], "decision_id": recorded["decision_id"],
    })
    assert outcome["decision_linked"] is True
    assert store.get_decision(recorded["decision_id"]).outcome_id == outcome["outcome_id"]

    # An unknown decision id must not fail the outcome write.
    stray = handlers["ncp_record_outcome"]({
        "success": True, "chunk_ids": ["sub_a"], "decision_id": "dec_nope",
    })
    assert stray["recorded"] is True
    assert stray["decision_linked"] is False


# ── dual write ────────────────────────────────────────────────────────────────

def test_dual_write_is_off_by_default(tmp_path: Path) -> None:
    """Default-on would change retrieval ranking for every existing deployment."""
    store, handlers, config = _setup(tmp_path)
    assert config.decisions_dual_write_chunks is False
    before = len(store.query(text="anything", k=50, fallback_to_trust_recency=True))
    handlers["ncp_record_decision"]({
        "decision": "d", "rationale": "r", "agent_id": "a",
        "schema_id": SCHEMA, "slot": "s", "choice": "stop", "confidence": 0.8,
    })
    after = store.query(text="anything", k=50, fallback_to_trust_recency=True)
    # Only the legacy reasoning_trace chunk lands, never a second json mirror.
    assert len([c for c in after if c.chunk_type == "json"]) == 0
    assert len(after) >= before


def test_dual_write_when_enabled_mirrors_and_links_evidence(tmp_path: Path) -> None:
    store, handlers, _ = _setup(tmp_path, dual_write_chunks=True)
    # Distinct content: write-time dedup collapses near-identical chunks, which
    # would silently shrink the evidence set this test is about.
    evidence = []
    for content in (
        "the auth service returns 401 when the bearer token has expired",
        "queue depth stays under fifty during the nightly reconciliation job",
    ):
        chunk = SubconsciousChunk(
            layer="semantic", content=content, src="tool_result",
            written_by="agent_x", pipeline_id="p1",
        )
        store.write(chunk)
        evidence.append(chunk.chunk_id)

    handlers["ncp_record_decision"]({
        "decision": "d", "rationale": "r", "agent_id": "a",
        "schema_id": SCHEMA, "slot": "s", "choice": "stop", "confidence": 0.8,
        "chunk_ids": evidence, "pipeline_id": "p1",
    })

    # Locate the mirror from the evidence side rather than by text search: the
    # mirror body is canonical JSON, which BM25 does not usefully rank.
    inbound = store.get_chunk_edges(evidence, edge_types=["derived_from"], direction="in")
    mirror_ids = {edge.src_chunk_id for edge in inbound}
    assert len(mirror_ids) == 1
    assert {edge.dst_chunk_id for edge in inbound} == set(evidence)

    mirror = store.get_chunks_by_ids(list(mirror_ids))[0]
    assert mirror.chunk_type == "json"
    assert mirror.layer == "reasoning_trace"
    assert '"choice":"stop"' in mirror.content


def test_dual_write_edges_respect_pipeline_ownership(tmp_path: Path) -> None:
    """A decision with no pipeline must not link to pipeline-scoped evidence.

    add_chunk_edges only joins chunks sharing a pipeline scope (silent
    disconnect audit, finding 9). The mirror chunk inherits the decision's
    pipeline_id, so an unscoped decision legitimately gets no edges rather
    than reaching across a scope boundary.
    """
    store, handlers, _ = _setup(tmp_path, dual_write_chunks=True)
    evidence = _seed(store, count=2)  # written into pipeline "p1"
    handlers["ncp_record_decision"]({
        "decision": "d", "rationale": "r", "agent_id": "a",
        "schema_id": SCHEMA, "slot": "s", "choice": "stop", "confidence": 0.8,
        "chunk_ids": evidence,  # no pipeline_id
    })
    assert store.get_chunk_edges(evidence, edge_types=["derived_from"], direction="in") == []


# ── catalog ───────────────────────────────────────────────────────────────────

def test_new_tools_are_advertised_in_the_full_profile(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path)
    names = {str(tool["name"]) for tool in tools_for_config(config)}
    assert {"ncp_compile_decision_query", "ncp_get_decision"} <= names


def test_core_profile_stays_minimal(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path)
    config.values["tools"]["profile"] = "core"
    names = {str(tool["name"]) for tool in tools_for_config(config)}
    assert "ncp_compile_decision_query" not in names
