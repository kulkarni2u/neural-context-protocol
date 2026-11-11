"""``ncp ontology scan``: a reference exporter from a polyglot monorepo to a seed manifest."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.config import load_config
from ncp.ontology.facade import OntologyError, OntologyFacade
from ncp.ontology.scan import scan_repository
from ncp.ontology.seed import memory_chunk_writer, seed_ontology
from ncp.stores.sqlite import SQLiteStore

FILES = {
    "package.json": '{"private": true, "workspaces": ["services/*", "libs/*"]}',
    ".github/CODEOWNERS": (
        "# default owners\n"
        "*            @acme/platform\n"
        "/services/payments/ @acme/payments\n"
        "/libs/       @acme/core @alice\n"
    ),
    # Maven service + library
    "services/payments/pom.xml": """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <groupId>com.acme</groupId>
  <artifactId>payments</artifactId>
  <dependencies>
    <dependency>
      <groupId>com.acme</groupId>
      <artifactId>ledger-client</artifactId>
    </dependency>
    <dependency><groupId>org.springframework.kafka</groupId><artifactId>spring-kafka</artifactId></dependency>
  </dependencies>
</project>""",
    "services/payments/Dockerfile": "FROM eclipse-temurin:21\n",
    "services/payments/k8s/deployment.yaml": (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: payments-svc\n"
        "  labels:\n    app: payments\n"
    ),
    "services/payments/src/main/java/com/acme/Payments.java": (
        "class Payments {\n"
        '  @KafkaListener(topics = "order.events", groupId = "payments")\n'
        "  void onOrder(String o) {\n"
        '    kafkaTemplate.send("payment.events", o);\n'
        "  }\n}\n"
    ),
    "services/payments/ncp-ontology.toml": (
        '[[entities]]\ntype = "Database"\nkey = "ledger-db"\n\n'
        '[[relations]]\nsrc = "Service:payments-svc"\ntype = "DEPENDS_ON"\ndst = "ledger-db"\n'
        'evidence = "payments-svc stores captures in ledger-db (DATABASE_URL in its chart)."\n'
    ),
    "libs/ledger-client/pom.xml": """<project>
  <parent><groupId>com.acme</groupId><artifactId>parent</artifactId></parent>
  <artifactId>ledger-client</artifactId>
</project>""",
    # npm service + library
    "services/checkout/package.json": json.dumps({
        "name": "@acme/checkout",
        "dependencies": {"@acme/ui-kit": "workspace:*", "express": "^4.0.0", "kafkajs": "^2"},
    }, indent=2),
    "services/checkout/Dockerfile": "FROM node:22\n",
    "services/checkout/chart/Chart.yaml": "apiVersion: v2\nname: checkout-web\nversion: 1.0.0\n",
    "services/checkout/src/producer.ts": (
        "import { Kafka } from 'kafkajs'\n"
        "await producer.send({ topic: 'order.events', messages })\n"
    ),
    "libs/ui-kit/package.json": '{"name": "@acme/ui-kit"}',
    # Python tool + library; Kafka only in a test file (ignored)
    "tools/report/pyproject.toml": (
        '[project]\nname = "Acme_Report"\ndependencies = ["acme-shared>=1.0", "requests"]\n'
    ),
    "tools/report/tests/test_feed.py": 'from kafka import KafkaConsumer\nKafkaConsumer("ignored-topic")\n',
    "libs/shared/pyproject.toml": '[project]\nname = "acme.shared"\n',
    # Go service + library
    "services/notifier/go.mod": (
        "module github.com/acme/notifier\n\ngo 1.22\n\nrequire (\n"
        "\tgithub.com/acme/gokit v0.0.0\n\tgithub.com/segmentio/kafka-go v0.4.47\n)\n"
    ),
    "services/notifier/Dockerfile": "FROM golang:1.22\n",
    "services/notifier/main.go": (
        "package main\nimport kafka \"github.com/segmentio/kafka-go\"\n"
        "var r = kafka.NewReader(kafka.ReaderConfig{\n\tBrokers: brokers,\n"
        '\tTopic:   "payment.events",\n})\n'
    ),
    "libs/gokit/go.mod": "module github.com/acme/gokit\n\ngo 1.22\n",
    # Gradle multi-project under jvm/
    "jvm/settings.gradle": "include ':app', ':core'\n",
    "jvm/build.gradle": "allprojects { repositories { mavenCentral() } }\n",
    "jvm/app/build.gradle": "dependencies {\n    implementation project(':core')\n}\n",
    "jvm/core/build.gradle": "plugins { id 'java-library' }\n",
    # Ignored directories
    "node_modules/leftpad/package.json": '{"name": "leftpad"}',
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "acme-monorepo"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _rels(manifest) -> set[tuple[str, str, str]]:
    return {(r.src, r.type, r.dst) for r in manifest.relations}


def _keys(manifest, entity_type: str) -> set[str]:
    return {e.key for e in manifest.entities if e.type == entity_type}


class TestScanV2:
    def test_modules_services_and_dependencies(self, repo: Path) -> None:
        result = scan_repository(repo, pipeline="shop")
        manifest = result.manifest
        assert manifest.schema_version == 2 and manifest.source == "acme-monorepo"
        assert _keys(manifest, "Module") == {
            "services/payments", "libs/ledger-client", "services/checkout", "libs/ui-kit",
            "tools/report", "libs/shared", "services/notifier", "libs/gokit", "jvm/app",
            "jvm/core",
        }  # no root workspace, no Gradle aggregator, nothing from node_modules
        assert _keys(manifest, "Service") == {"payments-svc", "checkout-web", "notifier"}
        rels = _rels(manifest)
        for src, dst in [("services/payments", "libs/ledger-client"),
                         ("services/checkout", "libs/ui-kit"), ("tools/report", "libs/shared"),
                         ("services/notifier", "libs/gokit"), ("jvm/app", "jvm/core")]:
            assert (f"Module:{src}", "DEPENDS_ON", f"Module:{dst}") in rels
        assert ("Module:services/payments", "BUILDS", "Service:payments-svc") in rels
        assert ("Module:services/checkout", "BUILDS", "Service:checkout-web") in rels
        assert ("Repo:acme-monorepo", "CONTAINS", "Module:jvm/core") in rels
        # Module aliases carry package identities so seeds resolve by name too.
        checkout = next(e for e in manifest.entities if e.key == "services/checkout")
        assert "@acme/checkout" in checkout.aliases
        assert result.summary["modules"] == 10 and result.summary["services"] == 3

    def test_evidence_points_at_file_and_line(self, repo: Path) -> None:
        manifest = scan_repository(repo).manifest
        by_triple = {(r.src, r.type, r.dst): r for r in manifest.relations}
        dep = by_triple[("Module:jvm/app", "DEPENDS_ON", "Module:jvm/core")]
        assert dep.attestation == "jvm/app/build.gradle#L2" and dep.status == "verified"
        builds = by_triple[("Module:services/payments", "BUILDS", "Service:payments-svc")]
        assert builds.attestation == "services/payments/k8s/deployment.yaml#L2"

    def test_codeowners_last_match_wins(self, repo: Path) -> None:
        rels = _rels(scan_repository(repo).manifest)
        assert ("Team:acme/payments", "OWNS", "Module:services/payments") in rels
        assert ("Team:acme/payments", "OWNS", "Service:payments-svc") in rels
        assert ("Team:acme/platform", "OWNS", "Module:services/payments") not in rels
        assert ("Team:acme/platform", "OWNS", "Service:checkout-web") in rels
        assert {("Team:acme/core", "OWNS", "Module:libs/shared"),
                ("Team:alice", "OWNS", "Module:libs/shared")} <= rels

    def test_kafka_edges_are_candidates_and_tests_are_skipped(self, repo: Path) -> None:
        manifest = scan_repository(repo).manifest
        kafka = {(r.src, r.type, r.dst): r for r in manifest.relations
                 if r.type in ("PRODUCES", "CONSUMES")}
        assert set(kafka) == {
            ("Service:payments-svc", "CONSUMES", "KafkaTopic:order.events"),
            ("Service:payments-svc", "PRODUCES", "KafkaTopic:payment.events"),
            ("Service:checkout-web", "PRODUCES", "KafkaTopic:order.events"),
            ("Service:notifier", "CONSUMES", "KafkaTopic:payment.events"),
        }
        assert {r.status for r in kafka.values()} == {"candidate"}
        assert "ignored-topic" not in _keys(manifest, "KafkaTopic")
        with_tests = scan_repository(repo, include_tests=True).manifest
        assert "ignored-topic" in _keys(with_tests, "KafkaTopic")

    @pytest.mark.parametrize(("mode", "expected"), [("off", set()), ("verified", {"verified"})])
    def test_heuristic_modes(self, repo: Path, mode: str, expected: set) -> None:
        manifest = scan_repository(repo, heuristics=mode).manifest  # type: ignore[arg-type]
        assert {r.status for r in manifest.relations if r.type in ("PRODUCES", "CONSUMES")} == expected

    def test_hand_written_module_manifest_is_merged(self, repo: Path) -> None:
        result = scan_repository(repo)
        assert result.summary["hand_manifests"] == ["services/payments/ncp-ontology.toml"]
        assert ("Service:payments-svc", "DEPENDS_ON", "ledger-db") in _rels(result.manifest)
        assert "ledger-db" in _keys(result.manifest, "Database")

    def test_unparseable_build_file_is_a_warning(self, repo: Path) -> None:
        (repo / "libs/broken").mkdir(parents=True)
        (repo / "libs/broken/package.json").write_text("{not json", encoding="utf-8")
        result = scan_repository(repo)
        assert any("libs/broken/package.json" in w for w in result.summary["warnings"])  # type: ignore[union-attr]

    @pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
    def test_respects_gitignore(self, repo: Path) -> None:
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / ".gitignore").write_text("tools/\n", encoding="utf-8")
        assert "tools/report" not in _keys(scan_repository(repo).manifest, "Module")


class TestScanV1:
    def test_v1_keeps_only_v1_types(self, repo: Path) -> None:
        manifest = scan_repository(repo, schema_version=1).manifest
        types = {e.type for e in manifest.entities}
        assert types <= {"Service", "KafkaTopic", "Team", "Database"}
        assert {r.src.split(":", 1)[0] for r in manifest.relations
                if r.type in ("PRODUCES", "CONSUMES")} == {"Service"}
        assert ("Team:acme/payments", "OWNS", "Service:payments-svc") in _rels(manifest)


class TestScanThenSeed:
    def _facade(self, tmp_path: Path) -> tuple[OntologyFacade, SQLiteStore]:
        root = tmp_path / "ncp-project"
        (root / ".git").mkdir(parents=True)
        (root / ".ncp").mkdir()
        (root / ".ncp" / "config.toml").write_text("[ontology]\nenabled = true\n", encoding="utf-8")
        store = SQLiteStore(root / ".ncp" / "store.db")
        return OntologyFacade.from_config(load_config(cwd=root), store), store

    @pytest.mark.parametrize("version", [1, 2])
    def test_scan_output_seeds_cleanly_and_idempotently(
        self, repo: Path, tmp_path: Path, version: int,
    ) -> None:
        facade, store = self._facade(tmp_path)
        manifest = scan_repository(repo, pipeline="shop", schema_version=version).manifest
        writer = memory_chunk_writer(store)
        first = seed_ontology(facade, manifest, write_chunk=writer)
        assert first.relations_created == len(manifest.relations)
        again = seed_ontology(facade, manifest, write_chunk=writer, prune=True)
        assert again.relations_unchanged == len(manifest.relations) and again.relations_pruned == 0

    def test_cli_scan_then_seed(self, repo: Path, tmp_path: Path) -> None:
        facade, _store = self._facade(tmp_path)
        out = tmp_path / "ontology.json"
        runner = CliRunner()
        scanned = runner.invoke(main, ["ontology", "scan", str(repo), "-o", str(out),
                                       "--pipeline-id", "shop"])
        assert scanned.exit_code == 0, scanned.output
        assert "10 modules" in scanned.output and out.exists()
        seeded = runner.invoke(main, ["ontology", "seed", str(out), "--cwd",
                                      str(tmp_path / "ncp-project"), "--json"])
        assert seeded.exit_code == 0, seeded.output
        report = json.loads(seeded.output[seeded.output.index("{"):])
        assert report["relations"]["created"] == len(json.loads(out.read_text())["relations"])

    def test_not_a_directory(self, tmp_path: Path) -> None:
        with pytest.raises(OntologyError, match="not a directory"):
            scan_repository(tmp_path / "missing")
