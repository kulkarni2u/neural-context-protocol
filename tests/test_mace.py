from __future__ import annotations

import json
from pathlib import Path

from benchmarks.mace.run import run_mace


def test_mace_run_writes_results_and_scores(tmp_path: Path) -> None:
    results_dir = tmp_path / "results"
    store_path = tmp_path / "mace.db"

    artifact = run_mace(
        turns=24,
        results_dir=results_dir,
        store_path=store_path,
    )

    assert artifact["benchmark"] == "MACE"
    assert artifact["version"] == "1.0"
    assert set(artifact["dimensions"]) == {"d1", "d2", "d3", "d4"}
    for result in artifact["dimensions"].values():
        assert 0.0 <= result["score"] <= 1.0

    ncp_path = results_dir / "ncp.json"
    baseline_path = results_dir / "baseline.json"
    trace_path = results_dir / "traces" / "ncp_trace.json"
    assert ncp_path.exists()
    assert baseline_path.exists()
    assert trace_path.exists()

    baseline = json.loads(baseline_path.read_text())
    assert baseline["dimensions"]["d3"]["score"] < artifact["dimensions"]["d3"]["score"]
    assert baseline["dimensions"]["d4"]["score"] < artifact["dimensions"]["d4"]["score"]

    # Baseline must be non-trivial (not all zeros)
    assert baseline["dimensions"]["d2"]["score"] > 0.0
    assert baseline["dimensions"]["d3"]["score"] > 0.0
    assert baseline["dimensions"]["d4"]["score"] > 0.0

    # NCP must beat baseline on all scored dimensions
    for dim in ("d2", "d3", "d4"):
        assert artifact["dimensions"][dim]["score"] > baseline["dimensions"][dim]["score"], (
            f"{dim}: NCP score {artifact['dimensions'][dim]['score']} not > baseline {baseline['dimensions'][dim]['score']}"
        )


def test_mace_template_schema_is_compatible() -> None:
    template_path = Path("benchmarks/mace/results/community/TEMPLATE.json")
    payload = json.loads(template_path.read_text())

    assert payload["benchmark"] == "MACE"
    assert set(payload["dimensions"]) == {"d1", "d2", "d3", "d4"}
    assert "composite_score" in payload
