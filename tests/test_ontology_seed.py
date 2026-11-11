"""Declarative ontology seeding (``ncp ontology seed`` / ``ncp.ontology.seed``)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.config import load_config
from ncp.mcp.server import make_handlers
from ncp.ontology.facade import OntologyError, OntologyFacade, auth_context_for
from ncp.ontology.seed import (
    load_manifest, memory_chunk_writer, merge_manifests, parse_manifest, seed_ontology,
    seed_principal,
)
from ncp.stores.sqlite import SQLiteStore

ENABLED = "[ontology]\nenabled = true\ntrigger_max_ms = 1000\ngraph_timeout_ms = 5000\n"
OBSERVED = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()

MANIFEST: dict[str, object] = {
    "pipeline": "shop",
    "source": "entity_registry",
    "observed_at": OBSERVED,
    "entities": [
        {"type": "Service", "key": "order-api", "aliases": ["orders-api"],
         "attributes": {"environment": "prod"}},
        {"type": "Service", "key": "order-worker", "attributes": {"environment": "prod"}},
        {"type": "KafkaTopic", "key": "order.events", "attributes": {"cluster": "main"}},
        {"type": "Team", "key": "team-orders"},
    ],
    "relations": [
        {"src": "order-api", "type": "PRODUCES", "dst": "order.events",
         "evidence": "order-api publishes OrderPlaced to the order.events topic."},
        {"src": "Service:order-worker", "type": "CONSUMES", "dst": "KafkaTopic:order.events",
         "attestation": "entity-registry.yaml#order-worker", "revision": "r42"},
        {"src": "team-orders", "type": "OWNS", "dst": "order-api",
         "evidence": "team-orders owns order-api (CODEOWNERS)."},
    ],
}


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        (self.root / ".git").mkdir(parents=True)
        (self.root / ".ncp").mkdir()
        (self.root / ".ncp" / "config.toml").write_text(ENABLED, encoding="utf-8")
        self.config = load_config(cwd=self.root)
        self.store = SQLiteStore(self.root / ".ncp" / "store.db")
        self.facade = OntologyFacade.from_config(self.config, self.store)
        assert self.facade.enabled

    def seed(self, manifest: dict[str, object] | None = None, **kwargs: object):
        return seed_ontology(
            self.facade, parse_manifest(manifest or MANIFEST),
            write_chunk=memory_chunk_writer(self.store), **kwargs,  # type: ignore[arg-type]
        )

    def relations(self) -> list:
        auth = auth_context_for(None, "shop", config=self.facade.config)
        return self.facade.store.relations_asserted_by(  # type: ignore[union-attr]
            seed_principal("entity_registry"), auth=auth)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _manifest(**changes: object) -> dict[str, object]:
    return {**MANIFEST, **changes}


class TestSeed:
    def test_first_run_creates_then_rerun_is_a_noop(self, world: World) -> None:
        first = world.seed().to_dict()
        assert first["entities"] == {"created": 4, "updated": 0, "unchanged": 0, "referenced": 0}
        assert first["relations"]["created"] == 3
        assert first["evidence_chunks"] == 2
        revisions = {r.relation_id: r.revision for r in world.relations()}
        assert all(r.status == "verified" for r in world.relations())

        second = world.seed().to_dict()
        assert second["entities"] == {"created": 0, "updated": 0, "unchanged": 4, "referenced": 0}
        assert second["relations"]["unchanged"] == 3
        assert second["evidence_chunks"] == 0
        assert {r.relation_id: r.revision for r in world.relations()} == revisions

    def test_evidence_is_cited(self, world: World) -> None:
        world.seed()
        refs = {ref.evidence_type: ref for r in world.relations() for ref in r.evidence}
        assert refs["external_attestation"].evidence_id == "entity-registry.yaml#order-worker"
        assert refs["external_attestation"].source_revision == "r42"
        [chunk] = world.store.get_chunks_by_ids([refs["chunk"].evidence_id])
        assert chunk.pipeline_id == "shop"
        assert chunk.written_by == "seed:entity_registry"

    def test_changed_relation_updates_in_place(self, world: World) -> None:
        world.seed()
        before = {r.relation_id: r for r in world.relations()}
        relations = [dict(item) for item in MANIFEST["relations"]]  # type: ignore[union-attr]
        relations[0]["confidence"] = 0.6
        report = world.seed(_manifest(relations=relations)).to_dict()
        assert report["relations"]["updated"] == 1 and report["relations"]["unchanged"] == 2
        after = {r.relation_id: r for r in world.relations()}
        assert after.keys() == before.keys()
        changed = [rid for rid in after if after[rid].revision != before[rid].revision]
        assert len(changed) == 1 and after[changed[0]].confidence == 0.6

    def test_removed_relation_is_stale_then_pruned_then_reopened(self, world: World) -> None:
        world.seed()
        smaller = _manifest(relations=MANIFEST["relations"][:2])  # type: ignore[index]
        report = world.seed(smaller)
        assert report.stale == ["Team:team-orders -OWNS-> Service:order-api"]
        assert report.relations_pruned == 0

        dry = world.seed(smaller, prune=True, dry_run=True)
        assert dry.relations_pruned == 1
        assert all(r.valid_until is None for r in world.relations())

        pruned = world.seed(smaller, prune=True)
        assert pruned.pruned == ["Team:team-orders -OWNS-> Service:order-api"]
        closed = [r for r in world.relations() if r.valid_until is not None]
        assert len(closed) == 1 and closed[0].relation_type == "OWNS"
        assert world.seed(smaller, prune=True).relations_pruned == 0  # already closed

        reopened = world.seed()
        assert reopened.relations_reopened == 1
        assert all(r.valid_until is None for r in world.relations())
        assert world.seed().to_dict()["relations"]["unchanged"] == 3

    def test_prune_never_touches_other_principals(self, world: World) -> None:
        world.seed()
        other = _manifest(source="cmdb", relations=MANIFEST["relations"][:1])  # type: ignore[index]
        world.seed(other)
        report = world.seed(_manifest(source="cmdb", relations=[]), prune=True)
        assert report.relations_pruned == 1
        assert all(r.valid_until is None for r in world.relations())

    def test_dry_run_writes_nothing(self, world: World) -> None:
        report = world.seed(dry_run=True)
        assert report.relations_created == 3 and report.entities_created == 4
        assert world.relations() == []
        with sqlite3.connect(world.root / ".ncp" / "store.db") as connection:
            assert connection.execute("SELECT COUNT(*) FROM ontology_entities").fetchone() == (0,)
            assert connection.execute(
                "SELECT COUNT(*) FROM chunks WHERE chunk_id LIKE 'ont-seed-%'").fetchone() == (0,)

    def test_status_override_stages_candidates(self, world: World) -> None:
        manifest = _manifest(relations=[{"src": "order-api", "type": "PRODUCES",
                                         "dst": "order.events", "status": "candidate"}])
        world.seed(manifest)
        assert [r.status for r in world.relations()] == ["candidate"]
        world.seed(status_override="candidate")
        assert {r.status for r in world.relations()} == {"candidate"}

    def test_seeded_facts_reach_get_context(self, world: World) -> None:
        world.seed()
        handlers = make_handlers(world.store, config=world.config)
        response = handlers["ncp_get_context"]({
            "agent_id": "a1", "role": "build", "pipeline_id": "shop",
            "task": "which_services_consume_order.events", "slot": "order.events",
            "intent": "diagnose", "max_tokens": 800,
        })
        assert "[NCP:ONTOLOGY]" in response["context"]  # type: ignore[operator]
        assert "CONSUMES" in response["context"]  # type: ignore[operator]


class TestValidation:
    @pytest.mark.parametrize(("change", "message"), [
        ({"relations": [{"src": "order-api", "type": "PRODUCES", "dst": "order.events"}]},
         "needs 'evidence' text or an 'attestation'"),
        ({"relations": [{"src": "nope", "type": "PRODUCES", "dst": "order.events",
                         "attestation": "x"}]}, "neither declared in the manifest nor an existing entity"),
        ({"relations": [{"src": "order.events", "type": "PRODUCES", "dst": "order-api",
                         "attestation": "x"}]}, "is not allowed"),
        ({"relations": [{"src": "order-api", "type": "CALLS", "dst": "order.events",
                         "attestation": "x"}]}, "unknown relation type"),
        ({"entities": [{"type": "Deployment", "key": "x"}], "relations": []},
         "unknown entity type"),
        ({"entities": [{"type": "Service", "key": "x"}], "relations": []},
         "missing required attribute: environment"),
        ({"entities": [{"type": "Team", "key": "a"}, {"type": "Team", "key": "A"}],
          "relations": []}, "duplicate entity"),
        ({"pipeline": None}, "no namespace"),
    ])
    def test_bad_manifest_writes_nothing(self, world: World, change: dict, message: str) -> None:
        with pytest.raises(OntologyError, match=message) as info:
            world.seed(_manifest(**change))
        assert info.value.code == "invalid"
        assert world.relations() == []

    def test_ambiguous_key_needs_type_prefix(self, world: World) -> None:
        manifest = _manifest(
            entities=[{"type": "Service", "key": "shop", "attributes": {"environment": "prod"}},
                      {"type": "KafkaTopic", "key": "shop", "attributes": {"cluster": "main"}}],
            relations=[{"src": "shop", "type": "PRODUCES", "dst": "shop", "attestation": "x"}],
        )
        with pytest.raises(OntologyError, match="ambiguous"):
            world.seed(manifest)
        manifest["relations"] = [{"src": "Service:shop", "type": "PRODUCES",
                                  "dst": "KafkaTopic:shop", "attestation": "x"}]
        assert world.seed(manifest).relations_created == 1

    def test_disabled_ontology(self, tmp_path: Path) -> None:
        facade = OntologyFacade.disabled()
        with pytest.raises(OntologyError) as info:
            seed_ontology(facade, parse_manifest(MANIFEST))
        assert info.value.code == "disabled"


class TestManifestFormats:
    def test_json_and_toml_load_identically(self, tmp_path: Path) -> None:
        (tmp_path / "m.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
        toml = (
            f'pipeline = "shop"\nsource = "entity_registry"\nobserved_at = "{OBSERVED}"\n'
            '[[entities]]\ntype = "Team"\nkey = "team-orders"\n'
        )
        (tmp_path / "m.toml").write_text(toml, encoding="utf-8")
        assert load_manifest(tmp_path / "m.json") == parse_manifest(MANIFEST)
        assert load_manifest(tmp_path / "m.toml").entities[0].key == "team-orders"

    def test_example_manifest_seeds(self, world: World) -> None:
        example = Path(__file__).resolve().parents[1] / "examples" / "13_ontology" / "manifest.toml"
        report = seed_ontology(world.facade, load_manifest(example),
                               write_chunk=memory_chunk_writer(world.store))
        assert (report.entities_created, report.relations_created) == (5, 4)

    def test_unsupported_suffix(self, tmp_path: Path) -> None:
        (tmp_path / "m.txt").write_text("{}", encoding="utf-8")
        with pytest.raises(OntologyError, match="must be .json"):
            load_manifest(tmp_path / "m.txt")


class TestCli:
    def test_seed_command(self, world: World, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
        runner = CliRunner()
        args = ["ontology", "seed", str(manifest), "--cwd", str(world.root), "--json"]
        dry = runner.invoke(main, [*args, "--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert json.loads(dry.output[dry.output.index("{"):])["relations"]["created"] == 3
        first = runner.invoke(main, args)
        assert first.exit_code == 0, first.output
        assert json.loads(first.output[first.output.index("{"):])["relations"]["created"] == 3
        again = runner.invoke(main, args)
        assert json.loads(again.output[again.output.index("{"):])["relations"]["unchanged"] == 3

    def test_seed_command_requires_enabled_ontology(self, tmp_path: Path) -> None:
        root = tmp_path / "plain"
        (root / ".git").mkdir(parents=True)
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
        result = CliRunner().invoke(main, ["ontology", "seed", str(manifest), "--cwd", str(root)])
        assert result.exit_code != 0
        assert "[ontology]" in result.output


# -- several manifests (monorepo modules) -----------------------------------------

PAYMENTS = {
    "pipeline": "shop", "observed_at": OBSERVED,
    "entities": [{"type": "Service", "key": "payments", "attributes": {"environment": "prod"}},
                 {"type": "Database", "key": "ledger-db"}],
    "relations": [{"src": "payments", "type": "DEPENDS_ON", "dst": "ledger-db",
                   "attestation": "payments/service.yaml#L3"}],
}
CHECKOUT = {
    "pipeline": "shop", "observed_at": OBSERVED,
    "entities": [{"type": "Service", "key": "checkout", "attributes": {"environment": "prod"}}],
    # References payments, declared only in the other module's manifest.
    "relations": [{"src": "checkout", "type": "DEPENDS_ON", "dst": "Service:payments",
                   "attestation": "checkout/service.yaml#L9"}],
}


def _merge(*items: tuple[str, dict], **kwargs: object):
    return merge_manifests([(label, parse_manifest(raw)) for label, raw in items], **kwargs)  # type: ignore[arg-type]


class TestMerge:
    def test_cross_manifest_reference_and_shared_prune_scope(self, world: World) -> None:
        merged = _merge(("payments.toml", PAYMENTS), ("checkout.toml", CHECKOUT), source="repo")
        report = seed_ontology(world.facade, merged.manifest, where=merged.where,
                               write_chunk=memory_chunk_writer(world.store))
        assert (report.entities_created, report.relations_created) == (3, 2)
        assert report.principal_id == "seed:repo"
        # Seeding one module alone under the shared source would prune the
        # other's edges; seeding them together keeps both.
        again = _merge(("payments.toml", PAYMENTS), ("checkout.toml", CHECKOUT), source="repo")
        assert seed_ontology(world.facade, again.manifest, prune=True,
                             write_chunk=memory_chunk_writer(world.store)).relations_pruned == 0

    def test_identical_redeclaration_is_merged(self) -> None:
        redeclared = {**CHECKOUT, "entities": [*CHECKOUT["entities"], PAYMENTS["entities"][0]]}
        merged = _merge(("a", PAYMENTS), ("b", redeclared))
        assert len(merged.manifest.entities) == 3

    def test_conflicting_redeclaration_names_both_files(self) -> None:
        conflicting = {**CHECKOUT, "entities": [
            *CHECKOUT["entities"],
            {"type": "Service", "key": "payments", "attributes": {"environment": "staging"}}]}
        with pytest.raises(OntologyError, match=r"b entities\[1\].*declared differently in a entities\[0\]"):
            _merge(("a", PAYMENTS), ("b", conflicting))

    @pytest.mark.parametrize(("field", "values", "hint"), [
        ("source", ("x", "y"), "--source"),
        ("pipeline", ("shop", "other"), "--pipeline-id"),
    ])
    def test_disagreeing_headers(self, field: str, values: tuple, hint: str) -> None:
        with pytest.raises(OntologyError, match=hint):
            _merge(("a", {**PAYMENTS, field: values[0]}), ("b", {**CHECKOUT, field: values[1]}))
        merged = _merge(("a", {**PAYMENTS, field: values[0]}), ("b", {**CHECKOUT, field: values[1]}),
                        **{field: "forced"})
        assert getattr(merged.manifest, field) == "forced"

    def test_errors_point_at_the_file(self, world: World) -> None:
        broken = {**CHECKOUT, "relations": [{"src": "checkout", "type": "DEPENDS_ON",
                                             "dst": "nowhere", "attestation": "x"}]}
        merged = _merge(("payments.toml", PAYMENTS), ("checkout.toml", broken))
        with pytest.raises(OntologyError, match=r"checkout\.toml relations\[0\]\.dst"):
            seed_ontology(world.facade, merged.manifest, where=merged.where)

    def test_manifest_observed_at_applies_to_its_own_relations(self) -> None:
        later = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()
        merged = _merge(("a", PAYMENTS), ("b", {**CHECKOUT, "observed_at": later}))
        stamps = [r.observed_at.isoformat() for r in merged.manifest.relations]  # type: ignore[union-attr]
        assert stamps == [OBSERVED, later]

    def test_cli_accepts_several_manifests(self, world: World, tmp_path: Path) -> None:
        paths = []
        for name, raw in (("payments.json", PAYMENTS), ("checkout.json", CHECKOUT)):
            (tmp_path / name).write_text(json.dumps(raw), encoding="utf-8")
            paths.append(str(tmp_path / name))
        result = CliRunner().invoke(main, ["ontology", "seed", *paths, "--source", "repo",
                                           "--cwd", str(world.root), "--json"])
        assert result.exit_code == 0, result.output
        report = json.loads(result.output[result.output.index("{"):])
        assert report["principal_id"] == "seed:repo" and report["relations"]["created"] == 2


# -- references to entities already in the store ----------------------------------


class TestExistingReferences:
    def test_reference_entity_seeded_by_another_source(self, world: World) -> None:
        world.seed(_manifest(source="platform", entities=PAYMENTS["entities"], relations=[]))
        report = world.seed(CHECKOUT)  # payments is not declared here
        assert report.relations_created == 1 and report.entities_referenced == 1
        assert report.entities_created == 1  # checkout only; payments is never rewritten

    def test_global_entity_can_be_a_target_but_not_a_source(self, world: World) -> None:
        world.seed(_manifest(pipeline="global", source="platform",
                             entities=[{"type": "Database", "key": "shared-db"}], relations=[]))
        ok = world.seed({**CHECKOUT, "relations": [
            {"src": "checkout", "type": "DEPENDS_ON", "dst": "Database:shared-db",
             "attestation": "x"}]})
        assert ok.relations_created == 1
        with pytest.raises(OntologyError, match="source must be in the target namespace"):
            world.seed({**CHECKOUT, "relations": [
                {"src": "Database:shared-db", "type": "DEPENDS_ON", "dst": "checkout",
                 "attestation": "x"}]})

    def test_bare_key_matching_several_existing_types_is_ambiguous(self, world: World) -> None:
        world.seed(_manifest(source="platform", relations=[], entities=[
            {"type": "Service", "key": "orders", "attributes": {"environment": "prod"}},
            {"type": "Database", "key": "orders"}]))
        with pytest.raises(OntologyError, match="several existing entity types"):
            world.seed({**CHECKOUT, "relations": [
                {"src": "checkout", "type": "DEPENDS_ON", "dst": "orders", "attestation": "x"}]})
        assert world.seed({**CHECKOUT, "relations": [
            {"src": "checkout", "type": "DEPENDS_ON", "dst": "Database:orders",
             "attestation": "x"}]}).relations_created == 1


# -- schema pack v2 ----------------------------------------------------------------

MONOREPO_V2 = {
    "pipeline": "shop", "source": "repo", "schema_version": 2, "observed_at": OBSERVED,
    "entities": [
        {"type": "Repo", "key": "monorepo"},
        {"type": "Module", "key": "services/payments"},
        {"type": "Service", "key": "payments", "attributes": {"environment": "prod"}},
        {"type": "Database", "key": "ledger-db"},
        {"type": "Table", "key": "ledger-db.captures"},
        {"type": "Team", "key": "team-payments"},
    ],
    "relations": [
        {"src": "monorepo", "type": "CONTAINS", "dst": "services/payments", "attestation": "a"},
        {"src": "services/payments", "type": "BUILDS", "dst": "payments", "attestation": "b"},
        {"src": "ledger-db", "type": "CONTAINS", "dst": "ledger-db.captures", "attestation": "c"},
        {"src": "payments", "type": "WRITES", "dst": "ledger-db.captures", "attestation": "d"},
        {"src": "team-payments", "type": "OWNS", "dst": "services/payments", "attestation": "e"},
    ],
}


class TestSchemaV2:
    def test_v2_types_need_an_explicit_version(self, world: World) -> None:
        with pytest.raises(OntologyError, match="unknown entity type 'Repo'"):
            world.seed({k: v for k, v in MONOREPO_V2.items() if k != "schema_version"})
        report = world.seed(MONOREPO_V2)
        assert (report.entities_created, report.relations_created) == (6, 5)

    def test_config_pin_makes_v2_the_default(self, tmp_path: Path) -> None:
        world = World(tmp_path)
        (world.root / ".ncp" / "config.toml").write_text(
            ENABLED + "pack_versions = { infrastructure = 2 }\n", encoding="utf-8")
        config = load_config(cwd=world.root)
        facade = OntologyFacade.from_config(config, world.store)
        manifest = parse_manifest({k: v for k, v in MONOREPO_V2.items() if k != "schema_version"})
        assert seed_ontology(facade, manifest).relations_created == 5
        caps = facade.capabilities()
        assert [(p["version"], p["default"]) for p in caps["packs"]] == [(1, False), (2, True)]  # type: ignore[index]

    def test_bad_pin_is_rejected(self, tmp_path: Path) -> None:
        world = World(tmp_path)
        (world.root / ".ncp" / "config.toml").write_text(
            ENABLED + "pack_versions = { infrastructure = 9 }\n", encoding="utf-8")
        with pytest.raises(ValueError, match="pack_versions pins infrastructure v9"):
            OntologyFacade.from_config(load_config(cwd=world.root), world.store)

    def test_v1_graph_cannot_be_silently_upgraded(self, world: World) -> None:
        world.seed(PAYMENTS | {"source": "repo"})
        upgraded = {**PAYMENTS, "source": "repo", "schema_version": 2}
        with pytest.raises(OntologyError, match="exists on infrastructure v1; the manifest uses v2"):
            world.seed(upgraded)
