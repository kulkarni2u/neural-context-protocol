from __future__ import annotations

from pathlib import Path

from ncp.bench.memo_reuse import run_memo_reuse_benchmark


def test_memo_reuse_benchmark_passes_gate(tmp_path: Path) -> None:
    artifact = run_memo_reuse_benchmark(workdir=tmp_path)
    arms = artifact["arms"]

    assert artifact["summary"]["pass"] is True, artifact["summary"]
    assert arms["memo_dependency_safe"]["false_hits"] == 0
    assert arms["memo_off"]["verified_successes"] == arms["memo_off"]["requests"]
    assert arms["naive_key_counterfactual"]["would_false_hit"] > 0


def test_memo_reuse_benchmark_is_deterministic(tmp_path: Path) -> None:
    first = run_memo_reuse_benchmark(workdir=tmp_path / "a", tasks=6, rounds=3, mutate_every=2)
    second = run_memo_reuse_benchmark(workdir=tmp_path / "b", tasks=6, rounds=3, mutate_every=2)
    assert first == second


def test_no_mutation_means_no_hazard(tmp_path: Path) -> None:
    artifact = run_memo_reuse_benchmark(workdir=tmp_path, tasks=4, rounds=3, mutate_every=10**6)
    # Only task 0 is ever mutated with this stride; check the arm math stays consistent.
    safe = artifact["arms"]["memo_dependency_safe"]
    assert safe["provider_calls"] + safe["memo_hits"] == safe["requests"]
    assert safe["false_hits"] == 0
