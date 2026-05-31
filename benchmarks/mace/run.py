#!/usr/bin/env python3
"""MACE — Multi-Agent Context Efficiency Benchmark."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile

from ncp.version import __version__ as NCP_VERSION

from benchmarks.mace.dimensions.d1_token_efficiency import D1TokenEfficiency
from benchmarks.mace.dimensions.d2_handoff_quality import D2HandoffQuality
from benchmarks.mace.dimensions.d3_deadend_prevention import D3DeadEndPrevention
from benchmarks.mace.dimensions.d4_goal_coherence import D4GoalCoherence
from benchmarks.mace.harness.baseline import build_baseline_result
from benchmarks.mace.harness.pipeline import MACEPipeline
from benchmarks.mace.harness.scoring import weighted_mean


ROOT = Path(__file__).resolve().parent


def load_config(path: str | Path | None = None) -> dict[str, object]:
    config_path = Path(path) if path is not None else ROOT / "config.yaml"
    return json.loads(config_path.read_text())


def load_fixture(name: str) -> dict[str, object]:
    return json.loads((ROOT / "fixtures" / name).read_text())


def _resolve_results_dir(config: dict[str, object], override: str | Path | None) -> Path:
    if override is not None:
        return Path(override)
    return ROOT / str(config["output"]["results_dir"])


def _save_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def compare_results(ncp_results: dict[str, object], other_path: str) -> None:
    other = json.loads(Path(other_path).read_text())
    print(f"\n── Comparison: NCP vs {other.get('system', 'other')} ──")
    print(f"{'Dimension':<25} {'NCP':>8} {'Other':>8} {'Delta':>8}")
    print("─" * 55)
    for dim in ["d1", "d2", "d3", "d4"]:
        ncp_s = ncp_results["dimensions"].get(dim, {}).get("score", 0)  # type: ignore[index]
        other_s = other.get("dimensions", {}).get(dim, {}).get("score", 0)
        delta = float(ncp_s) - float(other_s)
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "=")
        label = {
            "d1": "Token Efficiency",
            "d2": "Handoff Quality",
            "d3": "Dead-end Prevention",
            "d4": "Goal Coherence",
        }[dim]
        print(f"  {label:<23} {float(ncp_s):>8.4f} {float(other_s):>8.4f} {arrow}{abs(delta):>7.4f}")
    ncp_c = float(ncp_results["composite_score"])
    other_c = float(other.get("composite_score", 0))
    print("─" * 55)
    print(f"  {'Composite MACE':<23} {ncp_c:>8.4f} {other_c:>8.4f} {'↑' if ncp_c > other_c else '↓'}{abs(ncp_c-other_c):>7.4f}")


def run_mace(
    dims: str = "all",
    provider: str = "anthropic",
    turns: int = 40,
    compare_file: str | None = None,
    verbose: bool = False,
    *,
    config_path: str | Path | None = None,
    results_dir: str | Path | None = None,
    store_path: str | Path | None = None,
) -> dict[str, object]:
    config = load_config(config_path)
    config["pipeline"]["turns"] = turns  # type: ignore[index]
    config["providers"]["primary"] = provider  # type: ignore[index]
    task = load_fixture("task_decompose.json")
    run_dims = ["d1", "d2", "d3", "d4"] if dims == "all" else [item.strip() for item in dims.split(",") if item.strip()]
    results: dict[str, object] = {
        "benchmark": "MACE",
        "version": "1.0",
        "timestamp": datetime.now(UTC).isoformat(),
        "system": "NCP",
        "system_version": NCP_VERSION,
        "provider": provider,
        "turns": turns,
        "dimensions": {},
        "composite_score": None,
    }
    traces: dict[str, object] = {}

    def _run_with_pipeline(callback):
        if store_path is not None:
            pipeline = MACEPipeline(store_path=store_path, pipeline_id="mace_pipeline")
            try:
                return callback(pipeline)
            finally:
                pipeline.close()
        with tempfile.TemporaryDirectory(prefix="mace-bench-") as tmpdir:
            pipeline = MACEPipeline(store_path=Path(tmpdir) / "mace.db", pipeline_id="mace_pipeline")
            try:
                return callback(pipeline)
            finally:
                pipeline.close()

    d1_result: dict[str, object] | None = None
    if "d1" in run_dims:
        bench_store = Path(store_path) if store_path is not None else Path(tempfile.mkdtemp(prefix="mace-d1-")) / "d1.db"
        d1 = D1TokenEfficiency(config)
        d1_result = d1.run(store_path=bench_store, pipeline_id="mace_d1_pipeline")
        results["dimensions"]["d1"] = {k: v for k, v in d1_result.items() if k != "trace"}  # type: ignore[index]
        traces["d1"] = d1_result["trace"]

    if "d2" in run_dims:
        d2 = D2HandoffQuality()
        d2_result = _run_with_pipeline(lambda pipeline: d2.run(pipeline, task))
        results["dimensions"]["d2"] = {k: v for k, v in d2_result.items() if k != "trace"}  # type: ignore[index]
        traces["d2"] = d2_result["trace"]

    if "d3" in run_dims:
        d3 = D3DeadEndPrevention(known_dead_ends=list(task["known_dead_ends"]))  # type: ignore[arg-type]
        d3_result = _run_with_pipeline(lambda pipeline: d3.run(pipeline, task))
        results["dimensions"]["d3"] = {k: v for k, v in d3_result.items() if k != "trace"}  # type: ignore[index]
        traces["d3"] = d3_result["trace"]

    if "d4" in run_dims:
        d4 = D4GoalCoherence(goal_change_turn=int(task["goal_change"]["at_turn"]))  # type: ignore[index]
        d4_result = _run_with_pipeline(
            lambda pipeline: d4.run(
                pipeline,
                task,
                turns=turns,
                agents=list(config["pipeline"]["agents"]),  # type: ignore[arg-type]
            )
        )
        results["dimensions"]["d4"] = {k: v for k, v in d4_result.items() if k != "trace"}  # type: ignore[index]
        traces["d4"] = d4_result["trace"]

    weights = {
        "d1": float(config["scoring"]["d1_weight"]),  # type: ignore[index]
        "d2": float(config["scoring"]["d2_weight"]),  # type: ignore[index]
        "d3": float(config["scoring"]["d3_weight"]),  # type: ignore[index]
        "d4": float(config["scoring"]["d4_weight"]),  # type: ignore[index]
    }
    scores = {dim: float(payload["score"]) for dim, payload in results["dimensions"].items()}  # type: ignore[index]
    results["composite_score"] = round(weighted_mean(scores, weights), 4)

    baseline = build_baseline_result(
        provider=provider,
        turns=turns,
        system_version=NCP_VERSION,
        d1_result=d1_result,
        run_dims=run_dims,
    )

    result_root = _resolve_results_dir(config, results_dir)
    _save_json(result_root / "ncp.json", results)
    if bool(config["output"]["save_baseline"]):  # type: ignore[index]
        _save_json(result_root / "baseline.json", baseline)
    if bool(config["output"]["save_traces"]):  # type: ignore[index]
        _save_json(result_root / "traces" / "ncp_trace.json", {"benchmark": "MACE", "traces": traces})

    print("\n╔══════════════════════════════════════════════╗")
    print("║  MACE — Multi-Agent Context Efficiency       ║")
    print("║  Neural Context Protocol Benchmark Suite     ║")
    print("╚══════════════════════════════════════════════╝\n")
    print("┌─────────────────────────────────────────────┐")
    print("│  MACE Results                               │")
    print("├─────────────────────────────────────────────┤")
    for dim, result in results["dimensions"].items():  # type: ignore[index]
        label = {
            "d1": "D1 Token Efficiency   ",
            "d2": "D2 Handoff Quality    ",
            "d3": "D3 Dead-end Prevention",
            "d4": "D4 Goal Coherence     ",
        }.get(dim, dim)
        score = float(result["score"])
        bar = "█" * int(score * 20)
        print(f"│  {label}  {score:.4f}  {bar}")
    print("├─────────────────────────────────────────────┤")
    print(f"│  Composite MACE Score        {float(results['composite_score']):.4f}  │")
    print("└─────────────────────────────────────────────┘")
    print(f"\nFull results saved to: {result_root / 'ncp.json'}")

    if compare_file:
        compare_results(results, compare_file)
    if verbose:
        print(json.dumps(results, indent=2))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dims", default="all", help="Dimensions to run: all or d1,d2,d3,d4")
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument("--turns", type=int, default=40)
    parser.add_argument("--compare", default=None, help="Path to another system's results JSON")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--results-dir", default=None)
    parser.add_argument("--store-path", default=None)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    run_mace(
        dims=args.dims,
        provider=args.provider,
        turns=args.turns,
        compare_file=args.compare,
        verbose=args.verbose,
        config_path=args.config,
        results_dir=args.results_dir,
        store_path=args.store_path,
    )


if __name__ == "__main__":
    main()
