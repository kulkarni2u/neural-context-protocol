"""``ncp ontology scan``: a reference exporter from a polyglot monorepo to a seed manifest."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.config import load_config
from ncp.ontology.facade import OntologyError, OntologyFacade
from ncp.ontology.scan import _kafka_usages, _service_name, scan_repository
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


def test_k8s_name_parsing():
    head = "kind: Deployment\n"
    assert _service_name("d.yaml", head + "metadata:\n  name: web\n") == ("web", 1)
    assert _service_name("d.yaml", head + "metadata:\n  labels:\n    a: b\n  name: \"web-2\"\n")[0] == "web-2"
    assert _service_name("d.yaml", head + "metadata:\n  name: {{ .Release.Name }}\n")[0] is None
    # A templated sibling key (namespace) must not hide the literal name.
    helm = head + "metadata:\n  namespace: {{ .Release.Namespace }}\n  name: api\n"
    assert _service_name("d.yaml", helm)[0] == "api"
    nested = head + "metadata:\n  labels:\n    name: wrong\nspec:\n  name: also-wrong\n"
    assert _service_name("d.yaml", nested)[0] is None


def test_yaml_binding_parsing():
    text = (
        "bindings:\n"
        "  orders-in-0:\n"
        "    group: g\n"
        "    destination: order.events\n"
        "  pay-out-0:\n"
        "    destination: \"pay.events\"\n"
        "  empty-in-1:\n"
        "    group: g\n"
        "  other:\n"
        "    destination: not.this\n"
    )
    assert list(_kafka_usages("application.yml", text)) == [
        ("CONSUMES", "order.events", 4), ("PRODUCES", "pay.events", 6)]


def test_scan_yaml_regexes_are_linear():
    payload = "\t\t\n" * 50_000
    started = time.perf_counter()
    assert _service_name("k8s/deploy.yaml", "kind: Deployment\nmetadata:\n" + payload + "x") is not None
    assert list(_kafka_usages("application.yml", "orders-in-0:\n" + payload + "x")) == []
    assert time.perf_counter() - started < 1.0


# -- Spring topic resolution and coverage --------------------------------------------------


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _spring(tmp_path: Path, files: dict[str, str]) -> Path:
    """One Gradle module ``svc`` with a Dockerfile, plus ``files`` under it."""
    base = {"svc/build.gradle": "plugins { id 'java' }\n", "svc/Dockerfile": "FROM x\n"}
    return _write(tmp_path / "repo", {**base, **{f"svc/{k}": v for k, v in files.items()}})


def _topic(manifest, key: str):
    return next(e for e in manifest.entities if e.type == "KafkaTopic" and e.key == key)


def _edges(manifest) -> dict[tuple[str, str, str], object]:
    return {(r.src, r.type, r.dst): r for r in manifest.relations
            if r.type in ("PRODUCES", "CONSUMES")}


JAVA = "src/main/java/com/acme/K.java"
KT = "src/main/kotlin/com/acme/K.kt"


class TestSpringResolution:
    def test_property_placeholder_from_properties(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            "src/main/resources/application.properties": "app.topic=orders.v1\n",
            JAVA: 'class K { @KafkaListener(topics = "${app.topic}") void f() {} }\n',
        })
        result = scan_repository(root)
        topic = _topic(result.manifest, "orders.v1")
        assert topic.attributes["resolved_from"] == "property:app.topic"
        edge = _edges(result.manifest)[("Service:svc", "CONSUMES", "KafkaTopic:orders.v1")]
        assert edge.status == "candidate"
        kafka = result.summary["kafka"]
        assert kafka["resolved_via_property"] == 1 and kafka["matched_sites"] == 1

    def test_nested_yaml_default_and_list_forms(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            "src/main/resources/application.yml": (
                "messaging:\n  orders-topic: \"mgr.topic\"  # comment\n  other: 'x'\n"
                "list:\n  - a: b\nmulti: a.one,a.two\n---\nsecond: \"${messaging.orders-topic}\"\n"
            ),
            JAVA: (
                "class K {\n"
                '  @KafkaListener(topics = "${messaging.orders-topic}")\n  void a() {}\n'
                '  @KafkaListener(topics = {"${second}", "${missing.key:fallback.topic}", "plain"})\n'
                "  void b() {}\n"
                '  @KafkaListener(topics = "${multi}")\n  void c() {}\n}\n'
            ),
        })
        manifest = scan_repository(root).manifest
        names = _keys(manifest, "KafkaTopic")
        assert names == {"mgr.topic", "fallback.topic", "plain", "a.one", "a.two"}
        assert _topic(manifest, "mgr.topic").attributes["resolved_from"] == "property:messaging.orders-topic"
        assert _topic(manifest, "fallback.topic").attributes["resolved_from"] == "default:missing.key"
        assert _topic(manifest, "plain").attributes["resolved_from"] == "literal"

    def test_nested_placeholder_and_cycle(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            "src/main/resources/application.properties": (
                "env=prod\nbase=${env}.orders\nloop.a=${loop.b}\nloop.b=${loop.a}\n"),
            JAVA: (
                'class K {\n  @KafkaListener(topics = "${base}") void a() {}\n'
                '  @KafkaListener(topics = "${loop.a}") void b() {}\n}\n'),
        })
        manifest = scan_repository(root).manifest
        assert "prod.orders" in _keys(manifest, "KafkaTopic")
        assert _topic(manifest, "unresolved:loop.a").attributes["unresolved"] is True

    def test_profile_precedence(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            "src/main/resources/application.properties": "both=base.topic\n",
            "src/main/resources/application-prod.properties": "both=prod.topic\nonly=prod.only\n",
            "src/main/resources/application-dev.yml": "only: dev.only\n",
            JAVA: (
                'class K {\n  @KafkaListener(topics = "${both}") void a() {}\n'
                '  @KafkaListener(topics = "${only}") void b() {}\n}\n'),
        })
        manifest = scan_repository(root).manifest
        assert _keys(manifest, "KafkaTopic") == {"base.topic", "dev.only"}  # base wins; dev < prod
        assert _topic(manifest, "dev.only").attributes["profile"] == "dev"
        assert "profile" not in _topic(manifest, "base.topic").attributes

    def test_module_scope_then_repo_fallback(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            "src/main/resources/application.properties": "t=local.topic\n",
            JAVA: 'class K { @KafkaListener(topics = "${t}") void a() {} '
                  '@KafkaListener(topics = "${shared}") void b() {} }\n',
        })
        _write(root, {
            "other/build.gradle": "plugins {}\n", "other/Dockerfile": "FROM x\n",
            "other/src/main/resources/application.properties": "t=other.topic\nshared=shared.topic\n",
        })
        assert _keys(scan_repository(root).manifest, "KafkaTopic") == {"local.topic", "shared.topic"}

    def test_java_constants_and_class_name(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            "src/main/java/com/acme/Topics.java": (
                "public final class Topics {\n"
                '  public static final String ORDERS = "orders.const";\n'
                '  static public final String PROP = "${app.t}";\n}\n'),
            "src/main/resources/application.properties": "app.t=from.prop\n",
            JAVA: (
                "class K {\n"
                "  @KafkaListener(topics = Topics.ORDERS) void a() {}\n"
                "  @KafkaListener(topics = {Topics.PROP, ORDERS}) void b() {}\n"
                "  void c() { kafkaTemplate.send(Topics.ORDERS, x); }\n}\n"),
        })
        manifest = scan_repository(root).manifest
        assert _keys(manifest, "KafkaTopic") == {"orders.const", "from.prop"}
        assert _topic(manifest, "orders.const").attributes["resolved_from"] == "constant:ORDERS"
        assert ("Service:svc", "PRODUCES", "KafkaTopic:orders.const") in _edges(manifest)

    def test_kotlin_const_val_and_object_val(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            "src/main/kotlin/com/acme/Topics.kt": (
                'const val TOP_LEVEL = "top.level"\n'
                "object Topics {\n  const val ORDERS = \"orders.kt\"\n"
                '  val audit = "audit.kt"\n  val PLACE = "\\${kt.place}"\n}\n'),
            "src/main/resources/application.yaml": "kt:\n  place: kt.resolved\n",
            KT: (
                "class K {\n"
                "  @KafkaListener(topics = [Topics.ORDERS, Topics.audit, TOP_LEVEL, Topics.PLACE])\n"
                "  fun a() {}\n}\n"),
        })
        assert _keys(scan_repository(root).manifest, "KafkaTopic") == {
            "orders.kt", "audit.kt", "top.level", "kt.resolved"}

    def test_value_field_used_in_send(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            "src/main/resources/application.properties": "out.topic=out.events\nk.topic=kt.out\n",
            JAVA: (
                "class K {\n"
                '  @Value("${out.topic}")\n  private String outTopic;\n'
                "  void f() { this.kafkaTemplate.send(outTopic, k, v); }\n}\n"),
            KT: (
                "class KK(private val template: KafkaTemplate<String, String>) {\n"
                '  @Value("\\${k.topic}") lateinit var ktTopic: String\n'
                "  fun f() { template.send(ktTopic, v) }\n}\n"),
        })
        manifest = scan_repository(root).manifest
        assert _keys(manifest, "KafkaTopic") == {"out.events", "kt.out"}
        assert _topic(manifest, "out.events").attributes["resolved_from"] == "property:out.topic"

    def test_producer_record_with_constant(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            JAVA: (
                "import org.apache.kafka.clients.producer.ProducerRecord;\n"
                "class K {\n"
                '  static final String T = "rec.topic";\n'
                "  void f(ProducerRecord<String, String> unused) {}\n"
                "  void g() { var r = new ProducerRecord<String, String>(T, k, v); }\n"
                '  void h() { var r = new ProducerRecord<>("lit.topic", v); }\n}\n'),
        })
        manifest = scan_repository(root).manifest
        assert _keys(manifest, "KafkaTopic") == {"rec.topic", "lit.topic"}
        assert _topic(manifest, "rec.topic").attributes["resolved_from"] == "constant:T"

    def test_concatenation(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            JAVA: ('class K {\n  static final String P = "pre";\n'
                   '  @KafkaListener(topics = P + ".events") void a() {}\n}\n'),
        })
        assert _keys(scan_repository(root).manifest, "KafkaTopic") == {"pre.events"}

    def test_unresolved_placeholder_is_a_candidate_even_in_verified_mode(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            JAVA: ('class K {\n  @KafkaListener(topics = "${messaging.orders-topic}") void a() {}\n'
                   "  void b() { kafkaTemplate.send(topicVar, v); }\n}\n"),
        })
        for mode in ("candidate", "verified"):
            result = scan_repository(root, heuristics=mode)  # type: ignore[arg-type]
            topic = _topic(result.manifest, "unresolved:messaging.orders-topic")
            assert topic.attributes["unresolved"] is True
            assert topic.attributes["expression"] == "${messaging.orders-topic}"
            edges = _edges(result.manifest)
            unresolved = [e for (_s, _t, dst), e in edges.items() if dst.startswith("KafkaTopic:unresolved:")]
            assert len(unresolved) == 2 and {e.status for e in unresolved} == {"candidate"}
        assert _topic(result.manifest, "unresolved:topicVar").attributes["expression"] == "topicVar"
        assert result.summary["kafka"]["unresolved_placeholders"] == 2
        assert any("could not be resolved" in w for w in result.summary["warnings"])

    def test_spel_is_unresolved(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            JAVA: """class K {\n  @KafkaListener(topics = "#{'${a}'.split(',')}") void a() {}\n}\n""",
        })
        manifest = scan_repository(root).manifest
        (key,) = _keys(manifest, "KafkaTopic")
        assert key.startswith("unresolved:#{")
        assert _topic(manifest, key).attributes["unresolved"] is True

    def test_topic_pattern(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            JAVA: ('class K {\n  @KafkaListener(topicPattern = "orders\\\\..*") void a() {}\n'
                   '  @KafkaListener(topicPattern = "plain.topic") void b() {}\n}\n'),
        })
        manifest = scan_repository(root).manifest
        assert _keys(manifest, "KafkaTopic") == {"pattern:orders\\..*", "plain.topic"}
        assert _topic(manifest, "pattern:orders\\..*").attributes["pattern"] == "orders\\..*"

    def test_resolved_manifest_seeds_in_dry_run(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {JAVA: 'class K { @KafkaListener(topics = "${x.y}") void a() {} }\n'})
        out = tmp_path / "m.json"
        runner = CliRunner()
        assert runner.invoke(main, ["ontology", "scan", str(root), "-o", str(out)]).exit_code == 0
        project = tmp_path / "proj"
        project.mkdir()
        seeded = runner.invoke(main, ["ontology", "seed", str(out), "--dry-run", "--pipeline-id", "p",
                                      "--cwd", str(project), "--json"])
        assert seeded.exit_code == 0, seeded.output


class TestKafkaCoverage:
    def test_counts_and_examples(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            "src/main/resources/application.properties": "p.t=prop.topic\n",
            JAVA: (
                "class K {\n"
                '  static final String C = "const.topic";\n'
                '  @KafkaListener(topics = "${p.t}") void a() {}\n'
                "  @KafkaListener(topics = C) void b() {}\n"
                '  @KafkaListener(topics = "lit") void c() {}\n'
                "  void d() { kafkaTemplate.send(unknownVar, v); }\n"
                "  void e() { kafkaTemplate.send(buildTopic(), v); }\n"
                "  void f() { kafkaTemplate.send(msg); }\n}\n"),
        })
        kafka = scan_repository(root).summary["kafka"]
        assert kafka["kafka_like_sites"] == 6
        assert kafka["matched_sites"] == 3
        assert (kafka["resolved_via_property"], kafka["resolved_via_constant"]) == (1, 1)
        assert kafka["unresolved_placeholders"] == 2  # unknownVar, msg (bare identifiers)
        assert kafka["unmatched_sites"] == 1  # buildTopic(): a method call is not understood
        assert kafka["files_examined"] == 2 and kafka["skipped_no_owner"] == 0
        assert len(kafka["unmatched_examples"]) <= 5

    def test_counts_are_per_site_not_per_line(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            "src/main/resources/application.properties": "mgr.topic=mgr\n",
            "src/main/java/com/acme/Topics.java": 'class Topics { static final String ORDERS = "orders"; }\n',
            JAVA: (
                "class K {\n"
                '  @KafkaListener(topics = {Topics.ORDERS, "${missing.key}"}) void a() {}\n'
                '  void b() { kafkaTemplate.send("${mgr.topic}", "x"); kafkaTemplate.send(buildTopic(), "y"); }\n}\n'),
        })
        result = scan_repository(root)
        kafka = result.summary["kafka"]
        assert kafka["kafka_like_sites"] == 3  # one listener + two sends
        assert kafka["matched_sites"] == 2  # the listener (one resolved element) + the first send
        assert kafka["unresolved_placeholders"] == 1  # missing.key, on a matched site
        assert kafka["unmatched_sites"] == 1
        assert any("K.java:3: " in e and "buildTopic()" in e for e in kafka["unmatched_examples"])
        assert any("could not be resolved" in w for w in result.summary["warnings"])
        assert "unresolved:missing.key" in _keys(result.manifest, "KafkaTopic")

    def test_zero_topic_warning_names_the_cause(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            JAVA: ("class K {\n  void a() { kafkaTemplate.send(build(), v); }\n"
                   "  void b() { kafkaTemplate.send(other(), v); }\n}\n"),
        })
        result = scan_repository(root)
        assert result.summary["topics"] == 0
        (warning,) = result.summary["warnings"]
        assert "0 topics" in warning and "method call" in warning
        assert "svc/src/main/java/com/acme/K.java:2:" in warning
        examples = result.summary["kafka"]["unmatched_examples"]
        assert examples[0].startswith("svc/src/main/java/com/acme/K.java:2: ")

    def test_skipped_no_owner_explains_zero_topics(self, tmp_path: Path) -> None:
        root = _write(tmp_path / "repo", {
            "lib/K.java": 'class K { @KafkaListener(topics = "t") void a() {} }\n',
            "lib/L.java": 'class L { void a() { kafkaTemplate.send("u", v); } }\n',
        })
        result = scan_repository(root)
        kafka = result.summary["kafka"]
        assert kafka["skipped_no_owner"] == 2 and kafka["kafka_like_sites"] == 2
        assert kafka["unmatched_sites"] == 2 and result.summary["topics"] == 0
        assert any("no module" in w and "skipped" in w for w in result.summary["warnings"])

    def test_v1_without_service_is_skipped(self, tmp_path: Path) -> None:
        root = _write(tmp_path / "repo", {
            "svc/build.gradle": "plugins {}\n",  # a module but no Dockerfile: no service
            "svc/K.java": 'class K { @KafkaListener(topics = "t") void a() {} }\n',
        })
        assert scan_repository(root, schema_version=1).summary["kafka"]["skipped_no_owner"] == 1
        assert scan_repository(root).summary["kafka"]["skipped_no_owner"] == 0

    def test_majority_unmatched_warns_even_with_topics(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            JAVA: ('class K {\n  @KafkaListener(topics = "ok") void a() {}\n'
                   "  void b() { kafkaTemplate.send(f(), v); }\n"
                   "  void c() { kafkaTemplate.send(g(), v); }\n}\n"),
        })
        result = scan_repository(root)
        assert result.summary["topics"] == 1
        assert any("2 of 3" in w for w in result.summary["warnings"])

    def test_clean_scan_has_no_kafka_warning(self, repo: Path) -> None:
        result = scan_repository(repo)
        assert result.summary["warnings"] == []
        assert result.summary["kafka"]["unmatched_sites"] == 0

    def test_heuristics_off_skips_everything(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {JAVA: 'class K { @KafkaListener(topics = "t") void a() {} }\n'})
        result = scan_repository(root, heuristics="off")
        assert result.summary["kafka"]["kafka_like_sites"] == 0 and result.summary["warnings"] == []


def test_spring_parsers_are_linear():
    started = time.perf_counter()
    payload = "a: " + "${" * 20_000 + "\n" + "  " * 50_000 + "\n" + "x" * 100_000
    from ncp.ontology.scan import _CONFIG_FILE, _extract_constants, _parse_yaml_props, _split_top
    assert _CONFIG_FILE.fullmatch("application-" + "-" * 50_000 + "x") is None
    _parse_yaml_props(payload)
    _extract_constants("static String " * 20_000 + "\n" + "val " * 20_000, ".kt")
    _extract_constants("String " * 50_000 + "static\n", ".java")
    _split_top('("' * 20_000, ",")
    assert time.perf_counter() - started < 3.0


class TestCli:
    def test_scan_prints_coverage_and_warnings(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {
            JAVA: "class K {\n  void a() { kafkaTemplate.send(build(), v); }\n}\n"})
        out = tmp_path / "m.json"
        result = CliRunner().invoke(main, ["ontology", "scan", str(root), "-o", str(out)])
        assert result.exit_code == 0, result.output
        assert "Kafka coverage" in result.output and "kafka-like sites" in result.output
        assert "warning: found 1 Kafka-like site(s) but extracted 0 topics" in result.output
        assert "K.java:2:" in result.output

    def test_scan_json_includes_kafka_summary(self, tmp_path: Path) -> None:
        root = _spring(tmp_path, {JAVA: 'class K { @KafkaListener(topics = "t") void a() {} }\n'})
        result = CliRunner().invoke(main, ["ontology", "scan", str(root), "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["summary"]["kafka"]["matched_sites"] == 1
        assert payload["manifest"]["entities"] and payload["output"] is None

    def test_scan_never_opens_the_store(self, tmp_path: Path, monkeypatch) -> None:
        import ncp.cli as cli_module

        def boom(*_a, **_k):
            raise AssertionError("scan must not open the store")
        monkeypatch.setattr(cli_module, "create_store", boom)
        root = _spring(tmp_path, {JAVA: 'class K { @KafkaListener(topics = "t") void a() {} }\n'})
        result = CliRunner().invoke(main, ["ontology", "scan", str(root), "-o", str(tmp_path / "m.json")])
        assert result.exit_code == 0, result.output

    def test_seed_dry_run_with_ontology_disabled_needs_no_store(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        import ncp.cli as cli_module

        def boom(*_a, **_k):
            raise AssertionError("dry run must not open the store / embedding adapter")
        monkeypatch.setattr(cli_module, "create_store", boom)
        root = _spring(tmp_path, {JAVA: 'class K { @KafkaListener(topics = "t") void a() {} }\n'})
        out = tmp_path / "m.json"
        assert CliRunner().invoke(main, ["ontology", "scan", str(root), "-o", str(out),
                                         "--pipeline-id", "p"]).exit_code == 0
        project = tmp_path / "proj"
        project.mkdir()
        result = CliRunner().invoke(main, ["ontology", "seed", str(out), "--dry-run",
                                           "--cwd", str(project)])
        assert result.exit_code == 0, result.output
        assert "offline" in result.output and "disabled" in result.output.split("offline", 1)[1]
        assert "fastembed" not in result.output.lower()
        assert not (project / ".ncp").exists()

    def test_seed_dry_run_reports_invalid_manifest_offline(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"pipeline": "p", "entities": [{"type": "Nope", "key": "x"}]}))
        project = tmp_path / "proj"
        project.mkdir()
        result = CliRunner().invoke(main, ["ontology", "seed", str(bad), "--dry-run",
                                           "--cwd", str(project)])
        assert result.exit_code != 0 and "unknown entity type" in result.output

    def test_seed_without_dry_run_still_needs_ontology(self, tmp_path: Path) -> None:
        manifest = tmp_path / "m.json"
        manifest.write_text(json.dumps({"pipeline": "p", "entities": []}))
        project = tmp_path / "proj"
        project.mkdir()
        result = CliRunner().invoke(main, ["ontology", "seed", str(manifest), "--cwd", str(project)])
        assert result.exit_code != 0 and "disabled" in result.output
