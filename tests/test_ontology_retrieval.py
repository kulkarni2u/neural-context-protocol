"""Ontology PR 4: ontology-aware retrieval and the automatic trigger policy.

Covers the §6.4 acceptance table (disabled byte-for-byte, auto no-entity,
auto entity + intent, always on an empty graph, ambiguity, timeout fallback,
auth fail-closed, superseded/future evidence), token caps, provenance and
dedup, plus the PR 4 policy fixes and PR 3 review follow-ups.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ncp.api import get_context as api_get_context
from ncp.assembler import Assembler
from ncp.config import load_config
from ncp.mcp.server import _handle_request, make_handlers, tools_for_config
from ncp.ontology import facade as facade_module
from ncp.ontology.enrichment import (
    OntologyAuthError,
    OntologyEnricher,
    OntologyRequest,
    OntologyUnavailableError,
    build_enricher,
)
from ncp.ontology.facade import OntologyFacade, cached_facade
from ncp.ontology.models import AuthContext, Entity, Relation, Scope
from ncp.ontology.policy import OntologyConfig
from ncp.ontology.ranking import WEIGHTS, PathCandidate, rank_paths
from ncp.ontology.redaction import REDACTED, redact_attributes
from ncp.ontology.resolution import candidate_terms
from ncp.ontology.serializer import HEADER
from ncp.ontology.stores.sqlite import OntologyTimeoutError, SQLiteOntologyStore
from ncp.ontology.trigger import evaluate
from ncp.ontology.resolution import Resolution
from ncp.principals import Principal, ToolAuthorizer, hash_token
from ncp.stores.sqlite import SQLiteStore
from ncp.tokens import estimate_tokens
from ncp.types import BudgetContext, ConsciousBlock, SubconsciousChunk

NOW = datetime.now(timezone.utc)
OBSERVED = (NOW - timedelta(days=1)).isoformat()
# Generous time budgets keep the tests independent of machine load; the
# timeout paths are exercised explicitly in TestFailures.
ENABLED = "[ontology]\nenabled = true\ntrigger_max_ms = 1000\ngraph_timeout_ms = 5000\n"
VERIFIER = 'unauthenticated_capabilities = ["read", "write", "verify"]\n'


# ── fixtures ────────────────────────────────────────────────────────────────


def _project(tmp_path: Path, toml: str | None) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".ncp").mkdir()
    if toml is not None:
        (root / ".ncp" / "config.toml").write_text(toml, encoding="utf-8")
    return root


def _auth(namespace: str = "p1", caps: set[str] | None = None, principal: str = "local") -> AuthContext:
    return AuthContext(
        principal_id=principal, scope=Scope(tenant_id="default", namespace=namespace),
        capabilities=frozenset(caps or {"read", "write", "verify"}),
    )


class World:
    """An NCP SQLite store with an ontology: payroll consumes payroll-events
    (verified, chunk evidence), payroll depends on ledger (verified)."""

    def __init__(self, root: Path, toml: str = ENABLED + VERIFIER) -> None:
        (root / ".ncp" / "config.toml").write_text(toml, encoding="utf-8")
        self.root = root
        self.config = load_config(cwd=root)
        self.store = SQLiteStore(root / ".ncp" / "store.db")
        self.facade = OntologyFacade.from_config(self.config, self.store)
        assert self.facade.store is not None
        self.ontology: SQLiteOntologyStore = self.facade.store

    def chunk(self, content: str, *, pipeline: str = "p1", **extra: object) -> str:
        chunk = SubconsciousChunk(layer="semantic", content=content, src="tool_result",
                                  pipeline_id=pipeline, **extra)
        assert self.store.write(chunk)
        return chunk.chunk_id

    def entity(self, entity_type: str, key: str, *, aliases: tuple[str, ...] = (),
               namespace: str = "p1", **attributes: object) -> Entity:
        required = {"Service": {"name": key, "environment": "prod"},
                    "KafkaTopic": {"cluster": "main", "name": key},
                    "Database": {"name": key}, "Team": {"name": key}}[entity_type]
        entity = Entity(scope=Scope(tenant_id="default", namespace=namespace),
                        entity_type=entity_type, canonical_key=key, display_name=key,
                        aliases=aliases, attributes={**required, **attributes},
                        schema_pack="infrastructure")
        caps = {"read", "write", "verify", "admin"} if namespace == "global" else None
        return self.ontology.upsert_entity(entity, auth=_auth(namespace, caps))

    def relate(self, src: Entity, dst: Entity, relation_type: str, *, evidence: list[str] | None = None,
               status: str = "verified", key: str | None = None, auth: AuthContext | None = None,
               valid_from: datetime | None = None, external: bool = False, **extra: object) -> Relation:
        refs = [{"evidence_type": "chunk", "evidence_id": chunk_id, "source_revision": "1",
                 "observed_at": OBSERVED} for chunk_id in evidence or []]
        if external:
            refs.append({"evidence_type": "external_attestation", "evidence_id": "cmdb",
                         "source_revision": "1", "observed_at": OBSERVED})
        body: dict[str, object] = {
            "src_entity_id": str(src.entity_id), "dst_entity_id": str(dst.entity_id),
            "relation_type": relation_type, "status": status, "confidence": 0.9,
            "observed_at": OBSERVED, "evidence": refs, **extra,
        }
        if valid_from is not None:
            body["valid_from"] = valid_from.isoformat()
        result = self.facade.assert_relation(
            auth or _auth(src.scope.namespace), body,
            idempotency_key=key or f"{src.canonical_key}-{relation_type}-{dst.canonical_key}",
        )
        return Relation.model_validate(result["relation"])

    def payroll(self) -> dict[str, object]:
        events_chunk = self.chunk("payroll consumes payroll-events from kafka")
        ledger_chunk = self.chunk("payroll reads the ledger database")
        payroll = self.entity("Service", "payroll", aliases=("pay",))
        topic = self.entity("KafkaTopic", "payroll-events")
        ledger = self.entity("Database", "ledger")
        consumes = self.relate(payroll, topic, "CONSUMES", evidence=[events_chunk])
        depends = self.relate(payroll, ledger, "DEPENDS_ON", evidence=[ledger_chunk])
        return {"payroll": payroll, "topic": topic, "ledger": ledger, "consumes": consumes,
                "depends": depends, "events_chunk": events_chunk, "ledger_chunk": ledger_chunk}

    def handlers(self, toml: str | None = None) -> dict:
        config = self.config if toml is None else load_config(cwd=self.root)
        return make_handlers(self.store, config=config)

    def get_context(self, **args: object) -> dict:
        handlers = make_handlers(self.store, config=self.config)
        payload = {"agent_id": "a1", "role": "build", "pipeline_id": "p1", "task": "idle",
                   "slot": "x", "intent": "advance", **args}
        return handlers["ncp_get_context"](payload)  # type: ignore[return-value]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(_project(tmp_path, None))


def _call(handlers: dict, tools: list, name: str, args: dict, *, principal=None, authorizer=None) -> dict:
    return json.loads(_handle_request(  # type: ignore[arg-type]
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": args}},
        handlers, tools=tools, principal=principal, authorizer=authorizer,
    ))


# ── disabled / off: byte-for-byte legacy output ──────────────────────────────


FIXED_ARGS = {
    "agent_id": "a1", "role": "build", "pipeline_id": "p1", "task": "fix_payroll_consumer_lag",
    "slot": "payroll", "intent": "why_is_payroll_lagging", "max_tokens": 400,
    "ontology": {"mode": "always"},
}
# Response fields that legitimately vary between two runs (clocks, ids).
_VOLATILE = ("session_id",)


def _seed_memory(store: SQLiteStore) -> None:
    for index, text in enumerate(("payroll consumer lag spikes at noon",
                                  "payroll-events topic has 12 partitions",
                                  "ledger database is read by payroll")):
        chunk = SubconsciousChunk(chunk_id=f"fixed-{index}", layer="semantic", content=text,
                                  src="tool_result", pipeline_id="p1", created_at=1_700_000_000.0 + index)
        assert store.write(chunk)


def _legacy_response(root: Path, toml: str | None) -> dict:
    if toml is not None:
        (root / ".ncp" / "config.toml").write_text(toml, encoding="utf-8")
    config = load_config(cwd=root)
    store = SQLiteStore(root / ".ncp" / "store.db")
    response = make_handlers(store, config=config)["ncp_get_context"](dict(FIXED_ARGS))
    assert isinstance(response, dict)
    return response


PRE_CHANGE_CONTEXT = (
    "[NCP:CONSCIOUS]\n"
    "id:a1 role:build ncp_v:1.0\n"
    "task:fix_payroll_consumer_lag\n"
    "slot:payroll\n"
    "intent:why_is_payroll_lagging\n"
    "\n"
    "[NCP:SUBCONSCIOUS]\n"
    "chunk:fixed-2 layer:semantic score:0.9 src:tool_result trust:0.7\n"
    "  ledger database is read by payroll\n"
    "chunk:fixed-1 layer:semantic score:0.9 src:tool_result trust:0.7\n"
    "  payroll-events topic has 12 partitions\n"
    "\n"
    "[NCP:BUDGET] ctx_used:0.0 steps:0/? elapsed:0s pressure:low"
)


class TestDisabledIsLegacy:
    @pytest.mark.parametrize("toml", [
        "[ontology]\nenabled = false\n",
        '[ontology]\nenabled = true\nretrieval_mode = "off"\n',
        '[ontology]\nenabled = true\nprovider = "disabled"\n',
    ])
    def test_get_context_bytes_match_no_ontology_config(self, tmp_path: Path, toml: str) -> None:
        root = _project(tmp_path, None)
        _seed_memory(SQLiteStore(root / ".ncp" / "store.db"))
        # A populated ontology in the same store must not leak into context.
        World(root).payroll()
        baseline = _legacy_response(root, "")  # no [ontology] section at all
        subject = _legacy_response(root, toml)
        assert "ontology" not in subject
        assert subject["context"] == baseline["context"]
        assert HEADER not in subject["context"]
        strip = {key: value for key, value in baseline.items() if key not in _VOLATILE}
        assert json.dumps({k: v for k, v in subject.items() if k not in _VOLATILE}, sort_keys=True) == (
            json.dumps(strip, sort_keys=True)
        )

    def test_fixed_fixture_matches_pre_change_golden(self, tmp_path: Path) -> None:
        """Golden output captured by running the pre-PR-4 code on this fixture."""
        root = _project(tmp_path, "[ontology]\nenabled = false\n")
        _seed_memory(SQLiteStore(root / ".ncp" / "store.db"))
        response = _legacy_response(root, None)
        assert response["context"] == PRE_CHANGE_CONTEXT
        assert sorted(response) == [
            "budget_tokens", "complexity_signal", "context", "factors", "pending_whisper_ids",
            "retrieved_chunk_ids", "session_id", "telemetry", "tier_hint",
        ]
        assert response["retrieved_chunk_ids"] == ["fixed-2", "fixed-1"]

    def test_request_opt_out_is_legacy(self, world: World) -> None:
        world.payroll()
        args = {**FIXED_ARGS, "ontology": False}
        opted = world.get_context(**args)
        assert "ontology" not in opted and HEADER not in opted["context"]
        mode_off = world.get_context(**{**FIXED_ARGS, "ontology": {"mode": "off"}})
        assert "ontology" not in mode_off and mode_off["context"] == opted["context"]

    def test_disabled_tool_schema_unchanged_and_enabled_adds_ontology_arg(self, tmp_path: Path) -> None:
        off = tools_for_config(load_config(cwd=_project(tmp_path / "a", None)))
        on = tools_for_config(load_config(cwd=_project(tmp_path / "b", ENABLED)))
        off_ctx = next(tool for tool in off if tool["name"] == "ncp_get_context")
        on_ctx = next(tool for tool in on if tool["name"] == "ncp_get_context")
        assert "ontology" not in off_ctx["inputSchema"]["properties"]
        assert "ontology" in on_ctx["inputSchema"]["properties"]
        on_props = dict(on_ctx["inputSchema"]["properties"])
        on_props.pop("ontology")
        assert on_props == off_ctx["inputSchema"]["properties"]

    def test_api_get_context_disabled_is_legacy(self, tmp_path: Path) -> None:
        root = _project(tmp_path, "[ontology]\nenabled = false\n")
        store = SQLiteStore(root / ".ncp" / "store.db")
        _seed_memory(store)
        config = load_config(cwd=root)
        agent = ConsciousBlock(owns=[], must_not=[], agent_id="a1", role="build", task="payroll", slot="lag",
                               intent="advance", pipeline_id="p1")
        legacy = Assembler(store=store, config=config).assemble(
            conscious=agent, budget=BudgetContext(), max_tokens=400,
        ).context
        assert api_get_context(agent=agent, store=store, config=config, max_tokens=400,
                               ontology={"mode": "always"}) == legacy


# ── trigger decisions (§6.4) ─────────────────────────────────────────────────


class TestTrigger:
    def test_auto_entity_with_relationship_intent_expands(self, world: World) -> None:
        facts = world.payroll()
        response = world.get_context(task="fix_payroll_consumer_lag", slot="kafka", max_tokens=800)
        meta = response["ontology"]
        assert meta["ontology_triggered"] is True
        assert meta["reason_code"] == "entity_relationship_intent"
        assert meta["verified_path_count"] >= 2
        assert meta["graph_tokens"] > 0 and meta["fallback_reason"] is None
        assert HEADER in response["context"]
        assert "Service:payroll -CONSUMES-> KafkaTopic:payroll-events" in response["context"]
        assert f"ev:{facts['events_chunk']}" in response["context"]
        # Ontology block sits after memory, before the budget header.
        context = response["context"]
        assert context.index(HEADER) < context.index("[NCP:BUDGET]")
        for key in ("candidate_count", "latency_ms", "graph_tokens", "verified_path_count"):
            assert key in meta

    def test_auto_no_entity_is_ordinary_context(self, world: World) -> None:
        world.payroll()
        baseline = world.get_context(task="summarize_yesterday", slot="notes", ontology=False)
        response = world.get_context(task="summarize_yesterday", slot="notes")
        assert response["ontology"]["ontology_triggered"] is False
        assert response["ontology"]["reason_code"] == "no_entity"
        assert response["context"] == baseline["context"]

    def test_entity_without_intent_does_not_expand(self, world: World) -> None:
        world.payroll()
        meta = world.get_context(task="payroll", slot="notes")["ontology"]
        assert meta["reason_code"] == "entity_without_intent" and not meta["ontology_triggered"]

    def test_type_word_alone_never_triggers(self, world: World) -> None:
        world.payroll()
        meta = world.get_context(task="which_service_depends_on_what", slot="x")["ontology"]
        assert meta["reason_code"] == "no_entity"

    def test_diagnostic_intent_counts_as_second_signal(self, world: World) -> None:
        world.payroll()
        meta = world.get_context(task="why_is_payroll_slow", slot="x")["ontology"]
        assert meta["reason_code"] == "entity_diagnostic_intent" and meta["ontology_triggered"]

    def test_ambiguous_alias_is_never_guessed(self, world: World) -> None:
        world.payroll()
        world.entity("Service", "payments", aliases=("pay",))
        meta = world.get_context(task="what_depends_on_pay", slot="x")["ontology"]
        assert meta["reason_code"] == "ambiguous_entity"
        assert meta["ontology_triggered"] is False and meta["candidate_count"] == 2
        assert meta["paths"] == []

    def test_always_on_empty_graph_returns_ordinary_context(self, tmp_path: Path) -> None:
        root = _project(tmp_path, None)
        world = World(root, ENABLED + 'retrieval_mode = "always"\n')
        baseline = world.get_context(task="fix_payroll_consumer_lag", slot="x", ontology=False)
        response = world.get_context(task="fix_payroll_consumer_lag", slot="x")
        assert response["ontology"]["reason_code"] == "always_no_seed"
        assert response["context"] == baseline["context"]

    def test_always_expands_without_intent(self, tmp_path: Path) -> None:
        world = World(_project(tmp_path, None), ENABLED + VERIFIER + 'retrieval_mode = "always"\n')
        world.payroll()
        meta = world.get_context(task="payroll", slot="notes")["ontology"]
        assert meta["reason_code"] == "mode_always" and meta["verified_path_count"] >= 1

    def test_mode_override_requires_admin(self, world: World) -> None:
        world.payroll()
        meta = world.get_context(task="payroll", slot="notes", ontology={"mode": "always"})["ontology"]
        assert meta["mode"] == "auto"
        assert any("admin" in warning for warning in meta["warnings"])
        admin = OntologyConfig(enabled=True, unauthenticated_capabilities=("admin", "read", "write"))
        enricher = build_enricher(admin, world.store, request={"mode": "always"})
        assert enricher is not None and enricher.mode == "always"

    def test_invalid_ontology_argument_is_rejected_when_enabled(self, world: World) -> None:
        with pytest.raises(ValueError):
            world.get_context(task="x", ontology={"mode": "sometimes"})
        with pytest.raises(ValueError):
            OntologyRequest.parse({"max_hops": 9})

    def test_pure_decision_function(self) -> None:
        entity = Entity(scope=Scope(tenant_id="t", namespace="n"), entity_type="Service",
                        canonical_key="payroll", display_name="payroll",
                        attributes={"name": "payroll", "environment": "prod"},
                        schema_pack="infrastructure")
        resolution = Resolution(resolved={entity.entity_id: "payroll"})
        common = {"resolution": resolution, "entities": {str(entity.entity_id): entity},
                  "entity_types": {"Service", "KafkaTopic"}, "relation_types": {"CONSUMES"}}
        assert evaluate(mode="auto", texts=["payroll upstream"], **common).should_expand
        assert not evaluate(mode="auto", texts=["payroll service"], **common).should_expand
        supported = evaluate(mode="auto", texts=["payroll service"],
                             memory_texts=["the payroll job"], **common)
        assert supported.should_expand and supported.reason_code == "entity_supporting_signals"

    def test_candidate_terms_cover_slug_and_hyphen_spellings(self) -> None:
        terms, ids = candidate_terms(["fix_payroll_events_lag"])
        assert {"payroll", "payroll events", "payroll-events", "payroll_events"} <= set(terms)
        assert ids == []


# ── expansion, provenance, dedup, token caps ─────────────────────────────────


class TestExpansion:
    def test_candidate_relations_never_enter_automatic_context(self, world: World) -> None:
        payroll = world.entity("Service", "payroll")
        topic = world.entity("KafkaTopic", "payroll-events")
        world.relate(payroll, topic, "CONSUMES", status="candidate")
        meta = world.get_context(task="payroll_consumer_lag", slot="x")["ontology"]
        assert meta["ontology_triggered"] and meta["verified_path_count"] == 0

    def test_superseded_evidence_excludes_path(self, world: World) -> None:
        facts = world.payroll()
        replacement = SubconsciousChunk(layer="semantic", content="payroll moved off kafka",
                                        src="tool_result", pipeline_id="p1")
        assert world.store.write(replacement)
        world.store.supersede(facts["events_chunk"], replacement.chunk_id)
        response = world.get_context(task="payroll_consumer_lag", slot="x", max_tokens=800)
        assert "-CONSUMES->" not in response["context"]
        relations = {rel for path in response["ontology"]["paths"] for rel in path["relations"]}
        assert "CONSUMES" not in relations and "DEPENDS_ON" in relations

    def test_future_chunk_evidence_is_not_live(self, world: World) -> None:
        live_chunk = world.chunk("payroll reads ledger")
        payroll = world.entity("Service", "payroll")
        ledger = world.entity("Database", "ledger")
        world.relate(payroll, ledger, "DEPENDS_ON", evidence=[live_chunk])
        with closing(sqlite3.connect(world.store.path)) as connection:
            connection.execute("UPDATE chunks SET valid_from = ? WHERE chunk_id = ?",
                               (time.time() + 3600, live_chunk))
            connection.commit()
        auth = _auth()
        assert world.ontology.live_chunk_evidence([live_chunk], auth=auth, relation_namespace="p1") == set()
        meta = world.get_context(task="payroll_upstream", slot="x")["ontology"]
        assert meta["verified_path_count"] == 0

    def test_evidence_in_another_pipeline_is_not_cited(self, world: World) -> None:
        foreign = world.chunk("payroll consumes payroll-events", pipeline="p2")
        auth = _auth()
        assert world.ontology.live_chunk_evidence([foreign], auth=auth, relation_namespace="p1") == set()

    def test_cross_pipeline_graph_is_invisible(self, world: World) -> None:
        world.payroll()
        meta = world.get_context(task="payroll_consumer_lag", slot="x", pipeline_id="p2")["ontology"]
        assert meta["reason_code"] == "no_entity" and meta["paths"] == []

    def test_global_entities_and_external_attestations(self, world: World) -> None:
        team = world.entity("Team", "platform", namespace="global")
        service = world.entity("Service", "billing", namespace="global")
        world.relate(team, service, "OWNS", external=True,
                     auth=_auth("global", {"read", "write", "verify", "admin"}))
        response = world.get_context(task="who_owns_billing", slot="x")
        meta = response["ontology"]
        assert meta["verified_path_count"] == 1
        assert meta["paths"][0]["external_evidence_count"] == 1
        assert "Service:billing <-OWNS- Team:platform att:1" in response["context"]

    def test_graph_tokens_respect_slice_and_total_budget(self, tmp_path: Path) -> None:
        world = World(_project(tmp_path, None), ENABLED + VERIFIER + "max_graph_tokens = 60\n")
        facts = world.payroll()
        for index in range(6):
            topic = world.entity("KafkaTopic", f"payroll-out-{index}")
            world.relate(facts["payroll"], topic, "PRODUCES", evidence=[facts["events_chunk"]])
        response = world.get_context(task="payroll_consumer_lag", slot="x", max_tokens=300)
        meta = response["ontology"]
        assert 0 < meta["graph_tokens"] <= 60
        assert meta["omitted"].get("token_budget", 0) >= 1
        assert estimate_tokens(response["context"]) <= 300

    def test_graph_never_evicts_conscious_or_budget_header(self, world: World) -> None:
        world.payroll()
        response = world.get_context(task="payroll_consumer_lag", slot="x", max_tokens=60)
        context = response["context"]
        assert context.startswith("[NCP:CONSCIOUS]") and "[NCP:BUDGET]" in context
        assert response["ontology"]["graph_tokens"] == 0 or estimate_tokens(context) <= 60

    def test_stream_and_response_refit_keep_graph_bounded(self, world: World) -> None:
        world.payroll()
        streamed = world.get_context(task="payroll_consumer_lag", slot="x", stream=True)
        labels = [label for label, _ in streamed.sections]
        assert "ontology" in labels and labels.index("ontology") < labels.index("budget_header")
        assert streamed.handler_result["ontology"]["verified_path_count"] >= 1
        refit = world.get_context(task="payroll_consumer_lag", slot="x", max_tokens=800,
                                  response_max_tokens=300)
        assert "ontology" in refit
        assert refit["context"].startswith("[NCP:CONSCIOUS]")

    def test_graph_paths_are_not_credited_as_retrieved_evidence(self, world: World) -> None:
        facts = world.payroll()
        response = world.get_context(task="payroll_consumer_lag", slot="x", max_tokens=800)
        cited = {cid for path in response["ontology"]["paths"] for cid in path["evidence_ids"]}
        assert facts["events_chunk"] in cited
        assert all(not str(cid).startswith("ontology") for cid in response["retrieved_chunk_ids"])

    def test_automatic_context_has_no_attributes(self, world: World) -> None:
        events_chunk = world.chunk("payroll consumes events")
        payroll = world.entity("Service", "payroll", api_token="hunter2")
        topic = world.entity("KafkaTopic", "payroll-events")
        world.relate(payroll, topic, "CONSUMES", evidence=[events_chunk], attributes={"note": "zzsecretzz"})
        response = world.get_context(task="payroll_consumer_lag", slot="x", max_tokens=800)
        dumped = json.dumps(response)
        assert "hunter2" not in dumped and "zzsecretzz" not in dumped and "prod" not in response["context"]

    def test_ranking_is_deterministic_and_dedups_context_evidence(self) -> None:
        scope = Scope(tenant_id="t", namespace="n")

        def entity(entity_type: str, key: str) -> Entity:
            attrs = {"Service": {"name": key, "environment": "prod"}, "Database": {"name": key}}
            return Entity(scope=scope, entity_type=entity_type, canonical_key=key, display_name=key,
                          attributes=attrs[entity_type], schema_pack="infrastructure")

        def relation(src: Entity, dst: Entity, rid: str) -> Relation:
            return Relation(relation_id=f"00000000-0000-0000-0000-00000000000{rid}", scope=scope,
                            src_entity_id=src.entity_id, dst_entity_id=dst.entity_id,
                            relation_type="DEPENDS_ON", status="verified", confidence=0.9,
                            valid_from=NOW - timedelta(days=2), observed_at=NOW - timedelta(days=1),
                            created_by="x", schema_pack="infrastructure",
                            evidence=({"evidence_type": "chunk", "evidence_id": f"c{rid}",
                                       "source_revision": "1", "observed_at": NOW},))

        payroll, ledger, cache = entity("Service", "payroll"), entity("Database", "ledger"), entity("Database", "cache")
        a = PathCandidate((payroll, ledger), (relation(payroll, ledger, "1"),), (("c1",),), (0,))
        b = PathCandidate((payroll, cache), (relation(payroll, cache, "2"),), (("c2",),), (0,))
        first = rank_paths([a, b], query_terms=["payroll"], context_chunk_ids={"c1"}, now=NOW)
        again = rank_paths([b, a], query_terms=["payroll"], context_chunk_ids={"c1"}, now=NOW)
        assert [r.candidate.relation_ids for r in first] == [r.candidate.relation_ids for r in again]
        # c1 is already in memory context: the path citing it loses diversity.
        assert first[0].candidate is b
        assert first[1].components["diversity"] == 0.0 and first[0].components["diversity"] == 1.0
        assert set(first[0].components) == set(WEIGHTS)
        assert rank_paths([a, a], query_terms=[], now=NOW)[0].candidate is a
        assert len(rank_paths([a, a], query_terms=[], now=NOW)) == 1


# ── failure handling ─────────────────────────────────────────────────────────


class TestFailures:
    def test_graph_timeout_degrades_to_ordinary_context(self, world: World, monkeypatch) -> None:
        world.payroll()
        baseline = world.get_context(task="payroll_consumer_lag", slot="x", ontology=False)

        def slow(*args, **kwargs):
            raise OntologyTimeoutError("ontology read exceeded its deadline")

        monkeypatch.setattr(SQLiteOntologyStore, "traverse", slow)
        response = world.get_context(task="payroll_consumer_lag", slot="x")
        meta = response["ontology"]
        assert meta["fallback_reason"] == "graph_timeout" and meta["paths"] == []
        assert response["context"] == baseline["context"]

    def test_trigger_timeout_degrades(self, world: World, monkeypatch) -> None:
        world.payroll()

        def slow(*args, **kwargs):
            raise OntologyTimeoutError("ontology read exceeded its deadline")

        monkeypatch.setattr(SQLiteOntologyStore, "resolve_terms", slow)
        meta = world.get_context(task="payroll_consumer_lag", slot="x")["ontology"]
        assert meta["reason_code"] == "trigger_timeout" and meta["fallback_reason"] == "trigger_timeout"
        assert meta["ontology_triggered"] is False

    def test_real_deadline_interrupts_traversal(self, world: World) -> None:
        facts = world.payroll()
        from ncp.ontology.models import GraphQuery

        with pytest.raises(OntologyTimeoutError):
            world.ontology.traverse(GraphQuery(seeds=(facts["payroll"].entity_id,)), auth=_auth(),
                                    deadline=time.monotonic() - 1)

    def test_fail_mode_closed_raises(self, tmp_path: Path, monkeypatch) -> None:
        world = World(_project(tmp_path, None), ENABLED + VERIFIER + 'fail_mode = "closed"\n')
        world.payroll()
        monkeypatch.setattr(SQLiteOntologyStore, "traverse",
                            lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
        with pytest.raises(OntologyUnavailableError):
            world.get_context(task="payroll_consumer_lag", slot="x")

    def test_backend_error_falls_back_with_reason(self, world: World, monkeypatch) -> None:
        world.payroll()
        monkeypatch.setattr(SQLiteOntologyStore, "traverse",
                            lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
        meta = world.get_context(task="payroll_consumer_lag", slot="x")["ontology"]
        assert meta["fallback_reason"] == "graph_error:OperationalError"

    def test_auth_failure_fails_closed_without_unscoped_retry(self, world: World, monkeypatch) -> None:
        world.payroll()
        calls: list[str] = []
        original = SQLiteOntologyStore.resolve_terms

        def spy(self, *args, **kwargs):
            calls.append(kwargs["auth"].scope.namespace)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(SQLiteOntologyStore, "resolve_terms", spy)
        principal = Principal(name="bob", agent_id=None, pipelines=frozenset({"p2"}),
                              capabilities=frozenset())
        enricher = build_enricher(world.config, world.store, principal=principal)
        assert enricher is not None
        conscious = ConsciousBlock(owns=[], must_not=[], agent_id="a1", role="b", task="payroll_consumer_lag", slot="x",
                                   intent="advance", pipeline_id="p1")
        with pytest.raises(OntologyAuthError):
            Assembler(store=world.store, config=world.config, ontology=enricher).assemble(
                conscious=conscious, budget=BudgetContext(),
            )
        assert calls == []  # nothing ran, in no scope

    def test_mcp_auth_failure_is_forbidden(self, tmp_path: Path) -> None:
        root = _project(tmp_path, None)
        world = World(root)
        world.payroll()
        principal = Principal(name="bob", agent_id=None, pipelines=frozenset({"p2"}),
                              capabilities=frozenset())
        enricher = build_enricher(world.config, world.store, principal=principal)
        assert isinstance(enricher, OntologyEnricher)
        with pytest.raises(OntologyAuthError):
            enricher.enrich(conscious=ConsciousBlock(owns=[], must_not=[], agent_id="a", role="b", task="idle", slot="x",
                                                   intent="advance", pipeline_id="p1"),
                            query_text="payroll upstream", chunks=[], token_limit=None)

    def test_uninitialized_store_is_skipped_without_creating_tables(self, tmp_path: Path) -> None:
        root = _project(tmp_path, '[tools]\nprofile = "core"\n\n' + ENABLED)
        config = load_config(cwd=root)
        store = SQLiteStore(root / ".ncp" / "store.db")
        handlers = make_handlers(store, config=config)
        response = handlers["ncp_get_context"]({"agent_id": "a", "role": "b", "task": "payroll_lag",
                                                "slot": "x", "intent": "advance"})
        assert response["ontology"]["reason_code"] == "ontology_uninitialized"
        with closing(sqlite3.connect(store.path)) as connection:
            tables = {name for (name,) in connection.execute("SELECT name FROM sqlite_master")}
        assert not any(name.startswith("ontology_") for name in tables)


# ── PR 3 review follow-ups ───────────────────────────────────────────────────


class TestFacadeCaching:
    def test_facade_is_cached_per_store(self, tmp_path: Path, monkeypatch) -> None:
        root = _project(tmp_path, '[tools]\nprofile = "full"\n\n' + ENABLED)
        config = load_config(cwd=root)
        store = SQLiteStore(root / ".ncp" / "store.db")
        migrations: list[int] = []
        original = SQLiteOntologyStore._migrate

        def counting(self):
            migrations.append(1)
            return original(self)

        monkeypatch.setattr(SQLiteOntologyStore, "_migrate", counting)
        first = cached_facade(config, store)
        for _ in range(3):
            make_handlers(store, config=config)
        assert cached_facade(config, store) is first and len(migrations) == 1
        changed = OntologyConfig(enabled=True, max_hops=1)
        assert cached_facade(changed, store) is not first

    def test_seed_vanishing_between_reads_is_not_found(self, world: World, monkeypatch) -> None:
        world.payroll()
        original = SQLiteOntologyStore.get_entities

        def vanishing(self, ids, *, auth, deadline=None):
            return {} if len(list(ids)) == 1 else original(self, ids, auth=auth, deadline=deadline)

        monkeypatch.setattr(SQLiteOntologyStore, "get_entities", vanishing)
        from ncp.ontology.facade import OntologyError

        with pytest.raises(OntologyError) as raised:
            world.facade.query(_auth(), key="payroll")
        assert raised.value.code == "not_found"

    def test_core_profile_handlers_create_no_tables(self, tmp_path: Path) -> None:
        root = _project(tmp_path, '[tools]\nprofile = "core"\n\n' + ENABLED)
        store = SQLiteStore(root / ".ncp" / "store.db")
        make_handlers(store, config=load_config(cwd=root))
        with closing(sqlite3.connect(store.path)) as connection:
            tables = {name for (name,) in connection.execute("SELECT name FROM sqlite_master")}
        assert not any(name.startswith("ontology_") for name in tables)
        facade_module._FACADE_CACHE.pop(store, None)


# ── policy fixes (Task B) ────────────────────────────────────────────────────


class TestPolicyFixes:
    def _window_relation(self, world: World) -> tuple[Entity, Entity, Relation]:
        evidence = world.chunk("payroll reads ledger")
        payroll = world.entity("Service", "payroll")
        ledger = world.entity("Database", "ledger")
        relation = world.relate(payroll, ledger, "DEPENDS_ON", evidence=[evidence],
                                auth=_auth(principal="alice"))
        return payroll, ledger, relation

    def _reassert(self, world: World, relation: Relation, auth: AuthContext, **changes: object) -> Relation:
        payload = relation.model_copy(update=changes)
        return world.ontology.assert_relation(payload, auth=auth, idempotency_key=f"re-{time.time_ns()}")

    def test_only_asserter_or_admin_moves_validity_window(self, world: World) -> None:
        _, _, relation = self._window_relation(world)
        later = relation.valid_from + timedelta(hours=1)
        with pytest.raises(PermissionError):
            self._reassert(world, relation, _auth(principal="mallory"), valid_from=later)
        with pytest.raises(PermissionError):
            self._reassert(world, relation, _auth(principal="mallory"),
                           valid_until=NOW + timedelta(days=9))
        # Same window, other principal (with verify): allowed.
        assert self._reassert(world, relation, _auth(principal="mallory"), confidence=0.8).revision == 2
        moved = self._reassert(world, relation, _auth(principal="alice"), valid_until=NOW + timedelta(days=9))
        assert moved.valid_until is not None
        admin = _auth(principal="root", caps={"read", "write", "verify", "admin"})
        assert self._reassert(world, relation, admin, valid_until=None).valid_until is None

    def test_window_change_out_of_scope_is_not_found(self, world: World) -> None:
        _, _, relation = self._window_relation(world)
        other = relation.model_copy(update={"scope": Scope(tenant_id="default", namespace="p2")})
        from ncp.ontology.stores.sqlite import OntologyNotFoundError

        with pytest.raises((OntologyNotFoundError, ValueError)) as raised:
            world.ontology.assert_relation(
                other.model_copy(update={"valid_until": NOW + timedelta(days=3)}),
                auth=_auth("p2", principal="mallory"), idempotency_key="x",
            )
        assert not isinstance(raised.value, PermissionError)

    def test_future_relations_are_inactive_but_explainable(self, world: World) -> None:
        evidence = world.chunk("payroll will read ledger")
        payroll = world.entity("Service", "payroll")
        ledger = world.entity("Database", "ledger")
        future = world.relate(payroll, ledger, "DEPENDS_ON", evidence=[evidence],
                              valid_from=NOW + timedelta(days=1))
        result = world.facade.query(_auth(), key="payroll")
        assert result["paths"] == []
        explained = world.facade.explain(_auth(), str(future.relation_id))
        assert explained["validity"]["in_validity_window"] is False
        assert explained["validity"]["active"] is False
        meta = world.get_context(task="payroll_upstream", slot="x")["ontology"]
        assert meta["verified_path_count"] == 0

    def test_future_relations_do_not_count_for_cardinality(self, world: World, monkeypatch) -> None:
        from ncp.ontology.registry import RelationType

        evidence = world.chunk("payroll deps")
        payroll = world.entity("Service", "payroll")
        first, second = world.entity("Database", "ledger"), world.entity("Database", "cache")
        original = world.ontology.registry.relation_definition
        monkeypatch.setattr(world.ontology.registry, "relation_definition",
                            lambda relation: RelationType.model_validate(
                                {"from": ["Service"], "to": ["Database"], "max_outgoing": 1})
                            if relation.relation_type == "DEPENDS_ON" else original(relation))
        world.relate(payroll, first, "DEPENDS_ON", evidence=[evidence], valid_from=NOW + timedelta(days=1))
        world.relate(payroll, second, "DEPENDS_ON", evidence=[evidence])  # only active one: allowed

    def test_asserter_revokes_own_verified_relation_without_verify(self, world: World) -> None:
        _, _, relation = self._window_relation(world)
        revoked = world.ontology.revoke_relation(
            relation.relation_id, auth=_auth(principal="alice", caps={"read", "write"}), reason="wrong",
        )
        assert revoked.status == "revoked"

    def test_unverified_query_requires_verify_or_server_opt_out(self, world: World) -> None:
        payroll = world.entity("Service", "payroll")
        ledger = world.entity("Database", "ledger")
        world.relate(payroll, ledger, "DEPENDS_ON", status="candidate")
        reader = _auth(caps={"read", "write"})
        forced = world.facade.query(reader, key="payroll", verified_only=False)
        assert forced["limits"]["verified_only"] is True and forced["paths"] == []
        assert any("verified_only" in warning for warning in forced["warnings"])
        allowed = world.facade.query(_auth(), key="payroll", verified_only=False)
        assert allowed["limits"]["verified_only"] is False and len(allowed["paths"]) == 1
        relaxed = OntologyFacade(OntologyConfig(enabled=True, verified_only=False), world.ontology)
        assert len(relaxed.query(reader, key="payroll", verified_only=False)["paths"]) == 1

    def test_sensitive_attributes_redacted_unless_admin(self, world: World) -> None:
        evidence = world.chunk("payroll reads ledger")
        payroll = world.entity("Service", "payroll")
        ledger = world.entity("Database", "ledger")
        relation = world.relate(payroll, ledger, "DEPENDS_ON", evidence=[evidence], attributes={
            "db_password": "pw1", "API-Key": "k1", "nested": {"client_secret": "s1", "port": 5432},
            "private_key_pem": "pk", "auth_token": "t1", "credentials": ["c"], "port": 1521,
        })
        for result in (world.facade.query(_auth(), key="payroll")["paths"][0]["relations"][0],
                       world.facade.explain(_auth(), str(relation.relation_id))["relation"]):
            attributes = result["attributes"]
            assert attributes["db_password"] == REDACTED and attributes["API-Key"] == REDACTED
            assert attributes["nested"] == {"client_secret": REDACTED, "port": 5432}
            assert attributes["private_key_pem"] == REDACTED and attributes["auth_token"] == REDACTED
            assert attributes["credentials"] == REDACTED and attributes["port"] == 1521
        admin = _auth(caps={"read", "write", "verify", "admin"})
        raw = world.facade.explain(admin, str(relation.relation_id))["relation"]["attributes"]
        assert raw["db_password"] == "pw1"

    def test_redaction_helper(self) -> None:
        assert redact_attributes({"Password": 1, "items": [{"token": 2}], "ok": 3}) == {
            "Password": REDACTED, "items": [{"token": REDACTED}], "ok": 3,
        }
        assert redact_attributes({"zone": "a"}, extra=("zone",)) == {"zone": REDACTED}


# ── HTTP principal path ──────────────────────────────────────────────────────


class TestPrincipalPath:
    def test_authorized_principal_gets_enrichment(self, tmp_path: Path) -> None:
        root = _project(tmp_path, None)
        world = World(root)
        world.payroll()
        principal = Principal(name="alice", agent_id=None, pipelines=frozenset({"p1"}),
                              capabilities=frozenset())
        (root / ".ncp" / "config.toml").write_text(
            ENABLED + VERIFIER + '\n[[principals]]\nname = "alice"\n'
            f'token_sha256 = "{hash_token("t")}"\npipelines = ["p1"]\n', encoding="utf-8",
        )
        config = load_config(cwd=root)
        tools = tools_for_config(config)
        handlers = make_handlers(world.store, config=config)
        response = _call(handlers, tools, "ncp_get_context", {
            "agent_id": "a1", "role": "b", "pipeline_id": "p1", "task": "payroll_consumer_lag",
            "slot": "x", "intent": "advance",
        }, principal=principal, authorizer=ToolAuthorizer(world.store))
        body = json.loads(response["result"]["content"][0]["text"])
        assert body["ontology"]["verified_path_count"] >= 1
