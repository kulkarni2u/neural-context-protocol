"""D1 token-efficiency benchmark wired to the existing coding pipeline benchmark."""

from __future__ import annotations

from pathlib import Path

from ncp.benchmarks import run_coding_pipeline_benchmark

from benchmarks.mace.harness.scoring import clamp_score


class D1TokenEfficiency:
    """Use the existing coding pipeline benchmark as the canonical D1 source."""

    PRIMARY_CHECKPOINT = 40

    def __init__(self, config: dict[str, object]):
        self.config = config
        self.artifact: dict[str, object] | None = None

    def run(self, *, store_path: str | Path, pipeline_id: str) -> dict[str, object]:
        turns = int(self.config["pipeline"]["turns"])
        self.artifact = run_coding_pipeline_benchmark(
            store_path=store_path,
            turns=turns,
            pipeline_id=pipeline_id,
        )
        return self.score()

    def score(self) -> dict[str, object]:
        if self.artifact is None:
            raise RuntimeError("run() must be called before score()")

        checkpoints: list[int] = list(self.config["pipeline"]["turn_checkpoints"])
        turn_rows: list[dict[str, object]] = list(self.artifact["turn_rows"])  # type: ignore[index]
        checkpoint_results: dict[str, dict[str, float | int]] = {}
        available_turns = {int(row["turn"]): row for row in turn_rows}
        for checkpoint in checkpoints:
            row = available_turns.get(checkpoint)
            if row is None:
                continue
            baseline_tokens = int(row["naive_input_tokens"])
            ncp_tokens = int(row["ncp_input_tokens"])
            ratio = baseline_tokens / ncp_tokens if ncp_tokens else 1.0
            checkpoint_results[f"turn_{checkpoint}"] = {
                "baseline_tokens": baseline_tokens,
                "ncp_tokens": ncp_tokens,
                "reduction_ratio": round(ratio, 2),
                "reduction_pct": round((1 - 1 / ratio) * 100, 1) if ratio > 0 else 0.0,
            }

        primary_turn = self.PRIMARY_CHECKPOINT if f"turn_{self.PRIMARY_CHECKPOINT}" in checkpoint_results else max(
            int(name.split("_")[1]) for name in checkpoint_results
        )
        ratio_at_primary = float(checkpoint_results[f"turn_{primary_turn}"]["reduction_ratio"])
        score = clamp_score((ratio_at_primary - 1) / 19)
        return {
            "dimension": "D1_token_efficiency",
            "score": round(score, 4),
            "primary_checkpoint": primary_turn,
            "primary_reduction_ratio": ratio_at_primary,
            "checkpoints": checkpoint_results,
            "trace": turn_rows,
        }
