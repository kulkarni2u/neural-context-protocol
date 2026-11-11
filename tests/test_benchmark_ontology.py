"""Fast checks for the ontology benchmark harness (tiny corpus slice)."""

from __future__ import annotations

from pathlib import Path

from ncp.bench.ontology import run_ontology_benchmark


def _run(path: Path) -> dict:
    return run_ontology_benchmark(
        workdir=path, repeats=1, ladder=(300, 800), seeds=("checkout", "inventory"),
        controls=("rotate_tls_certificates",),
        # Generous time budgets keep the quality numbers independent of machine load.
        ontology_toml="[ontology]\nenabled = true\ntrigger_max_ms = 1000\ngraph_timeout_ms = 5000\n",
    )


def test_ontology_benchmark_reports_both_arms(tmp_path: Path) -> None:
    artifact = _run(tmp_path)
    sqlite = artifact["backends"]["sqlite"]
    baseline, ontology = sqlite["arms"]["baseline"], sqlite["arms"]["ontology"]
    dep_base, dep_ont = baseline["families"]["dependency"], ontology["families"]["dependency"]
    # The baseline never enriches; the ontology arm triggers on every dependency task
    # and on no control task.
    assert dep_base["triggered"] == 0 and dep_ont["triggered"] == 2
    assert ontology["families"]["control"]["triggered"] == 0
    assert dep_ont["fallbacks"] == 0
    assert dep_ont["fact_recall_mean"] == 1.0
    assert dep_ont["fact_recall_mean"] >= dep_base["fact_recall_mean"]
    # Planted candidate and superseded-evidence relations never surface.
    assert ontology["surfaced_relations"] > 0
    assert ontology["unsupported_surfaced"] == 0
    assert sqlite["comparison"]["hypotheses"]["zero_unsupported_assertions"] is True
    # Control tasks are untouched by the ontology.
    assert sqlite["comparison"]["control_prompt_token_delta"] == 0
    assert sqlite["comparison"]["control_evidence_recall_delta"] == 0
    for family in (dep_base, dep_ont):
        assert family["latency_ms_p95"] >= family["latency_ms_p50"] > 0
        assert family["sql_statements_mean"] > 0
    assert dep_ont["sql_statements_mean"] > dep_base["sql_statements_mean"]


def test_ontology_benchmark_quality_is_deterministic(tmp_path: Path) -> None:
    volatile = {"graph_latency_ms"}

    def quality(artifact: dict) -> list[dict]:
        return [
            {key: value for key, value in row.items() if key not in volatile}
            for arm in artifact["backends"]["sqlite"]["arms"].values() for row in arm["tasks"]
        ]

    assert quality(_run(tmp_path / "a")) == quality(_run(tmp_path / "b"))


def test_unsupported_citation_check_flags_dead_or_missing_evidence() -> None:
    from ncp.bench.ontology import Fact, _unsupported_citations

    facts = {
        "r1": Fact("DEPENDS_ON", "a", "b", "ev-1"),
        "r2": Fact("DEPENDS_ON", "b", "c", "ev-2"),
        "stale": Fact("DEPENDS_ON", "a", "c", "ev-old", supported=False),
    }

    def check(relation_ids: list[str], evidence_ids: list[str]) -> int:
        response = {"ontology": {"paths": [
            {"relation_ids": relation_ids, "evidence_ids": evidence_ids},
        ]}}
        return _unsupported_citations(response, facts, {"ev-old"})

    assert check(["r1", "r2"], ["ev-1", "ev-2"]) == 0
    assert check(["r1", "r2"], ["ev-1"]) == 1             # r2 shown without its evidence
    assert check(["r1"], ["ev-1", "ev-old"]) == 1         # cites superseded evidence
    assert check(["r1"], ["ev-1", "unrelated-chunk"]) == 1  # cites evidence of no relation shown
