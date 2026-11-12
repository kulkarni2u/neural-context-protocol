"""Promoting candidate relations (``ncp ontology promote/demote``, seed auto-promote)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.config import load_config
from ncp.ontology.facade import OntologyFacade, auth_context_for
from ncp.ontology.models import GraphQuery
from ncp.ontology.policy import PromotionConfig
from ncp.ontology.promotion import (
    PromotionItem, load_relations, parse_checklist, select_promotions,
)
from ncp.ontology.seed import memory_chunk_writer, parse_manifest, seed_ontology
from ncp.stores.sqlite import SQLiteStore

ENABLED = "[ontology]\nenabled = true\ntrigger_max_ms = 1000\ngraph_timeout_ms = 5000\n"
OBSERVED = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0).isoformat()

RELATIONS: list[dict[str, object]] = [
    {"src": "order-api", "type": "PRODUCES", "dst": "order.events", "status": "candidate",
     "confidence": 0.6, "evidence": "order-api publishes to order.events (scan)."},
    {"src": "order-worker", "type": "CONSUMES", "dst": "KafkaTopic:order.events",
     "status": "candidate", "confidence": 0.6, "attestation": "scan#worker"},
    {"src": "order-api", "type": "PRODUCES", "dst": "lonely.topic", "status": "candidate",
     "confidence": 0.5},
    {"src": "order-api", "type": "PRODUCES", "dst": "KafkaTopic:unresolved:dyn",
     "status": "candidate", "confidence": 0.4},
]
MANIFEST: dict[str, object] = {
    "pipeline": "shop",
    "source": "scan",
    "observed_at": OBSERVED,
    "entities": [
        {"type": "Service", "key": "order-api", "attributes": {"environment": "prod"}},
        {"type": "Service", "key": "order-worker", "attributes": {"environment": "prod"}},
        {"type": "KafkaTopic", "key": "order.events", "attributes": {"cluster": "main"}},
        {"type": "KafkaTopic", "key": "lonely.topic", "attributes": {"cluster": "main"}},
        {"type": "KafkaTopic", "key": "unresolved:dyn", "attributes": {"cluster": "main"}},
    ],
    "relations": RELATIONS,
}
API_ORDERS = "Service:order-api -PRODUCES-> KafkaTopic:order.events"
WORKER_ORDERS = "Service:order-worker -CONSUMES-> KafkaTopic:order.events"
LONELY = "Service:order-api -PRODUCES-> KafkaTopic:lonely.topic"
DYN = "Service:order-api -PRODUCES-> KafkaTopic:unresolved:dyn"


class World:
    def __init__(self, tmp_path: Path, extra_config: str = "") -> None:
        self.root = tmp_path / "repo"
        (self.root / ".git").mkdir(parents=True)
        (self.root / ".ncp").mkdir()
        (self.root / ".ncp" / "config.toml").write_text(ENABLED + extra_config, encoding="utf-8")
        self.config = load_config(cwd=self.root)
        self.store = SQLiteStore(self.root / ".ncp" / "store.db")
        self.facade = OntologyFacade.from_config(self.config, self.store)
        assert self.facade.enabled
        self.auth = auth_context_for(None, "shop", config=self.facade.config)
        self.runner = CliRunner()

    def seed(self, manifest: dict[str, object] | None = None, **kwargs: object):
        return seed_ontology(
            self.facade, parse_manifest(manifest or MANIFEST),
            write_chunk=memory_chunk_writer(self.store), **kwargs,  # type: ignore[arg-type]
        )

    def stored(self) -> dict:
        return {s.item.ref: s for s in load_relations(self.facade, self.auth)}

    def status(self, ref: str) -> str:
        return self.stored()[ref].item.status

    def relation(self, ref: str):
        return self.stored()[ref].relation

    def rid(self, ref: str) -> str:
        return self.stored()[ref].item.key

    def cli(self, *args: str, exit_code: int = 0):
        result = self.runner.invoke(
            main, ["ontology", *args, "--pipeline-id", "shop", "--cwd", str(self.root)],
        )
        assert result.exit_code == exit_code, result.output
        return result

    def verified_edges(self) -> set[str]:
        store = self.facade.store
        match, _ = store.resolve_entities("order-api", auth=self.auth)  # type: ignore[union-attr]
        result = store.traverse(  # type: ignore[union-attr]
            GraphQuery(seeds=match.entity_ids, verified_only=True, max_hops=2), auth=self.auth,
        )
        return {str(rel.relation_id) for path in result.paths for rel in path.relations}


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    w.seed()
    return w


class TestList:
    def test_list_table_and_json(self, world: World) -> None:
        out = world.cli("promote", "--list").output
        assert API_ORDERS in out and "4 candidate" in out and "0.60" in out
        assert "scan#worker" in out and "no evidence" in out
        data = json.loads(world.cli("promote", "--list", "--json").output)
        assert len(data["candidates"]) == 4
        assert {c["status"] for c in data["candidates"]} == {"candidate"}

    def test_list_applies_selectors(self, world: World) -> None:
        data = json.loads(world.cli("promote", "--list", "--type", "CONSUMES", "--json").output)
        assert [f"{c['src']} -{c['type']}-> {c['dst']}" for c in data["candidates"]] == [
            WORKER_ORDERS]


class TestPromote:
    def test_by_id_keeps_evidence_and_records_provenance(self, world: World) -> None:
        rid = world.rid(API_ORDERS)
        before = world.relation(API_ORDERS)
        assert rid not in world.verified_edges()
        world.cli("promote", rid)
        after = world.relation(API_ORDERS)
        assert after.status == "verified" and after.confidence == 0.9
        assert after.attributes["promoted_by"] == "cli" and "promoted_at" in after.attributes
        assert {e.evidence_id for e in after.evidence} == {e.evidence_id for e in before.evidence}
        assert after.revision == before.revision + 1
        assert rid in world.verified_edges()  # now visible to verified-only retrieval
        assert world.status(WORKER_ORDERS) == "candidate"

    def test_adds_attestation_when_none_existed(self, world: World) -> None:
        world.cli("promote", world.rid(LONELY))
        assert [e.evidence_id for e in world.relation(LONELY).evidence] == ["promoted:cli"]

    def test_by_match_and_type_and_min_confidence_combine(self, world: World) -> None:
        world.cli("promote", "--match", "*order.events", "--type", "PRODUCES")
        assert world.status(API_ORDERS) == "verified"
        assert world.status(WORKER_ORDERS) == "candidate"
        world.cli("promote", "--min-confidence", "0.55", "--type", "CONSUMES")
        assert world.status(WORKER_ORDERS) == "verified"
        assert world.status(LONELY) == "candidate"  # 0.5 < 0.55 and not CONSUMES

    def test_confidence_never_lowered(self, tmp_path: Path) -> None:
        w = World(tmp_path, "[ontology.promotion]\nverified_confidence = 0.7\n")
        w.seed({**MANIFEST, "relations": [{**RELATIONS[0], "confidence": 0.8}]})
        w.cli("promote", "--all")
        assert w.relation(API_ORDERS).confidence == 0.8

    def test_dry_run_changes_nothing(self, world: World) -> None:
        result = world.cli("promote", "--all", "--dry-run")
        assert "would promote 3" in result.output
        assert {s.item.status for s in world.stored().values()} == {"candidate"}

    def test_unresolved_held_back_unless_included(self, world: World) -> None:
        out = world.cli("promote", "--all").output
        assert "held back 1" in out and "--include-unresolved" in out
        assert world.status(DYN) == "candidate" and world.status(LONELY) == "verified"
        world.cli("promote", "--all", "--include-unresolved")
        assert world.status(DYN) == "verified"

    def test_empty_selection_exit_code(self, world: World) -> None:
        world.cli("promote", "--match", "nothing*", exit_code=1)
        world.cli("promote", "--match", "nothing*", "--allow-empty")
        world.cli("promote", exit_code=2)  # no selector at all is a usage error

    def test_only_unresolved_selected_counts_as_empty(self, world: World) -> None:
        result = world.cli("promote", world.rid(DYN), exit_code=1)
        assert "held back 1" in result.output

    def test_disabled_ontology_is_an_error(self, tmp_path: Path) -> None:
        root = tmp_path / "plain"
        (root / ".git").mkdir(parents=True)
        result = CliRunner().invoke(
            main, ["ontology", "promote", "--list", "--pipeline-id", "shop", "--cwd", str(root)])
        assert result.exit_code != 0 and "[ontology]" in result.output


class TestChecklist:
    def test_parse_ticked_and_unticked(self) -> None:
        text = (
            f"- [x] `{API_ORDERS}`\n"
            f"- [ ] `{WORKER_ORDERS}`\n"
            f"* [X] `{LONELY}` - 0.50 (scan#1) reviewed by me\n"
            "  - [x]   `Service:a -OWNS-> Team:b`\n"
            f"not a list line [x] `{DYN}`\n"
        )
        assert parse_checklist(text) == [
            ("Service:order-api", "PRODUCES", "KafkaTopic:order.events"),
            ("Service:order-api", "PRODUCES", "KafkaTopic:lonely.topic"),
            ("Service:a", "OWNS", "Team:b"),
        ]

    def test_from_checklist_promotes_only_ticked(self, world: World, tmp_path: Path) -> None:
        review = tmp_path / "review.md"
        review.write_text(
            f"# Review\n- [x] `{API_ORDERS}` (0.60) seen in code\n- [ ] `{WORKER_ORDERS}`\n",
            encoding="utf-8",
        )
        world.cli("promote", "--from-checklist", str(review))
        assert world.status(API_ORDERS) == "verified"
        assert world.status(WORKER_ORDERS) == "candidate"

    def test_checklist_with_nothing_ticked_is_empty(self, world: World, tmp_path: Path) -> None:
        review = tmp_path / "review.md"
        review.write_text(f"- [ ] `{API_ORDERS}`\n", encoding="utf-8")
        world.cli("promote", "--from-checklist", str(review), exit_code=1)


class TestDemote:
    def test_demote_returns_to_candidate_and_leaves_context(self, world: World) -> None:
        rid = world.rid(API_ORDERS)
        world.cli("promote", rid)
        assert rid in world.verified_edges()
        world.cli("demote", rid, "--reason", "topic renamed")
        relation = world.relation(API_ORDERS)
        assert relation.status == "candidate"
        assert relation.attributes["demote_reason"] == "topic renamed"
        assert "promoted_by" not in relation.attributes and relation.evidence
        assert rid not in world.verified_edges()

    def test_demote_selectors_and_empty(self, world: World) -> None:
        world.cli("demote", "--match", "*", exit_code=2)  # --reason is required
        world.cli("demote", "--match", "nothing*", "--reason", "x", exit_code=1)
        world.cli("promote", "--all")
        world.cli("demote", "--type", "CONSUMES", "--reason", "x")
        assert world.status(WORKER_ORDERS) == "candidate"
        assert world.status(API_ORDERS) == "verified"


class TestLimitAndGlobal:
    def test_limit_warns_when_the_namespace_may_hold_more(self, world: World) -> None:
        result = world.runner.invoke(main, [
            "ontology", "promote", "--list", "--limit", "2",
            "--pipeline-id", "shop", "--cwd", str(world.root)])
        assert result.exit_code == 0, result.output
        assert "warning: loaded 2 relation(s)" in result.output  # stderr is mixed in by default
        quiet = world.cli("promote", "--list")
        assert "warning: loaded" not in quiet.output

    def test_demote_accepts_limit(self, world: World) -> None:
        world.cli("promote", "--all")
        result = world.cli("demote", "--match", "*", "--reason", "x", "--limit", "1")
        assert "warning: loaded 1 relation(s)" in result.output and "demoted 1" in result.output

    @pytest.mark.parametrize("command", ["promote", "demote"])
    def test_global_namespace_needs_allow_global(self, world: World, command: str) -> None:
        args = ["ontology", command, "--all" if command == "promote" else "--match", "*",
                "--pipeline-id", "global", "--cwd", str(world.root)]
        if command == "demote":
            args += ["--reason", "x"]
        refused = world.runner.invoke(main, args)
        assert refused.exit_code == 2 and "--allow-global" in refused.output
        allowed = world.runner.invoke(main, [*args, "--allow-global", "--allow-empty"])
        assert allowed.exit_code == 0, allowed.output


class TestSelectPromotions:
    @staticmethod
    def item(key: str, type_: str, dst: str, conf: float = 0.6,
             status: str = "candidate") -> PromotionItem:
        return PromotionItem(key, "Service:s", type_, dst, status, conf)

    def test_reasons(self) -> None:
        items = [
            self.item("a", "PRODUCES", "KafkaTopic:t"),
            self.item("b", "PRODUCES", "KafkaTopic:t", conf=0.3),
            self.item("c", "PRODUCES", "KafkaTopic:unresolved:x"),
            self.item("d", "PRODUCES", "KafkaTopic:pattern:x*"),
            self.item("e", "OWNS", "Team:t"),
            self.item("f", "PRODUCES", "KafkaTopic:t", status="verified"),
        ]
        plan = select_promotions(items, PromotionConfig())
        assert [d.item.key for d in plan.promoted] == ["a"]
        reasons = {d.item.key: d.reason for d in plan.held}
        assert set(reasons) == {"b", "c", "d", "e"}  # verified items are not judged
        assert "confidence" in reasons["b"] and "unresolved" in reasons["c"]
        assert "promotion.types" in reasons["e"]
        waived = select_promotions(items, PromotionConfig(), include_unresolved=True)
        assert {d.item.key for d in waived.promoted} == {"a", "c", "d"}

    def test_require_counterpart_uses_context(self) -> None:
        producer = self.item("p", "PRODUCES", "KafkaTopic:t")
        rules = PromotionConfig(require_counterpart=True)
        assert [d.reason for d in select_promotions([producer], rules).held] == [
            "no CONSUMES counterpart for KafkaTopic:t"]
        consumer = self.item("c", "CONSUMES", "KafkaTopic:t", status="verified")
        plan = select_promotions([producer], rules, context=[producer, consumer])
        assert [d.item.key for d in plan.promoted] == ["p"]


class TestAutoPromote:
    def test_promote_auto_applies_rules_to_store(self, tmp_path: Path) -> None:
        w = World(tmp_path, "[ontology.promotion]\nrequire_counterpart = true\nmin_confidence = 0.5\n")
        w.seed()
        out = w.cli("promote", "--auto").output
        assert w.status(API_ORDERS) == "verified" and w.status(WORKER_ORDERS) == "verified"
        assert w.status(LONELY) == "candidate" and w.status(DYN) == "candidate"
        assert "no CONSUMES counterpart" in out
        assert w.relation(API_ORDERS).attributes["promoted_by"].startswith("auto:")

    def test_min_confidence_rule(self, tmp_path: Path) -> None:
        w = World(tmp_path, "[ontology.promotion]\nmin_confidence = 0.55\n")
        w.seed()
        w.cli("promote", "--auto")
        assert w.status(LONELY) == "candidate"  # 0.5
        assert w.status(API_ORDERS) == "verified"

    def test_seed_with_config(self, tmp_path: Path) -> None:
        w = World(tmp_path, "[ontology.promotion]\nauto = true\n")
        report = w.seed(auto_promote=w.config.ontology.promotion)
        assert len(report.promoted) == 2 and len(report.held_back) == 2
        assert w.status(API_ORDERS) == "verified" and w.status(DYN) == "candidate"
        # confidence 0.5 (lonely) < min_confidence 0.6 is held back too
        assert w.status(LONELY) == "candidate"
        assert report.to_dict()["promotion"] == {
            "auto": True, "promoted": 2, "held_back": 2, "held": report.held_back}
        relation = w.relation(API_ORDERS)
        assert relation.confidence == 0.9
        assert relation.attributes["promoted_by"].startswith("auto:")
        assert w.seed(auto_promote=w.config.ontology.promotion).relations_unchanged == 4  # idempotent

    def test_seed_flag(self, world: World, tmp_path: Path) -> None:
        manifest = tmp_path / "m.json"
        manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
        args = ["ontology", "seed", str(manifest), "--cwd", str(world.root), "--json"]
        result = world.runner.invoke(main, [*args, "--auto-promote"])
        assert result.exit_code == 0, result.output
        report = json.loads(result.output[result.output.index("{"):])
        assert report["promotion"]["promoted"] == 2 and report["promotion"]["held_back"] == 2
        assert world.status(API_ORDERS) == "verified"

    def test_no_auto_promote_overrides_config(self, tmp_path: Path) -> None:
        w = World(tmp_path, "[ontology.promotion]\nauto = true\n")
        manifest = tmp_path / "m.json"
        manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
        result = w.runner.invoke(main, [
            "ontology", "seed", str(manifest), "--cwd", str(w.root), "--no-auto-promote",
            "--json"])
        assert result.exit_code == 0, result.output
        report = json.loads(result.output[result.output.index("{"):])
        assert report["promotion"]["auto"] is False and report["promotion"]["promoted"] == 0
        assert {s.item.status for s in w.stored().values()} == {"candidate"}

    def test_seed_require_counterpart(self, tmp_path: Path) -> None:
        w = World(tmp_path, "[ontology.promotion]\nauto = true\nrequire_counterpart = true\n"
                            "min_confidence = 0.5\n")
        w.seed(auto_promote=w.config.ontology.promotion)
        assert w.status(API_ORDERS) == "verified" and w.status(WORKER_ORDERS) == "verified"
        assert w.status(LONELY) == "candidate"  # confident enough, but nobody consumes it

    def test_status_override_wins_over_auto_promote(self, tmp_path: Path) -> None:
        w = World(tmp_path, "[ontology.promotion]\nauto = true\n")
        report = w.seed(status_override="candidate", auto_promote=w.config.ontology.promotion)
        assert report.promoted == [] and not report.auto_promote
        assert w.status(API_ORDERS) == "candidate"
