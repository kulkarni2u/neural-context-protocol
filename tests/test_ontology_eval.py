"""``ncp ontology eval``: scoring a scanned manifest against a golden one."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from ncp.cli import main
from ncp.ontology.facade import OntologyError
from ncp.ontology.scan import scan_repository
from ncp.ontology.scan_eval import evaluate, load_raw, split_eval

REPO = Path(__file__).resolve().parent.parent
EVAL_DIR = REPO / "examples" / "13_ontology" / "eval"


def _rel(src: str, rtype: str, dst: str, status: str = "candidate") -> dict[str, object]:
    return {"src": src, "type": rtype, "dst": dst, "status": status}


def _manifest(entities: list[tuple[str, str]], relations: list[dict[str, object]],
              aliases: dict[str, list[str]] | None = None) -> dict[str, object]:
    aliases = aliases or {}
    return {
        "entities": [{"type": t, "key": k, "aliases": aliases.get(k, [])} for t, k in entities],
        "relations": relations,
    }


BASE_ENTITIES = [("Service", "order-api"), ("KafkaTopic", "order.events"), ("KafkaTopic", "pay.events")]
EXPECTED = _manifest(
    BASE_ENTITIES,
    [_rel("Service:order-api", "PRODUCES", "KafkaTopic:order.events"),
     _rel("Service:order-api", "CONSUMES", "KafkaTopic:pay.events")],
)


def test_perfect_match() -> None:
    report = evaluate(EXPECTED, EXPECTED)
    assert report.relations.precision == report.relations.recall == report.relations.f1 == 1.0
    assert report.entities.f1 == 1.0
    assert not (report.missing_relations or report.unexpected_relations or report.unresolved)


def test_false_positive_and_false_negative() -> None:
    actual = _manifest(
        [*BASE_ENTITIES, ("KafkaTopic", "extra.events")],
        [_rel("Service:order-api", "PRODUCES", "KafkaTopic:order.events"),
         _rel("Service:order-api", "PRODUCES", "KafkaTopic:extra.events")],
    )
    report = evaluate(EXPECTED, actual)
    assert (report.relations.tp, report.relations.fp, report.relations.fn) == (1, 1, 1)
    assert report.relations.precision == 0.5 and report.relations.recall == 0.5
    assert any("CONSUMES" in item for item in report.missing_relations)
    assert any("extra.events" in item for item in report.unexpected_relations)
    assert report.unexpected_entities == ["KafkaTopic:extra.events"]


def test_alias_match_on_entities_and_relations() -> None:
    expected = _manifest(
        [("Service", "orders"), ("KafkaTopic", "order.events")],
        [_rel("Service:orders", "PRODUCES", "KafkaTopic:order.events")],
        aliases={"orders": ["order-api"]},
    )
    actual = _manifest(
        [("Service", "order-api"), ("KafkaTopic", "Order.Events")],
        [_rel("order-api", "PRODUCES", "KafkaTopic:Order.Events")],
    )
    report = evaluate(expected, actual)
    assert report.entities.recall == 1.0 and report.entities.precision == 1.0
    assert report.relations.tp == 1 and report.relations.fp == 0


def test_unresolved_bucket() -> None:
    actual = _manifest(
        [("Service", "order-api"), ("KafkaTopic", "unresolved:${topic}")],
        [_rel("Service:order-api", "PRODUCES", "KafkaTopic:unresolved:${topic}")],
    )
    report = evaluate(EXPECTED, actual)
    assert report.relations.fp == 0 and report.relations.fn == 2
    assert report.unexpected_entities == []
    assert len(report.unresolved) == 1
    entry = report.unresolved[0]
    assert "unresolved" in str(entry["actual"])
    assert entry["expected"] is not None and "PRODUCES" in str(entry["expected"])


def test_per_type_metrics() -> None:
    actual = _manifest(
        BASE_ENTITIES, [_rel("Service:order-api", "PRODUCES", "KafkaTopic:order.events")],
    )
    report = evaluate(EXPECTED, actual)
    assert report.relations_by_type["PRODUCES"].recall == 1.0
    assert report.relations_by_type["CONSUMES"].recall == 0.0
    assert report.entities_by_type["KafkaTopic"].tp == 2
    data = report.to_dict()
    assert data["relations_by_type"]["CONSUMES"]["fn"] == 1  # type: ignore[index]


def test_type_filters_and_eval_ignore() -> None:
    only = evaluate(EXPECTED, _manifest(BASE_ENTITIES, []), relation_types=["PRODUCES"])
    assert only.relations.fn == 1
    golden = {**EXPECTED, "eval": {"ignore_relation_types": ["CONSUMES"]}}
    report = evaluate(golden, _manifest(BASE_ENTITIES, []))
    assert report.relations.fn == 1
    ignoring_entities = {**EXPECTED, "eval": {"ignore_entity_types": ["KafkaTopic"]}}
    report = evaluate(ignoring_entities, _manifest([("Service", "order-api")], []))
    assert report.relations.fn == 0 and report.entities.fn == 0


def test_status_mismatch_only_when_not_ignored() -> None:
    expected = _manifest(
        BASE_ENTITIES[:2],
        [{**_rel("Service:order-api", "PRODUCES", "KafkaTopic:order.events", "verified"),
          "evidence": "checked"}],
    )
    actual = _manifest(
        BASE_ENTITIES[:2], [_rel("Service:order-api", "PRODUCES", "KafkaTopic:order.events")],
    )
    assert evaluate(expected, actual).status_mismatches == []
    mismatch = evaluate(expected, actual, ignore_status=False).status_mismatches
    assert len(mismatch) == 1 and "expected verified, got candidate" in mismatch[0]


def test_eval_key_stripped_and_unknown_rejected() -> None:
    body, options = split_eval({**EXPECTED, "eval": {"ignore_entity_types": ["Team"]}})
    assert "eval" not in body and options == {"ignore_entity_types": ["Team"]}
    with pytest.raises(OntologyError):
        split_eval({"eval": {"bogus": 1}})
    with pytest.raises(ValueError):
        evaluate(EXPECTED, EXPECTED, match="fuzzy")


def test_golden_example_scores_full_recall() -> None:
    result = scan_repository(EVAL_DIR / "repo", repo_name="repo")
    report = evaluate(load_raw(EVAL_DIR / "expected.json"), result.manifest)
    assert report.relations.recall == 1.0 and report.relations.precision == 1.0
    kafka = {k: v for k, v in report.relations_by_type.items() if k in ("PRODUCES", "CONSUMES")}
    assert set(kafka) == {"PRODUCES", "CONSUMES"} and all(m.recall == 1.0 for m in kafka.values())


class TestEvalCli:
    def test_scan_mode(self) -> None:
        result = CliRunner().invoke(main, [
            "ontology", "eval", str(EVAL_DIR / "expected.json"),
            "--scan", str(EVAL_DIR / "repo"), "--repo-name", "repo", "--by-type",
            "--min-recall", "1.0", "--min-precision", "1.0",
        ])
        assert result.exit_code == 0, result.output
        assert "recall 1.000" in result.output and "rel CONSUMES" in result.output

    def test_actual_mode_json_and_thresholds(self, tmp_path: Path) -> None:
        actual = tmp_path / "actual.json"
        actual.write_text(json.dumps(_manifest(
            BASE_ENTITIES, [_rel("Service:order-api", "PRODUCES", "KafkaTopic:order.events")],
        )))
        golden = tmp_path / "expected.json"
        golden.write_text(json.dumps({**EXPECTED, "eval": {}}))
        runner = CliRunner()
        ok = runner.invoke(main, ["ontology", "eval", str(golden), "--actual", str(actual),
                                  "--json", "--min-precision", "1.0"])
        assert ok.exit_code == 0, ok.output
        payload = json.loads(ok.output)
        assert payload["relations"]["recall"] == 0.5 and payload["failures"] == []
        low = runner.invoke(main, ["ontology", "eval", str(golden), "--actual", str(actual),
                                   "--min-recall", "0.9"])
        assert low.exit_code == 1 and "recall" in low.output

    def test_requires_exactly_one_source(self, tmp_path: Path) -> None:
        golden = tmp_path / "e.json"
        golden.write_text(json.dumps(EXPECTED))
        result = CliRunner().invoke(main, ["ontology", "eval", str(golden)])
        assert result.exit_code != 0
