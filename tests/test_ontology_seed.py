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
    load_manifest, memory_chunk_writer, parse_manifest, seed_ontology, seed_principal,
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
        assert first["entities"] == {"created": 4, "updated": 0, "unchanged": 0}
        assert first["relations"]["created"] == 3
        assert first["evidence_chunks"] == 2
        revisions = {r.relation_id: r.revision for r in world.relations()}
        assert all(r.status == "verified" for r in world.relations())

        second = world.seed().to_dict()
        assert second["entities"] == {"created": 0, "updated": 0, "unchanged": 4}
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
                         "attestation": "x"}]}, "not an entity declared"),
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
