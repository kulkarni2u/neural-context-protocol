"""``ncp ontology scan`` destination resolution: SpEL ternaries and literal folding, the
external property overlay, ``@ConfigurationProperties`` getters and stable unresolved keys."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.ontology.facade import OntologyError
from ncp.ontology.scan import load_extra_properties, scan_repository

JAVA = "src/main/java/com/acme/K.java"
APP = "src/main/resources/application.properties"
SPEL_KEY = re.compile(r"unresolved:spel:[0-9a-f]{12}")


def _spring(tmp_path: Path, files: dict[str, str]) -> Path:
    base = {"svc/build.gradle": "plugins { id 'java' }\n", "svc/Dockerfile": "FROM x\n"}
    root = tmp_path / "repo"
    for rel, text in {**base, **{f"svc/{k}": v for k, v in files.items()}}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _listener(expr: str, extra: str = "") -> dict[str, str]:
    java = ("class K {\n" + extra + '  @KafkaListener(topics = "' + expr.replace('"', '\\"')
            + '") void a() {}\n}\n')
    return {JAVA: java}


def _raw_listener(topics: str) -> dict[str, str]:
    return {JAVA: f"class K {{\n  @KafkaListener(topics = {topics}) void a() {{}}\n}}\n"}


def _topics(result) -> dict[str, object]:
    return {e.key: e for e in result.manifest.entities if e.type == "KafkaTopic"}


def _edge(result, dst: str):
    return next(r for r in result.manifest.relations if r.dst == f"KafkaTopic:{dst}")


# -- 1. ternary --------------------------------------------------------------------------


def test_ternary_unknown_property_yields_both_branches_as_candidates(tmp_path: Path) -> None:
    root = _spring(tmp_path, _listener("#{'${mode}'=='A' ? 'X' : 'Y'}"))
    result = scan_repository(root, heuristics="verified")
    topics = _topics(result)
    assert set(topics) == {"X", "Y"}
    assert topics["X"].attributes["ternary_branch"] == "true"
    assert topics["Y"].attributes["ternary_branch"] == "false"
    assert topics["X"].attributes["ternary"] == "'${mode}'=='A'"
    assert topics["X"].attributes["resolved_from"] == "spel"
    for key in ("X", "Y"):  # candidates even when heuristics are "verified"
        edge = _edge(result, key)
        assert edge.status == "candidate" and edge.confidence == pytest.approx(0.4)
    assert result.summary["kafka"]["unresolved_placeholders"] == 0


@pytest.mark.parametrize(("expr", "props", "expected"), [
    ("#{'${mode}'=='A' ? 'X' : 'Y'}", "mode=A\n", {"X"}),
    ("#{'${mode}'=='A' ? 'X' : 'Y'}", "mode=B\n", {"Y"}),
    ("#{'${mode}'!='A' ? 'X' : 'Y'}", "mode=A\n", {"Y"}),
    ('#{"${mode}"=="A" ? "X" : "Y"}', "mode=A\n", {"X"}),
    ("#{'${mode:A}'=='A' ? 'X' : 'Y'}", "", {"X"}),
    # literal-only folding and the generalised evaluator
    ("#{'A'=='A' ? 'X' : 'Y'}", "", {"X"}),
    ("#{'A'!='A' ? 'X' : 'Y'}", "", {"Y"}),
    ("#{'X'}", "", {"X"}),
    ("#{'a' + 'b'}", "", {"ab"}),
    ("#{('a' + 'b') == 'ab' ? ('p' + 'q') : 'z'}", "", {"pq"}),
    ("#{'${mode}'=='A' ? 'X' : ('${mode}'=='B' ? 'Y' : 'Z')}", "mode=B\n", {"Y"}),
])
def test_spel_ternary_and_literal_folding(tmp_path: Path, expr: str, props: str,
                                          expected: set[str]) -> None:
    files = {**_listener(expr), **({APP: props} if props else {})}
    assert set(_topics(scan_repository(_spring(tmp_path, files)))) == expected


def test_ternary_nested_unknown_gives_every_branch(tmp_path: Path) -> None:
    expr = "#{'${m}'=='A' ? 'X' : ('${m}'=='B' ? 'Y' : 'Z')}"
    result = scan_repository(_spring(tmp_path, _listener(expr)))
    assert set(_topics(result)) == {"X", "Y", "Z"}


def test_ternary_unresolvable_branch_falls_back_to_unresolved(tmp_path: Path) -> None:
    expr = "#{'${mode}'=='A' ? 'X' : @ghost.topic}"
    result = scan_repository(_spring(tmp_path, _listener(expr)))
    topics = _topics(result)
    (bad,) = [key for key in topics if key != "X"]
    assert SPEL_KEY.fullmatch(bad) and topics[bad].attributes["unresolved"] is True
    assert "X" in topics and result.summary["kafka"]["unresolved_placeholders"] == 1


def test_ternary_candidates_for_send_sites(tmp_path: Path) -> None:
    java = 'class K {\n  void s() { kafkaTemplate.send("#{\'${m}\'==\'A\' ? \'X\' : \'Y\'}", v); }\n}\n'
    assert set(_topics(scan_repository(_spring(tmp_path, {JAVA: java})))) == {"X", "Y"}


# -- 3. external overlay -----------------------------------------------------------------


def test_extra_properties_fill_missing_and_override_repo_values(tmp_path: Path) -> None:
    files = {**_raw_listener('{"${topic.a}", "${topic.b}", "${topic.c:dflt}"}'),
             APP: "topic.a=repo.a\ntopic.b=repo.b\n"}
    root = _spring(tmp_path, files)
    assert set(_topics(scan_repository(root))) == {"repo.a", "repo.b", "dflt"}
    over = scan_repository(root, extra_properties={
        "topic.b": "deploy.b", "TOPIC_C": "deploy.c"})
    assert set(_topics(over)) == {"repo.a", "deploy.b", "deploy.c"}


def test_extra_properties_resolve_an_otherwise_unresolved_topic(tmp_path: Path) -> None:
    root = _spring(tmp_path, _listener("${kafka.topic.orders}"))
    assert set(_topics(scan_repository(root))) == {"unresolved:kafka.topic.orders"}
    result = scan_repository(root, extra_properties={"KAFKA_TOPIC_ORDERS": "orders.v1"})
    assert set(_topics(result)) == {"orders.v1"}
    assert _topics(result)["orders.v1"].attributes["resolved_from"] == (
        "property:kafka.topic.orders")


def test_load_extra_properties_formats_and_order(tmp_path: Path) -> None:
    props = tmp_path / "a.properties"
    props.write_text("x=1\ny=2\n", encoding="utf-8")
    yml = tmp_path / "b.yml"
    yml.write_text("kafka:\n  topic:\n    orders: from-yaml\n", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text("# c\nexport Y=\"3\"\nKAFKA_TOPIC_PAY=pay # note\nbad line\n", encoding="utf-8")
    merged = load_extra_properties([props, yml], [env])
    assert merged == {"x": "1", "y": "2", "kafka.topic.orders": "from-yaml",
                      "Y": "3", "KAFKA_TOPIC_PAY": "pay"}
    with pytest.raises(OntologyError):
        load_extra_properties([tmp_path / "missing.properties"])


def test_cli_properties_file_and_env_file(tmp_path: Path) -> None:
    root = _spring(tmp_path, _raw_listener('{"${kafka.topic.orders}", "${kafka.topic.pay}"}'))
    props = tmp_path / "deploy.yml"
    props.write_text("kafka:\n  topic:\n    orders: o.v1\n", encoding="utf-8")
    env = tmp_path / "deploy.env"
    env.write_text("KAFKA_TOPIC_PAY=p.v1\n", encoding="utf-8")
    out = tmp_path / "m.json"
    result = CliRunner().invoke(main, [
        "ontology", "scan", str(root), "-o", str(out),
        "--properties-file", str(props), "--env-file", str(env)])
    assert result.exit_code == 0, result.output
    keys = {e["key"] for e in json.loads(out.read_text())["entities"] if e["type"] == "KafkaTopic"}
    assert keys == {"o.v1", "p.v1"}
    missing = CliRunner().invoke(main, ["ontology", "scan", str(root), "--env-file",
                                        str(tmp_path / "nope.env")])
    assert missing.exit_code != 0


# -- 4. @ConfigurationProperties ---------------------------------------------------------

CONFIG_YML = ("app:\n  kafka:\n    topic: cfg.topic\n    orders:\n      topic: cfg.orders\n"
              "    order-events: cfg.events\n")


def _config_java(annotation: str) -> str:
    return (
        "package com.acme;\n"
        f"{annotation}\npublic class KafkaProps {{\n"
        "  private String topic;\n  private String orderEvents;\n  private Orders orders;\n"
        "  public String getTopic() { return topic; }\n"
        "  public Orders getOrders() { return orders; }\n"
        "  public static class Orders {\n    private String topic;\n"
        "    public String getTopic() { return topic; }\n  }\n}\n")


@pytest.mark.parametrize("annotation", [
    '@ConfigurationProperties(prefix = "app.kafka")', '@ConfigurationProperties("app.kafka")',
    '@ConfigurationProperties(value = "app.kafka", ignoreUnknownFields = false)'])
def test_configuration_properties_getters(tmp_path: Path, annotation: str) -> None:
    files = {
        "src/main/resources/application.yml": CONFIG_YML,
        "src/main/java/com/acme/KafkaProps.java": _config_java(annotation),
        JAVA: ("class K {\n  private final KafkaProps config;\n"
               "  void a() { kafkaTemplate.send(config.getTopic(), v); }\n"
               "  void b() { kafkaTemplate.send(config.getOrders().getTopic(), v); }\n"
               "  void c() { kafkaTemplate.send(config.getOrderEvents(), v); }\n"
               "  void d() { kafkaTemplate.send(props.getMissing(), v); }\n}\n")}
    result = scan_repository(_spring(tmp_path, files))
    topics = _topics(result)
    assert {"cfg.topic", "cfg.orders", "cfg.events"} <= set(topics)
    assert topics["cfg.orders"].attributes["resolved_from"] == "property:app.kafka.orders.topic"
    assert topics["cfg.topic"].attributes["config_class"] == "KafkaProps"


def test_configuration_properties_kotlin_and_unresolved_key(tmp_path: Path) -> None:
    files = {
        "src/main/kotlin/KafkaProps.kt": (
            '@ConfigurationProperties(prefix = "app.kafka")\n'
            "data class KafkaProps(val topic: String, val orders: Orders) {\n"
            "  data class Orders(val topic: String)\n}\n"),
        "src/main/kotlin/K.kt": (
            "class K(private val config: KafkaProps) {\n"
            "  fun a() { kafkaTemplate.send(config.topic, v) }\n"
            "  fun b() { kafkaTemplate.send(config.orders.topic, v) }\n}\n")}
    result = scan_repository(_spring(tmp_path, files))
    assert set(_topics(result)) == {"unresolved:app.kafka.topic", "unresolved:app.kafka.orders.topic"}
    filled = scan_repository(_spring(tmp_path / "again", files), extra_properties={
        "app.kafka.topic": "k.t", "APP_KAFKA_ORDERS_TOPIC": "k.o"})
    assert set(_topics(filled)) == {"k.t", "k.o"}


def test_configuration_properties_spel_bean_field(tmp_path: Path) -> None:
    files = {
        APP: "app.kafka.topic=bean.t\n",
        "src/main/java/com/acme/KafkaProps.java": _config_java(
            '@ConfigurationProperties(prefix = "app.kafka")'),
        JAVA: 'class K {\n  @KafkaListener(topics = "#{@kafkaProps.topic}") void a() {}\n}\n'}
    assert set(_topics(scan_repository(_spring(tmp_path, files)))) == {"bean.t"}


def test_plain_class_without_annotation_is_not_resolved(tmp_path: Path) -> None:
    files = {
        APP: "app.kafka.topic=nope\n",
        "src/main/java/com/acme/KafkaProps.java": _config_java(""),
        JAVA: "class K {\n  KafkaProps config;\n  void a() { kafkaTemplate.send(config.getTopic(), v); }\n}\n"}
    assert "nope" not in _topics(scan_repository(_spring(tmp_path, files)))


# -- 5. stable unresolved keys -----------------------------------------------------------


def test_unresolved_keys_are_stable_across_files_and_whitespace(tmp_path: Path) -> None:
    expr = "#{@ghost.topic + '-x'}"
    files = {
        JAVA: _listener(expr)[JAVA],
        "src/main/java/com/acme/Other.java": _listener("#{ @ghost.topic  +  '-x' }")[JAVA].replace(
            "class K", "class Other"),
    }
    result = scan_repository(_spring(tmp_path, files))
    (key,) = _topics(result)
    assert SPEL_KEY.fullmatch(key)
    entity = _topics(result)[key]
    assert entity.attributes["expression"] in (expr, "#{ @ghost.topic + '-x' }")
    assert entity.attributes["unresolved"] is True
    assert len([r for r in result.manifest.relations if r.dst == f"KafkaTopic:{key}"]) == 1


def test_plain_placeholder_keys_stay_readable(tmp_path: Path) -> None:
    root = _spring(tmp_path, _listener("${a.b.c}"))
    assert set(_topics(scan_repository(root))) == {"unresolved:a.b.c"}


def test_different_spel_expressions_get_different_keys(tmp_path: Path) -> None:
    files = {JAVA: ("class K {\n  @KafkaListener(topics = \"#{@a.x}\") void a() {}\n"
                    "  @KafkaListener(topics = \"#{@b.y}\") void b() {}\n}\n")}
    keys = set(_topics(scan_repository(_spring(tmp_path, files))))
    assert len(keys) == 2 and all(SPEL_KEY.fullmatch(k) for k in keys)


# -- unresolved is counted per site, not per ternary branch -------------------------------

_TERNARY = "#{${m.a}=='A' ? '${x.t1}' : '${x.t2}'}"


def _ternary_send(props: str) -> dict[str, str]:
    return {
        APP: props,
        JAVA: ('class K {\n  @Value("' + _TERNARY + '") private String topic;\n'
               "  void a() { kafkaTemplate.send(topic, v); }\n}\n"),
    }


def test_ternary_send_with_all_unresolved_is_one_site_and_one_key(tmp_path: Path) -> None:
    result = scan_repository(_spring(tmp_path, _ternary_send("other=1\n")))
    kafka = result.summary["kafka"]
    assert kafka["unresolved_placeholders"] == 1
    assert kafka["unresolved_unique_keys"] == 1
    produces = [r for r in result.manifest.relations if r.type == "PRODUCES"]
    assert len(produces) == 1


def test_ternary_send_with_one_known_branch_counts_one_unresolved_site(tmp_path: Path) -> None:
    result = scan_repository(_spring(tmp_path, _ternary_send("x.t1=known.topic\n")))
    kafka = result.summary["kafka"]
    assert kafka["unresolved_placeholders"] == 1
    assert kafka["unresolved_unique_keys"] == 1
    assert "known.topic" in _topics(result)
